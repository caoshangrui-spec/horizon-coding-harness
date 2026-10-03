from collections import OrderedDict
import json
from pathlib import Path


def generate_context(context_file):
    context = OrderedDict()
    with open(context_file) as file_handle:
        values = json.load(file_handle, object_pairs_hook=OrderedDict)
    context[Path(context_file).stem] = values
    return context
