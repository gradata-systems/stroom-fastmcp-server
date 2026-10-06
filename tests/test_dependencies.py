"""What the server imports at run time is installed in its image (uv sync --no-dev). saxonche was a dev dependency,
and build_translation_xslt failed in a deployed image with "No module named 'saxonche'"."""
import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
RUNTIME = ['main.py', 'main_tools.py', 'config.py', 'security', 'tools', 'utils']
# Import names that differ from the distribution's name.
DISTRIBUTION = {'yaml': 'pyyaml', 'pydantic_settings': 'pydantic-settings', 'mcp': 'fastmcp', 'starlette': 'fastmcp',
                'uvicorn': 'fastmcp', 'authlib': 'fastmcp', 'jwt': 'fastmcp', 'httpx2': 'fastmcp', 'mcp_types': 'fastmcp'}


def _name(requirement: str) -> str:
    return re.split(r'[<>=!~\[ ;]', requirement, maxsplit=1)[0].strip().lower()


def test_runtime_code_imports_only_runtime_dependencies():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    runtime = {_name(r) for r in project['project']['dependencies']}
    dev = {_name(r) for group in project.get('dependency-groups', {}).values() for r in group if isinstance(r, str)}
    local = {p.stem for p in ROOT.iterdir()} | {'security', 'tools', 'utils'}
    files = [f for entry in RUNTIME for f in ([ROOT / entry] if entry.endswith('.py') else (ROOT / entry).rglob('*.py'))]
    wrong = []
    for file in files:
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else [])
            for name in names:
                top = name.split('.')[0]
                if top in sys.stdlib_module_names or top in local:
                    continue
                dist = DISTRIBUTION.get(top, top.replace('_', '-')).lower()
                if dist not in runtime:
                    wrong.append(f"{file.relative_to(ROOT)} imports {top}" + (" (a dev dependency)" if dist in dev else ""))
    assert not wrong, sorted(set(wrong))
