import ast
from pathlib import Path


def test_domain_does_not_import_execution_or_infrastructure():
    root = Path(__file__).resolve().parents[2] / "src/horizon/domain"
    forbidden = (
        "horizon.adapters",
        "horizon.application",
        "horizon.interfaces",
        "minisweagent",
        "swerex",
        "litellm",
        "subprocess",
        "sqlite3",
    )
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(item.name for item in node.names)
            if isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(name.startswith(forbidden) for name in imports), path
