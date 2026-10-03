import builtins
from collections import OrderedDict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cookiecutter.generate import generate_context


real_open = builtins.open


def ascii_default_open(file, mode="r", *args, **kwargs):
    if "b" not in mode and "encoding" not in kwargs:
        kwargs["encoding"] = "ascii"
    return real_open(file, mode, *args, **kwargs)


builtins.open = ascii_default_open
try:
    observed = generate_context(ROOT / "tests" / "non_ascii.json")
finally:
    builtins.open = real_open

expected = OrderedDict(
    [("non_ascii", OrderedDict([("full_name", "éèà")]))]
)
assert observed == expected, (observed, expected)
