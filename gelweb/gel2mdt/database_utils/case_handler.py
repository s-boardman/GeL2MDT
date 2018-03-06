import os
import json
import hashlib
import labkey as lk
from datetime import datetime
from django.utils.dateparse import parse_date
import time
from ..models import *
from ..api_utils.poll_api import PollAPI
from ..vep_utils.run_vep_batch import CaseVariant, CaseTranscript
from ..config import load_config
import re
import pprint


class Case(object):
    """
    Representation of a single case which can be added to the database,
    updated in the database, or skipped dependent on whether a matching
    case/case family is found.
    """
    def __init__(self, case_json, panel_manager, variant_manager, gene_manager, skip_demographics=False):
        self.json = case_json
        self.json_case_data = self.json["interpretation_request_data"]
        self.json_request_data = self.json_case_data["json_request"]
        self.request_id = str(
            self.json["interpretation_request_id"]) \
            + "-" + str(self.json["version"])

        self.json_hash = self.hash_json()
        self.proband = self.get_proband_json()
        self.family_members = self.get_family_members()
        self.tools_and_versions = self.get_tools_and_versions()
        self.status = self.get_status_json()
        self.json_variants \
            = self.json_case_data["json_request"]["TieredVariants"]

        self.panel_manager = panel_manager
        self.variant_manager = variant_manager
        self.gene_manager = gene_manager

        self.panels = self.get_panels_json()
        self.variants = self.get_case_variants()
        self.transcripts = []  # set by MCM with a call to vep_utils

        # initialise a dict to contain the AttributeManagers for this case,
        # which will be set by the MCA as they are required (otherwise there
        # are missing dependencies)
        self.skip_demographics = skip_demographics
        self.attribute_managers = {}

    def hash_json(self):
        """
        Hash the given json for this Case, sorting the keys to ensure
        that order is preserved, or else different order -> different
        hash.
        """
        hash_buffer = json.dumps(self.json, sort_keys=True).encode('utf-8')
        hash_hex = hashlib.sha512(hash_buffer)
        hash_digest = hash_hex.hexdigest()
        return hash_digest

    def get_proband_json(self):
        """
        Get the proband from the list of partcipants in the JSON.
        """
        participant_jsons = \
            self.json_case_data["json_request"]["pedigree"]["participants"]
        proband_json = None
        for participant in participant_jsons:
            if participant["isProband"]:
                proband_json = participant
        return proband_json

    def get_family_members(self):
        '''
        Gets the family member details from the JSON.
        :return: A list of dictionaries containing family member details (gel ID, relationship and affection status)
        '''
        family_members = []
        participant_jsons = \
            self.json_case_data["json_request"]["pedigree"]["participants"]
        for participant in participant_jsons:
            if not participant["isProband"] and participant["additionalInformation"]:
                if "relation_to_proband" not in participant["additionalInformation"]:
                    continue
                family_member = {'gel_id': participant["gelId"],
                                 'relation_to_proband': participant["additionalInformation"]["relation_to_proband"],
                                 'affection_status': participant["affectionStatus"],
                                 'sex': participant['sex'],
                                 }
                family_members.append(family_member)
        return family_members

    def get_tools_and_versions(self):
        '''
        Gets the genome build from the JSON. Details of other tools (VEP, Polyphen/SIFT) to be pulled from config file?
        :return: A dictionary of tools and versions used for the case
        '''
        tools_dict = {'genome_build': self.json_request_data["genomeAssemblyVersion"]}
        return tools_dict


    def get_status_json(self):
        """
        JSON has a list of statuses. Extract only the latest.
        """
        status_jsons = self.json["status"]
        return status_jsons[-1]  # assuming GeL will always work upwards..

    def get_panels_json(self):
        """
        Get the list of panels from the json
        """
        json_request = self.json_case_data["json_request"]
        return json_request["pedigree"]["analysisPanels"]

    def get_case_variants(self):
        """
        Create CaseVariant objects for each variant listed in the json,
        then return a list of all CaseVariants for construction of
        CaseTranscripts using VEP.
        """
        json_variants = self.json_variants
        case_variant_list = []
        # go through each variant in the json
        variant_object_count = 0
        for variant in json_variants:
            # check if it has any Tier1 or Tier2 Report Events
            variant_min_tier = None
            for report_event in variant["reportEvents"]:
                tier = int(report_event["tier"][-1])
                if variant_min_tier is None:
                    variant_min_tier = tier
                elif tier < variant_min_tier:
                    variant_min_tier = tier
            variant["max_tier"] = variant_min_tier

            if variant["max_tier"] < 3:
                variant_object_count += 1
                case_variant = CaseVariant(
                    chromosome=variant["chromosome"],
                    position=variant["position"],
                    ref=variant["reference"],
                    alt=variant["alternate"],
                    case_id=self.request_id,
                    variant_count=str(variant_object_count),
                    genome_build=self.json_request_data["genomeAssemblyVersion"]
                )
                case_variant_list.append(case_variant)
                # also add it to the dict within self.json_variants
                variant["case_variant"] = case_variant
            else:
                # if the variant is Tier 3, assign False to the dict within
                # self.json_variants so we don't add a variant later
                variant["case_variant"] = False

        # check for CIP flagged variants
        for interpreted_genome in self.json["interpreted_genome"]:
            for variant in interpreted_genome["interpreted_genome_data"]["reportedVariants"]:
                variant_object_count += 1
                case_variant = CaseVariant(
                    chromosome=variant["chromosome"],
                    position=variant["position"],
                    ref=variant["reference"],
                    alt=variant["alternate"],
                    case_id=self.request_id,
                    variant_count=str(variant_object_count),
                    genome_build=self.json_request_data["genomeAssemblyVersion"]
                )
                case_variant_list.append(case_variant)
                # also add it to the dict within self.json_variants
                variant["case_variant"] = case_variant

        return case_variant_list


