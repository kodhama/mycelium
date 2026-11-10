"""No file under src/ but src/mycelium/pane.py invokes herdr (KTD19; hub plan U4).

The check reads the source tree: a module that names the herdr command, as
`"herdr"` or a command line such as `"herdr pane list"`, is calling it or
building a call. Docstrings, comments and messages may mention herdr freely.
"""

import ast
import re
from pathlib import Path

SOURCE = Path(__file__).parent.parent / "src"
PANE = SOURCE / "mycelium" / "pane.py"
# herdr 0.9.3's command groups, as a command line would start.
COMMAND_LINE = re.compile(r"herdr (pane|agent|tab|workspace|server|session)\b")


def names_herdr(path: Path) -> bool:
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and (node.value == "herdr" or COMMAND_LINE.match(node.value))
        for node in ast.walk(tree)
    )


def test_the_check_finds_the_command_where_it_is():
    assert names_herdr(PANE)


def test_the_check_finds_a_command_line_string(tmp_path):
    module = tmp_path / "module.py"
    module.write_text('"""herdr is mentioned here, which is fine."""\nRUN = "herdr pane list"\n')
    assert names_herdr(module)
    module.write_text('"""herdr is mentioned here."""\nSAYS = "herdr could not be read"\n')
    assert not names_herdr(module)


def test_no_module_under_src_but_pane_invokes_herdr():
    modules = sorted(SOURCE.rglob("*.py"))
    assert len(modules) > 10, "the scan must see the source tree"
    offenders = [
        str(path.relative_to(SOURCE)) for path in modules if path != PANE and names_herdr(path)
    ]
    assert offenders == []
