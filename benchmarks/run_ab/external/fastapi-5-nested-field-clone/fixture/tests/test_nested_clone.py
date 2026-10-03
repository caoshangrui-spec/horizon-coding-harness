from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.utils import ModelField, ModelType, create_cloned_field


leaf = ModelField("username")
nested = ModelField("model_b", ModelType({"username": leaf}))
root = ModelField("model_a", ModelType({"model_b": nested}))

cloned = create_cloned_field(root)
cloned_nested = cloned.type_.__fields__["model_b"]
cloned_leaf = cloned_nested.type_.__fields__["username"]

assert cloned is not root
assert cloned_nested is not nested
assert cloned_leaf is not leaf

leaf.name = "mutated"
assert cloned_leaf.name == "username"