class CaseAttributeManager(object):
    """
    Handler for managing each different type of case attribute.
    Holds get/refresh functions to be called by MCA, as well as pointing to
    CaseModels and ManyCaseModels for access by MCA.bulk_create_new().
    """
    def __init__(self, case, model_type, model_objects, many=False):
        """
        Initialise with CaseModel or ManyCaseModel, dependent on many param.
        """
        self.case = case  # for accessing related table entries
        self.model_type = model_type
        self.model_objects = model_objects
        self.many = many
        self.case_model = self.get_case_model()

    def get_case_model(self):
        """
        Call the corresponding function to update the case model within the
        AttributeManager.
        """

        if self.model_type == Clinician:
            case_model = self.get_clinician()
        elif self.model_type == Proband:
            case_model = self.get_proband()
        elif self.model_type == Family:
            case_model = self.get_family()
        elif self.model_type == Relative:
            case_model = self.get_relatives()
        elif self.model_type == Phenotype:
            case_model = self.get_phenotypes()
        elif self.model_type == FamilyPhenotype:
            case_model = self.get_family_phenotypes()
        elif self.model_type == InterpretationReportFamily:
            case_model = self.get_ir_family()
        elif self.model_type == Panel:
            case_model = self.get_panels()
        elif self.model_type == PanelVersion:
            case_model = self.get_panel_versions()
        elif self.model_type == InterpretationReportFamilyPanel:
            case_model = self.get_ir_family_panel()
        elif self.model_type == Gene:
            case_model = self.get_genes()
        elif self.model_type == PanelVersionGene:
            case_model = self.get_panel_version_genes()
        elif self.model_type == Transcript:
            case_model = self.get_transcripts()
        elif self.model_type == GELInterpretationReport:
            case_model = self.get_ir()
        elif self.model_type == Variant:
            case_model = self.get_variants()
        elif self.model_type == TranscriptVariant:
            case_model = self.get_transcript_variants()
        elif self.model_type == ProbandVariant:
            case_model = self.get_proband_variants()
        elif self.model_type == ProbandTranscriptVariant:
            case_model = self.get_proband_transcript_variants()
        elif self.model_type == ReportEvent:
            case_model = self.get_report_events()
        elif self.model_type == ToolOrAssemblyVersion:
            case_model = self.get_tool_and_assembly_versions()

        return case_model

    def get_clinician(self):
        """
        Create a case model to handle adding/getting the clinician for case.
        """
        # family ID used to search for clinician details in labkey
        family_id = self.case.json["family_id"]
        # load in site specific details from config file
        config_dict = load_config.LoadConfig().load()

        if self.case.skip_demographics:
            # don't poll labkey
            clinician_details = {"name": "unknown", "hospital": "unknown"}
        elif not self.case.skip_demographics:
            # poll labkey
            labkey_server_request = config_dict['labkey_server_request']
            server_context = lk.utils.create_server_context(
                'gmc.genomicsengland.nhs.uk',
                labkey_server_request,
                '/labkey', use_ssl=True)

            search_results = lk.query.select_rows(
                server_context=server_context,
                schema_name='gel_rare_diseases',
                query_name='rare_diseases_registration',
                filter_array=[
                    lk.query.QueryFilter('family_id', family_id, 'contains')
                ]
            )
            clinician_details = {"name": "unknown", "hospital": "unknown"}
            # The results contain multiple rows for each famliy member.
            # This code just takes the first entry. May need refining.
            try:
                clinician_details['name'] = search_results['rows'][0].get(
                    'consultant_details_full_name_of_responsible_consultant')
            except IndexError as e:
                pass
            try:
                clinician_details['hospital'] = search_results['rows'][0].get(
                    'consultant_details_hospital_of_responsible_consultant')
            except IndexError as e:
                pass

            if not clinician_details['name']:
                clinician_details['name'] = "unknown"
            if not clinician_details["hospital"]:
                clinician_details["hospital"] = "unknown"

        clinician = CaseModel(Clinician, {
            "name": clinician_details['name'],
            "email": "unknown",  # clinicain email not on labkey
            "hospital": clinician_details['hospital']
        }, self.model_objects)
        return clinician

    def get_paricipant_demographics(self, participant_id):
        '''
        Calls labkey to retrieve participant demographics
        :param participant_id: GEL participant ID
        :return: dict containing participant demographics
        '''
        # load in site specific details from config file
        config_dict = load_config.LoadConfig().load()

        if self.case.skip_demographics:
            # don't poll labkey
            participant_demographics = {
                "surname": 'unknown',
                "forename": 'unknown',
                "date_of_birth": '2011/01/01', # unknown but needs to be in date format
                "nhs_num": 'unknown',
                }

        elif not self.case.skip_demographics:
            labkey_server_request = config_dict['labkey_server_request']
            server_context = lk.utils.create_server_context(
                'gmc.genomicsengland.nhs.uk',
                labkey_server_request,
                '/labkey', use_ssl=True)

            participant_demographics = {
                "surname": 'unknown',
                "forename": 'unknown',
                "date_of_birth": '2011/01/01', # unknown but needs to be in date format
                "nhs_num": 'unknown',
                }

            search_results = lk.query.select_rows(
                    server_context=server_context,
                    schema_name='gel_rare_diseases',
                    query_name='participant_identifier',
                    filter_array=[
                                lk.query.QueryFilter(
                                    'participant_id', participant_id, 'contains')
                            ]
            )
            try:
                participant_demographics["surname"] = search_results['rows'][0].get(
                        'surname')
            except IndexError as e:
                pass
            try:
                participant_demographics["forename"] = search_results['rows'][0].get(
                        'forenames')
            except IndexError as e:
                pass
            try:
                participant_demographics["date_of_birth"] = search_results['rows'][0].get(
                        'date_of_birth').split(' ')[0]
            except IndexError as e:
                pass
            try:
                if search_results['rows'][0].get('person_identifier_type').upper() == "NHSNUMBER":
                        participant_demographics["nhs_num"] = search_results['rows'][0].get(
                                'person_identifier')
            except IndexError as e:
                pass

        return participant_demographics

    def get_proband(self):
        """
        Create a case model to handle adding/getting the proband for case.
        """
        participant_id = self.case.json["proband"]
        demographics = self.get_paricipant_demographics(participant_id)
        family = self.case.attribute_managers[Family].case_model
        proband = CaseModel(Proband, {
            "gel_id": participant_id,
            "family": family.entry,
            "nhs_number": demographics['nhs_num'],
            "forename": demographics["forename"],
            "surname": demographics["surname"],
            "date_of_birth": datetime.strptime(demographics["date_of_birth"], "%Y/%m/%d").date(),
            "sex": self.case.proband["sex"],
            "status": 'N' # initialised to not started? (N)
        }, self.model_objects)
        return proband

    def get_relatives(self):
        """
        Creates entries for each relative. Calls labkey to retrieve demograhics
        """
        family_members = self.case.family_members
        relative_list = []
        for family_member in family_members:
            demographics = self.get_paricipant_demographics(family_member['gel_id'])
            proband = self.case.attribute_managers[Proband].case_model
            relative = {
                "gel_id": family_member['gel_id'],
                "relation_to_proband": family_member["relation_to_proband"],
                "affected_status": family_member["affection_status"],
                "proband": proband.entry,
                "nhs_number": demographics["nhs_num"],
                "forename": demographics["forename"],
                "surname":demographics["surname"],
                "date_of_birth": demographics["date_of_birth"],
                "sex": family_member["sex"],
            }
            relative_list.append(relative)
        relatives = ManyCaseModel(Relative,[{
            "gel_id": relative['gel_id'],
            "relation_to_proband": relative["relation_to_proband"],
            "affected_status": relative["affected_status"],
            "proband": relative['proband'],
            "nhs_number": relative["nhs_number"],
            "forename": relative["forename"],
            "surname": relative["surname"],
            "date_of_birth": datetime.strptime(relative["date_of_birth"], "%Y/%m/%d").date(),
            "sex": relative["sex"],
        } for relative in relative_list], self.model_objects)
        return relatives

    def get_family(self):
        """
        Create case model to handle adding/getting family for this case.
        """
        clinician = self.case.attribute_managers[Clinician].case_model
        family = CaseModel(Family, {
            "clinician": clinician.entry,
            "gel_family_id": self.case.json["family_id"]
        }, self.model_objects)
        return family

    def get_phenotypes(self):
        """
        Create a list of CaseModels for phenotypes for this case.
        """
        phenotypes = ManyCaseModel(Phenotype, [
            {"hpo_terms": phenotype["term"],
             "description": "unknown"}
            for phenotype in self.case.proband["hpoTermList"]
            if phenotype["termPresence"] is True
        ], self.model_objects)
        return phenotypes

    def get_family_phenotyes(self):
        # TODO
        family_phenotypes = ManyCaseModel(FamilyPhenotype, [
            {"family": None,
             "phenotype": None}
        ], self.model_objects)

        return family_phenotypes

    def get_panels(self):
        """
        Poll panelApp to fetch information about a panel, then create a
        ManyCaseModel with this information.
        """
        config_dict = load_config.LoadConfig().load()
        panelapp_storage = config_dict['panelapp_storage']
        if self.case.panels:
            for panel in self.case.panels:
                polled = self.case.panel_manager.fetch_panel_response(
                    panelapp_id=panel["panelName"],
                    panel_version=panel["panelVersion"]
                )
                if polled:
                    panel["panelapp_results"] = polled.results
                if not polled:
                    panel_file = os.path.join(panelapp_storage, '{}_{}.json'.format(panel['panelName'],
                                                                                    panel['panelVersion']))
                    if os.path.isfile(panel_file):
                        panel_app_response = json.load(open(panel_file))
                    else:
                        panelapp_poll = PollAPI(
                            "panelapp", "get_panel/{panelapp_id}/?version={v}".format(
                                panelapp_id=panel["panelName"],
                                v=panel["panelVersion"])
                        )
                        with open(panel_file, 'w') as f:
                            json.dump(panelapp_poll.get_json_response(), f)
                        panel_app_response = panelapp_poll.get_json_response()

                    # inform the PanelManager that a new panel has been added
                    polled = self.case.panel_manager.add_panel_response(
                        panelapp_id=panel["panelName"],
                        panel_version=panel["panelVersion"],
                        panelapp_response=panel_app_response["result"]
                    )
                    panel["panelapp_results"] = polled.results

            for panel in self.case.panels:
                panel["panel_name_results"] = self.case.panel_manager.fetch_panel_names(
                    panelapp_id=panel["panelName"]
                    )

            panels = ManyCaseModel(Panel, [{
                "panelapp_id": panel["panelName"],
                "panel_name": panel["panel_name_results"]["SpecificDiseaseName"],
                "disease_group": panel["panel_name_results"]["DiseaseGroup"],
                "disease_subgroup": panel["panel_name_results"]["DiseaseSubGroup"]
            } for panel in self.case.panels], self.model_objects)
        else:
            panels = ManyCaseModel(Panel, [], self.model_objects)
        return panels

    def get_panel_versions(self):
        """
        Add the panel model to description in case.panel then set values
        for the ManyCaseModel.
        """
        panel_models = [
            # get all the panel models from the attribute manager
            case_model.entry
            for case_model
            in self.case.attribute_managers[Panel].case_model.case_models]
        if self.case.panels:
            for panel in self.case.panels:
                # set self.case.panels["model"] to the correct model
                for panel_model in panel_models:
                    if panel["panelName"] == panel_model.panelapp_id:
                        panel["model"] = panel_model

            panel_versions = ManyCaseModel(PanelVersion, [{
                # create the MCM
                "version_number": panel["panelapp_results"]["version"],
                "panel": panel["model"]
            } for panel in self.case.panels], self.model_objects)
        else:
            panel_versions = ManyCaseModel(PanelVersion, [], self.model_objects)
        return panel_versions

    def get_genes(self):
        """
        Create gene objects from the genes from panelapp.
        """
        panels = self.case.panels

        # get the list of genes from the panelapp_result
        gene_list = []
        if panels:
            for panel in panels:
                genes = panel["panelapp_results"]["Genes"]
                gene_list += genes

        for gene in gene_list:
            # Alot of pilot cases just have E for this
            if not gene["EnsembleGeneIds"] or gene["EnsembleGeneIds"] == 'E':
                gene["EnsembleGeneIds"] = None
            else:
                if type(gene['EnsembleGeneIds']) is str:
                    gene['EnsembleGeneIds'] = gene['EnsembleGeneIds']
                else:
                    gene['EnsembleGeneIds'] = gene['EnsembleGeneIds'][0]

        self.case.gene_manager.load_genes()

        for transcript in self.case.transcripts:
            if transcript.gene_ensembl_id and transcript.gene_hgnc_id:
                gene_list.append({
                    'EnsembleGeneIds': transcript.gene_ensembl_id,
                    'GeneSymbol': transcript.gene_hgnc_name,
                    'HGNC_ID': str(transcript.gene_hgnc_id),
                })
                self.case.gene_manager.add_searched(transcript.gene_ensembl_id, str(transcript.gene_hgnc_id))

        for gene in gene_list:
            gene['HGNC_ID'] = None
            if gene['EnsembleGeneIds']:
                polled = self.case.gene_manager.fetch_searched(gene['EnsembleGeneIds'])
                if polled == 'Not_found':
                    gene['HGNC_ID'] = None
                elif not polled:
                    print('Polling {}'.format(gene["EnsembleGeneIds"]))
                    genename_poll = PollAPI(
                        "genenames", "search/{gene}/".format(
                            gene=gene["EnsembleGeneIds"])
                    )
                    genename_response = genename_poll.get_json_response()
                    if genename_response['response']['docs']:
                        hgnc_id = genename_response['response']['docs'][0]['hgnc_id'].split(':')
                        gene['HGNC_ID'] = str(hgnc_id[1])
                        self.case.gene_manager.add_searched(gene["EnsembleGeneIds"], str(hgnc_id[1]))
                    else:
                        self.case.gene_manager.add_searched(gene["EnsembleGeneIds"], 'Not_found')
                else:
                    gene['HGNC_ID'] = polled

        cleaned_gene_list = []
        for gene in gene_list:
            if gene['HGNC_ID']:
                self.case.gene_manager.add_gene(gene)
                new_gene = self.case.gene_manager.fetch_gene(gene)
                cleaned_gene_list.append(new_gene)

        self.case.gene_manager.write_genes()

        genes = ManyCaseModel(Gene, [{
            "ensembl_id": gene["EnsembleGeneIds"],  # TODO: which ID to use?
            "hgnc_name": gene["GeneSymbol"],
            "hgnc_id": gene['HGNC_ID']
        } for gene in cleaned_gene_list if gene["HGNC_ID"]], self.model_objects)
        return genes

    def get_panel_version_genes(self):
        # TODO: implement M2M relationship
        panel_version_genes = ManyCaseModel(PanelVersionGenes, [{
            "panel_version": None,
            "gene": None
        }], self.model_objects)

        return panel_version_genes

    def get_transcripts(self):
        """
        Create a ManyCaseModel for transcripts based on information returned
        from VEP.
        """

        genes = self.case.attribute_managers[Gene].case_model.case_models
        case_transcripts = self.case.transcripts
        print("Getting transcripts for case", self.case.request_id)
        print("Case trancsripts:", case_transcripts)
        # for each transcript, add an FK to the gene with matching ensg ID
        for transcript in case_transcripts:
            # convert canonical to bools:
            transcript.canonical = transcript.transcript_canonical == "YES"
            if not transcript.gene_hgnc_id:
                # if the transcript has no recognised gene associated
                continue  # don't bother checking genes
                print("Skipping transcript: no hgnc")
            transcript.gene_model = None
            for gene in genes:
                print("GENE HGNC", gene.entry.hgnc_id, "\t", transcript.gene_hgnc_id)
                if gene.entry.hgnc_id == transcript.gene_hgnc_id:
                    transcript.gene_model = gene.entry
                    print("Match found")

        transcripts = ManyCaseModel(Transcript, [{
            "gene": transcript.gene_model,
            "name": transcript.transcript_name,
            "canonical_transcript": transcript.canonical,
            "strand": transcript.transcript_strand,
            # add all transcripts except those without associated genes
        } for transcript in case_transcripts if transcript.gene_model],
                                    self.model_objects)

        return transcripts

    def get_ir_family(self):
        """
        Create a CaseModel for the new IRFamily Model to be added to the
        database (unlike before it is impossible that this alreay exists).
        """
        family = self.case.attribute_managers[Family].case_model
        ir_family = CaseModel(InterpretationReportFamily, {
            "participant_family": family.entry,
            "cip": self.case.json["cip"],
            "ir_family_id": self.case.request_id,
            "priority": self.case.json["case_priority"]
        }, self.model_objects)
        return ir_family

    def get_ir_family_panels(self):
        # TODO: implement M2M relationship

        ir_family_panels = ManyCaseModel(InterpretationReportFamilyPanel, [{
            "ir_family": None,
            "panel": None
        }], self.model_objects)

        return ir_family_panels

    def get_ir(self):
        """
        Get json information about an Interpretation Report and create a
        CaseModel from it.
        """
        case_attribute_managers = self.case.attribute_managers
        irf_manager = case_attribute_managers[InterpretationReportFamily]
        ir_family = irf_manager.case_model

        ir = CaseModel(GELInterpretationReport, {
            "ir_family": ir_family.entry,
            "polled_at_datetime": timezone.now(),
            "sha_hash": self.case.json_hash,
            "status": self.case.status["status"],
            "updated": timezone.make_aware(
                datetime.strptime(
                    self.case.status["created_at"][:19],
                    '%Y-%m-%dT%H:%M:%S'
                )),
            "user": self.case.status["user"]
        }, self.model_objects)
        return ir

    def get_variants(self):
        """
        Get the variant information (genetic position) for the variants in this
        case and return a matching ManyCaseModel with model_type = Variant.
        """
        tool_models = [
            case_model.entry
            for case_model in self.case.attribute_managers[ToolOrAssemblyVersion].case_model.case_models]

        genome_assembly = None

        for tool in tool_models:
            if tool.tool_name == 'genome_build':
                genome_assembly = tool

        variants_list = []
        # loop through all variants and check that they have a case_variant
        # (all variants Tier1 and Tier2, Tier3 variants do not
        for variant in self.case.json_variants:
            if variant["case_variant"]:
                if variant['dbSNPid']:
                    if not re.match('rs\d+', str(variant['dbSNPid'])):
                        variant['dbSNPid'] = None
                tiered_variant = {
                    "genome_assembly": genome_assembly,
                    "alternate": variant["case_variant"].alt,
                    "chromosome": variant["case_variant"].chromosome,
                    "db_snp_id": variant["dbSNPid"],
                    "reference": variant["case_variant"].ref,
                    "position": variant["case_variant"].position,
                }
                variants_list.append(tiered_variant)

        # loop through all variants and check that they have a case_variant (all should?)
        for interpreted_genome in self.case.json["interpreted_genome"]:
            for variant in interpreted_genome["interpreted_genome_data"]["reportedVariants"]:
                if variant["case_variant"]:
                    cip_variant = {
                        "genome_assembly": genome_assembly,
                        "alternate": variant["case_variant"].alt,
                        "chromosome": variant["case_variant"].chromosome,
                        "db_snp_id": variant["dbSNPid"],
                        "reference": variant["case_variant"].ref,
                        "position": variant["case_variant"].position,
                    }
                    variants_list.append(cip_variant)

        for variant in variants_list:
            self.case.variant_manager.add_variant(variant)

        cleaned_variant_list = []
        for variant in variants_list:
            cleaned_variant_list.append(self.case.variant_manager.fetch_variant(variant))

        # set and return the MCM
        variants = ManyCaseModel(Variant, [{
            "genome_assembly": genome_assembly,
            "alternate": variant["alternate"],
            "chromosome": variant["chromosome"],
            "db_snp_id": variant["db_snp_id"],
            "reference": variant["reference"],
            "position": variant["position"],
        } for variant in cleaned_variant_list],
        self.model_objects)

        return variants

    def get_transcript_variants(self):
        """
        Get all variant transcripts. This is essentialy a 'through' table for
        the M2M relationship between Variant and Transcript, but with extra
        information.
        """
        # get all Transcript and Variant entries
        case_attribute_managers = self.case.attribute_managers
        transcript_manager = case_attribute_managers[Transcript].case_model
        transcript_entries = [transcript.entry
                       for transcript in transcript_manager.case_models]
        variant_manager = case_attribute_managers[Variant].case_model
        variant_entries = [variant.entry
                           for variant in variant_manager.case_models]

        # for each CaseTranscript (which contains necessary info):
        for case_transcript in self.case.transcripts:
            # get information to hook up transcripts with variants
            case_id = case_transcript.case_id
            variant_id = case_transcript.variant_count
            for variant in self.case.variants:
                if (
                    case_id == variant.case_id and
                    variant_id == variant.variant_count
                ):
                    case_variant = variant
                    break

            # name to hook up CaseTranscript with Transcript
            transcript_name = case_transcript.transcript_name

            # add the corresponding Variant entry
            for variant_entry in variant_entries:
                # find the matching variant entry
                if (
                    variant_entry.chromosome == case_variant.chromosome and
                    variant_entry.position == case_variant.position and
                    variant_entry.reference == case_variant.ref and
                    variant_entry.alternate == case_variant.alt
                ):
                    # add match to the case_transcript
                    case_transcript.variant_entry = variant_entry

            # add the corresponding Transcript entry
            for transcript_entry in transcript_entries:
                found = False
                if transcript_entry.name == transcript_name:
                    case_transcript.transcript_entry = transcript_entry
                    found = True
                    break
                if not found:
                    # we don't make entries for tx with no Gene
                    case_transcript.transcript_entry = None

        # use the updated CaseTranscript instances to create an MCM
        transcript_variants = ManyCaseModel(TranscriptVariant, [{
            "transcript": transcript.transcript_entry,
            "variant": transcript.variant_entry,
            "af_max": transcript.transcript_variant_af_max,
            "hgvs_c": transcript.transcript_variant_hgvs_c,
            "hgvs_p": transcript.transcript_variant_hgvs_p,
            "hgvs_g": transcript.transcript_variant_hgvs_g,
            "sift": transcript.variant_sift,
            "polyphen": transcript.variant_polyphen,
        } for transcript in self.case.transcripts
            if transcript.transcript_entry], self.model_objects)

        return transcript_variants

    def get_proband_variants(self):
        """
        Get proband variant information from VEP and the JSON and create MCM.
        """
        ir_manager = self.case.attribute_managers[GELInterpretationReport]

        # match up created variants to corresponding dict in json_variants:
        variant_entries = [
            variant.entry
            for variant
            in self.case.attribute_managers[Variant].case_model.case_models
        ]
        # tiered variants
        tiered_proband_variants = []
        for json_variant in self.case.json_variants:
            # some json_variants won't have an entry (T3), so:
            json_variant["variant_entry"] = None
            # for those that do, fetch from list of entries:
            # variant in json matches variant entry
            for variant in variant_entries:
                if (
                        json_variant["position"] == variant.position and
                        json_variant["reference"] == variant.reference and
                        json_variant["alternate"] == variant.alternate
                ):
                    # variant in json matches variant entry
                    json_variant["variant_entry"] = variant

        for variant in self.case.json_variants:
            if variant["variant_entry"]:
                tiered_proband_variant = {
                    "max_tier": variant["max_tier"],
                    "variant": variant["variant_entry"],
                }
                tiered_proband_variants.append(tiered_proband_variant)

        # cip flagged variants
        cip_proband_variants = []
        for interpreted_genome in self.case.json["interpreted_genome"]:
            for json_variant in interpreted_genome["interpreted_genome_data"]["reportedVariants"]:
                # all CIP flagged variants should have a variant entry.
                json_variant["variant_entry"] = None
                # variant in json matches variant entry
                for variant in variant_entries:
                    if (
                            json_variant["position"] == variant.position and
                            json_variant["reference"] == variant.reference and
                            json_variant["alternate"] == variant.alternate
                    ):
                        # variant in json matches variant entry
                        json_variant["variant_entry"] = variant

            for variant in interpreted_genome["interpreted_genome_data"]["reportedVariants"]:
                if variant["variant_entry"]:
                    cip_proband_variant = {
                        "max_tier": 0,  # CIP flagged variants assigned tier 0
                        "variant": variant["variant_entry"],
                    }
                    cip_proband_variants.append(cip_proband_variant)
        # remove CIP tiered variants which are in cip variants
        tiered_and_cip_proband_variants = cip_proband_variants + tiered_proband_variants
        seen_variants = {}
        for cip_variant in cip_proband_variants:
            tiered_and_cip_proband_variants.append(cip_variant)
            seen_variants[cip_variant['variant']] = 1

        for variant in tiered_proband_variants:
            if variant['variant'] not in seen_variants:
                tiered_and_cip_proband_variants.append(variant)

        proband_variants = ManyCaseModel(ProbandVariant, [{
            "interpretation_report": ir_manager.case_model.entry,
            "max_tier": variant["max_tier"],
            "variant": variant["variant"],
            "somatic": False
            # only adding T1/2 and CIP flagged
        } for variant in tiered_and_cip_proband_variants],
                                         self.model_objects)

        return proband_variants

    def get_report_events(self):

        """
        Get all the report events for each case from the json and populate an
        MCM with this information.
        """
        # get gene and panel entries
        genes = [gene.entry for gene
                 in self.case.attribute_managers[Gene].case_model.case_models]
        panel_versions = [panel_version.entry for panel_version
                          in self.case.attribute_managers[PanelVersion].case_model.case_models]
        phenotypes = [phenotype.entry for phenotype
                      in self.case.attribute_managers[Phenotype].case_model.case_models]
        proband_variants = [proband_variant.entry for proband_variant
                            in self.case.attribute_managers[ProbandVariant].case_model.case_models]

        # get list of dicts of each report event
        json_report_events = []

        # modify report event dicts with gene and panel info
        for variant in self.case.json_variants:
            # exlude Tier 3s:
            if variant["max_tier"] < 3:

                # go through each RE in the variant
                for report_event in variant["reportEvents"]:

                    # set the Gene entry
                    found = False
                    gene_found = False
                    re_genomic_info = report_event.get("genomicFeature", None)
                    if re_genomic_info:
                        re_gene_ensembl_id = re_genomic_info.get("ensemblId", None)
                        for gene in genes:
                            if re_gene_ensembl_id == gene.ensembl_id:
                                report_event["gene_entry"] = gene
                                gene_found = True
                                break

                        if not gene_found:
                            # re-attempt with HGNC
                            re_gene_hgnc = re_genomic_info.get("HGNC", None)
                            for gene in genes:
                                if re_gene_hgnc == gene.hgnc_name:
                                    report_event["gene_entry"] = gene
                                    gene_found = True

                    if not gene_found:
                        report_event["gene_entry"] = None

                    # set the Panel entry
                    panel_found = False
                    re_panel_name = report_event.get("panelName", None)
                    re_panel_version = report_event.get("panelVersion", None)

                    for panel_version in panel_versions:
                        if (re_panel_name == panel_version.panel.panel_name and
                                re_panel_version == panel_version.version_number
                        ):
                            report_event["panel_version_entry"] = panel_version
                            panel_found = True
                            break
                    if not panel_found:
                        report_event["panel_version_entry"] = None

                    if panel_found:
                        try:
                            panel = report_event["panel_version_entry"].panel
                            panelapp_id = panel.panelapp_id
                            # coverages is a dict of dicts: (1) access panel using hash
                            panel_coverages = self.case.json_request_data["genePanelsCoverage"]
                            panel_coverage = panel_coverages.get(panelapp_id, None)
                            # (2) access coverage info using gene hgnc

                            re_gene_hgnc = report_event["genomicFeature"]["HGNC"]
                            re_gene_coverage = panel_coverage[re_gene_hgnc]
                            # coverage info lists samples, get correct sample
                            proband_sample = self.case.proband["samples"][0]
                            proband_sample_avg = proband_sample + "_avg"
                            gene_avg_coverage = re_gene_coverage[proband_sample_avg]
                            report_event["gene_coverage"] = gene_avg_coverage
                        except KeyError as e:
                            report_event["gene_coverage"] = None

                    # set the ProbandVariant entry
                    proband_variant_found = False
                    for proband_variant in proband_variants:
                        if proband_variant.variant == variant["variant_entry"]:
                            report_event["proband_variant_entry"] = proband_variant
                            proband_variant_found = True
                            break
                    if not proband_variant_found:
                        report_event["proband_variant_entry"] = None

                    try:
                        report_event_tier = int(report_event["tier"][-1:])
                    except:
                        report_event_tier = None

                    report_event_id = report_event.get("reportEventId", None)
                    if report_event_id and report_event['proband_variant_entry']:
                        json_report_events.append({
                            "coverage": report_event.get("gene_coverage", None),
                            "gene": report_event["gene_entry"],
                            "mode_of_inheritance": report_event.get("modeOfInheritance", None),
                            "panel": report_event["panel_version_entry"],
                            "penetrance": report_event.get("penetrance", None),
                            "proband_variant": report_event["proband_variant_entry"],
                            "re_id": report_event_id,
                            "tier": report_event_tier
                        })

        # repeat for CIP flagged variants:
        for interpreted_genome in self.case.json["interpreted_genome"]:
            for variant in interpreted_genome["interpreted_genome_data"]["reportedVariants"]:
                # go through each RE in the variant
                for report_event in variant["reportEvents"]:
                    # set the Gene entry
                    found = False
                    gene_found = False
                    re_genomic_info = report_event.get("genomicFeature", None)
                    if re_genomic_info:
                        re_gene_ensembl_id = re_genomic_info.get("ensemblId", None)
                        for gene in genes:
                            if re_gene_ensembl_id == gene.ensembl_id:
                                report_event["gene_entry"] = gene
                                gene_found = True
                                break

                        if not gene_found:
                            # re-attempt with HGNC
                            re_gene_hgnc = re_genomic_info.get("HGNC", None)
                            for gene in genes:
                                if re_gene_hgnc == gene.hgnc_name:
                                    report_event["gene_entry"] = gene
                                    gene_found = True

                    if not gene_found:
                        report_event["gene_entry"] = None

                    # set the Panel entry
                    panel_found = False
                    re_panel_name = report_event.get("panelName", None)
                    re_panel_version = report_event.get("panelVersion", None)

                    for panel_version in panel_versions:
                        if (re_panel_name == panel_version.panel.panel_name and
                                re_panel_version == panel_version.version_number
                        ):
                            report_event["panel_version_entry"] = panel_version
                            panel_found = True
                            break
                    if not panel_found:
                        report_event["panel_version_entry"] = None

                    if panel_found:
                        try:
                            panel = report_event["panel_version_entry"].panel
                            panelapp_id = panel.panelapp_id
                            # coverages is a dict of dicts: (1) access panel using hash
                            panel_coverages = self.case.json_request_data["genePanelsCoverage"]
                            panel_coverage = panel_coverages.get(panelapp_id, None)
                            # (2) access coverage info using gene hgnc

                            re_gene_hgnc = report_event["genomicFeature"]["HGNC"]
                            re_gene_coverage = panel_coverage[re_gene_hgnc]
                            # coverage info lists samples, get correct sample
                            proband_sample = self.case.proband["samples"][0]
                            proband_sample_avg = proband_sample + "_avg"
                            gene_avg_coverage = re_gene_coverage[proband_sample_avg]
                            report_event["gene_coverage"] = gene_avg_coverage
                        except KeyError as e:
                            report_event["gene_coverage"] = None
                    else:
                        report_event["gene_coverage"] = None

                    # set the ProbandVariant entry
                    proband_variant_found = False
                    for proband_variant in proband_variants:
                        if proband_variant.variant == variant["variant_entry"]:
                            report_event["proband_variant_entry"] = proband_variant
                            proband_variant_found = True
                            break
                    if not proband_variant_found:
                        report_event["proband_variant_entry"] = None

                    try:
                        report_event_tier = int(report_event["tier"][-1:])
                    except:
                        report_event_tier = None

                    report_event_id = report_event.get("reportEventId", None)
                    if report_event_id and report_event['proband_variant_entry']:
                        json_report_events.append({
                            "coverage": report_event.get("gene_coverage", None),
                            "gene": report_event["gene_entry"],
                            "mode_of_inheritance": report_event.get("modeOfInheritance", None),
                            "panel": report_event["panel_version_entry"],
                            "penetrance": report_event.get('penetrance', None),
                            "proband_variant": report_event["proband_variant_entry"],
                            "re_id": report_event_id,
                            "tier": report_event_tier
                        })

        report_events = ManyCaseModel(ReportEvent, json_report_events, self.model_objects)
        return report_events

    def get_proband_transcript_variants(self):
        """
        Get the ProbandTranscriptVariants associated with this case and return
        a MCM containing them.
        """

        # get the transcript_variants

        # associat a proband_variant with a transcript
        proband_variants = [proband_variant.entry for proband_variant
                            in self.case.attribute_managers[ProbandVariant].case_model.case_models]

        for transcript in self.case.transcripts:
            for proband_variant in proband_variants:
                if proband_variant.variant == transcript.variant_entry:
                    transcript.proband_variant_entry = proband_variant


        proband_transcript_variants = ManyCaseModel(ProbandTranscriptVariant, [{
            "transcript": transcript.transcript_entry,
            "proband_variant": transcript.proband_variant_entry,
            # default is true if assoc. tx is canonical
            "selected": transcript.transcript_entry.canonical_transcript,
            "effect": transcript.proband_transcript_variant_effect
        } for transcript
            in self.case.transcripts
            if transcript.transcript_entry
            and transcript.proband_variant_entry], self.model_objects)

        return proband_transcript_variants

    def get_tool_and_assembly_versions(self):
        '''
        Create tool and assembly entries for the case
        '''
        tools_and_assemblies = ManyCaseModel(ToolOrAssemblyVersion, [{
            "tool_name": tool,
            "version_number": version
        }for tool, version in self.case.tools_and_versions.items()],
        self.model_objects)

        return tools_and_assemblies


