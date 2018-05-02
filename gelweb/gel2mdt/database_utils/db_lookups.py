"""Copyright (c) 2018 Great Ormond Street Hospital for Children NHS Foundation
Trust & Birmingham Women's and Children's NHS Foundation Trust

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import hashlib
import json

from django.forms.models import model_to_dict

from gel2mdt.models import *

class LookupManager(object):
    """
    Class to manage efficiently manage database lookups during an iteration
    of the MCA. This parent class stores the lookup fields we use to check
    existence in the database, and either pull from the database or determine
    that a new model should be entered.
    """
    def __init__(self):
        self.fields_to_hash = {
            Clinician: ["name","hospital","email"],
            Phenotype: ["hpo_terms"],
            Family: ["gel_family_id"],
            Gene: ["hgnc_id"],
            Panel: ["panelapp_id"],
            PanelVersion: ["panel"],
            ToolOrAssemblyVersion: ["tool_name", "version_number"],
            InterpretationReportFamily: ["ir_family_id"],
            InterpretationReportFamilyPanel: ["ir_family", "panel"],
            GELInterpretationReport: ["sha_hash"],
            Proband: ["gel_id"],
            Relative: ["gel_id", "proband"],
            Variant: ["chromosome","position","reference","alternate","genome_assembly"],
            Transcript: ["name"],
            TranscriptVariant: ["transcript", "variant"],
            ProbandVariant: ["variant", "max_tier", "interpretation_report"],
            ProbandTranscriptVariant: ["transcript", "proband_variant"],
            ReportEvent: ["re_id", "proband_variant"],
        }

    def get_dict_hash(self, values):
        """
        Takes a dictionary of values from a model instance, sorts them using a
        json sort, then returns a hexadecimal digest of the SHA512 hash of a
        unicode representation of that sorted dictionary.
        """
        hash_buffer = json.dumps(values, sort_keys=True).encode('utf-8')
        hash_hex = hashlib.sha512(hash_buffer)
        hash_digest = hash_hex.hexdigest()
        return hash_digest


class HashMap(LookupManager):
    """
    Creates a hashmap of all of the model values currently present in the
    database, based on the fields defined in self.fields_to_hash. This can be
    leveraged to fetch items out of the database with 1 query and no iteration
    through a list.
    """
    def __init__(self, model_type, model_object_list):
        self.model_type = model_type
        self.model_object_list = model_object_list

        super().__init__()

    def get_hashmap(self):
        """
        Create and return a hashmap as a dictionary with the key being a hash,
        the value being the model isntance for each model of a particular type.
        """
        hashmap = {}

        fields = self.fields_to_hash[self.model_type]
        model_object_values = list(self.model_object_list.values(*fields))

        # convert all attributes to a string repr, to allow json serialisation
        # of django model types
        for field_attributes in model_object_values:
            for field, attribute in field_attributes.items():
                field_attributes[field] = str(attribute)

        # create list of tuples of (hash, values_list) for each model entry
        # using the tuple, then zip with the model_object_list and create a
        # hashmap, ensuring that the valuesin values_list match the models
        # actual values
        hash_tuple_list = [(self.get_dict_hash(values), values)
                           for values in model_object_values]
        for hash_tuple, model in zip(hash_tuple_list, self.model_object_list):
            model_fields = model_to_dict(model, fields=fields)
            for field in fields:
                if hash_tuple[1][field] != str(model_fields[field]):
                    raise AttributeError(
                        "Field values do not match for hashmap! {hash} and {model}.".format(
                            hash=hash_tuple[1][field],
                            model=model_fields[field]
                        )
                    )

            if not hashmap.get(hash_tuple[0], None):
                # mapping not yet made, add the hash mapping
                hashmap[hash_tuple[0]] = model
            else:
                # Multiple objects with same attributes, BARF
                print(hashmap[hash_tuple[0]])
                raise ValueError("Multiple entries found for same object.")

        return hashmap


class SingleHash(LookupManager):
    """
    Creates a single hash of a dictionary of desired fields defined in the
    parent LookupManager class. This hash can be used in conjuction with the
    hashmap from the sibling HashMap class, which should be defined per model
    type (as apposed to SingleHash, which should be defined per model type
    instance.
    """
    def __init__(self, model_type, model_attributes):
        self.model_type = model_type
        self.model_attributes = model_attributes

        super().__init__()

    def get_hash(self):
        """
        Create and return the hash of a sorted dictionary of one model's values,
        as in HashMap but for one model only.
        """
        fields = self.fields_to_hash[self.model_type]

        attribute_dict = {
            field: str(self.model_attributes[field])
            for field in fields
        }
        print(attribute_dict)

        values_hash = self.get_dict_hash(attribute_dict)
        print(values_hash)

        return values_hash

