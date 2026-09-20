import ast
from pathlib import Path


def test_source_modules_expose_real_module_docstrings():
    package = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "voucher_management"
    )
    missing = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not ast.get_docstring(tree):
            missing.append(path.relative_to(package).as_posix())

    assert missing == []