class CaseModel(object):
    """
    A handler for an instance of a model that belongs to a case. Holds an
    instance of a model (pre-creation or post-creation) and whether it
    requires creation in the database.
    """
    def __init__(self, model_type, model_attributes, model_objects):
        self.model_type = model_type
        self.model_attributes = model_attributes
        self.model_objects = model_objects
        self.entry = self.check_found_in_db(self.model_objects)

    def check_found_in_db(self, queryset):
        """
        Queries the database for a model of the given type with the given
        attributes. Returns True if found, False if not.
        """

        if self.model_type == Clinician:
            entry = [db_obj for db_obj in queryset
                     if db_obj.name == self.model_attributes["name"]
                     and db_obj.hospital == self.model_attributes["hospital"]
                     and db_obj.email == self.model_attributes["email"]]
        elif self.model_type == Proband:
            entry = [db_obj for db_obj in queryset
                     if db_obj.gel_id == str(self.model_attributes["gel_id"])]
        elif self.model_type == Family:
            entry = [db_obj for db_obj in queryset
                     if db_obj.gel_family_id == str(self.model_attributes["gel_family_id"])]
        elif self.model_type == Relative:
            entry = [db_obj for db_obj in queryset
                     if str(db_obj.gel_id) == str(self.model_attributes["gel_id"])]
        elif self.model_type == Phenotype:
            entry = [db_obj for db_obj in queryset
                     if db_obj.hpo_terms == self.model_attributes["hpo_terms"]]
        elif self.model_type == FamilyPhenotype:
                     pass
        elif self.model_type == InterpretationReportFamily:
            entry =[db_obj for db_obj in queryset
                    if db_obj.ir_family_id == self.model_attributes["ir_family_id"]]
        elif self.model_type == Panel:
            entry = [db_obj for db_obj in queryset
                     if db_obj.panelapp_id == self.model_attributes["panelapp_id"]]
        elif self.model_type == PanelVersion:
            entry = [db_obj for db_obj in queryset
                     if db_obj.panel == self.model_attributes["panel"]
                     and db_obj.version_number == self.model_attributes["version_number"]]
        elif self.model_type == InterpretationReportFamilyPanel:
                     pass
        elif self.model_type == Gene:
            entry = [db_obj for db_obj in queryset
                     if db_obj.ensembl_id == self.model_attributes["ensembl_id"]
                     and db_obj.hgnc_name == self.model_attributes["hgnc_name"]]
        elif self.model_type == PanelVersionGene:
                     pass
        elif self.model_type == Transcript:
            entry = [db_obj for db_obj in queryset
                     if db_obj.name == self.model_attributes["name"]]
        elif self.model_type == GELInterpretationReport:
            entry = [db_obj for db_obj in queryset
                     if db_obj.sha_hash == self.model_attributes["sha_hash"]]
        elif self.model_type == Variant:
            entry = [db_obj for db_obj in queryset
                     if db_obj.chromosome == self.model_attributes["chromosome"]
                     and db_obj.position == self.model_attributes["position"]
                     and db_obj.reference == self.model_attributes["reference"]
                     and db_obj.alternate == self.model_attributes["alternate"]
                     and db_obj.genome_assembly == self.model_attributes["genome_assembly"]]
        elif self.model_type == TranscriptVariant:
            entry = [db_obj for db_obj in queryset
                     if db_obj.transcript == self.model_attributes["transcript"]
                     and db_obj.variant == self.model_attributes["variant"]]
        elif self.model_type == ProbandVariant:
            entry = [db_obj for db_obj in queryset
                     if db_obj.variant == self.model_attributes["variant"]
                     and db_obj.max_tier == self.model_attributes["max_tier"]
                     and db_obj.interpretation_report == self.model_attributes["interpretation_report"]]
        elif self.model_type == ProbandTranscriptVariant:
            entry = [db_obj for db_obj in queryset
                     if db_obj.transcript == self.model_attributes["transcript"]
                     and db_obj.proband_variant == self.model_attributes["proband_variant"]]
        elif self.model_type == ReportEvent:
            entry = [db_obj for db_obj in queryset
                     if db_obj.re_id == self.model_attributes["re_id"]
                     and db_obj.proband_variant == self.model_attributes["proband_variant"]]
        elif self.model_type == ToolOrAssemblyVersion:
            entry = [db_obj for db_obj in queryset
                     if db_obj.tool_name == self.model_attributes["tool_name"]
                     and db_obj.version_number == self.model_attributes["version_number"]]

        if len(entry) == 1:
            entry = entry[0]
            # also need to set self.entry here as it may not be called in init
            self.entry = entry
        elif len(entry) == 0:
            entry = False
            self.entry = entry
        else:
            raise ValueError("Multiple entries found for same object.")

        return entry


class ManyCaseModel(object):
    """
    Class to deal with situations where we need to extend on CaseModel to allow
    for ManyToMany field population, as the bulk update must be handled using
    'through' tables.
    """
    def __init__(self, model_type, model_attributes_list, model_objects):
        self.model_type = model_type
        self.model_attributes_list = model_attributes_list
        self.model_objects = model_objects

        self.case_models = [
            CaseModel(model_type, model_attributes, model_objects)
            for model_attributes in model_attributes_list
        ]
        self.entries = self.get_entry_list()

    def get_entry_list(self):
        entries = []
        for case_model in self.case_models:
            entries.append(case_model.entry)
        return entries
