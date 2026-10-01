"""The server's version, as released: the one in pyproject.toml, which each release sets.

The image installs the dependencies but not the project itself, so there is no package metadata to ask;
pyproject.toml is copied in beside the code instead.
"""
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / 'pyproject.toml'


def server_version(pyproject: Path = PYPROJECT) -> str:
    try:
        return tomllib.loads(pyproject.read_text(encoding='utf-8'))['project']['version']
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return 'unknown'


SERVER_VERSION = server_version()
