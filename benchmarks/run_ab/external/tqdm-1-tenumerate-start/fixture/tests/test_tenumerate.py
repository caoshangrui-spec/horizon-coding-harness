from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tqdm.contrib import tenumerate


values = ["a", "b", "c"]
assert list(tenumerate(values)) == list(enumerate(values))
assert list(tenumerate(values, 42)) == list(enumerate(values, 42))
