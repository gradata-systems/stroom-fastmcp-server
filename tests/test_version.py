"""The server says which version it is: in serverInfo and on /healthz, from pyproject.toml."""
import json
import tomllib
from pathlib import Path

from tests.test_startup import start
from utils.version import server_version

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']

PROBE = """
import asyncio, json
from fastmcp import Client
from starlette.testclient import TestClient
import main

async def server_info(mode):
    async with Client(main.mcp, mode=mode) as client:
        return client.server_info.version

print(json.dumps({'healthz': TestClient(main.mcp.http_app()).get('/healthz').text,
                  # The initialize handshake (VS Code today) and the newer server/discover one.
                  'server_info': [asyncio.run(server_info(mode)) for mode in ('legacy', 'auto')]}))
"""


def test_version_comes_from_pyproject(tmp_path):
    assert server_version() == VERSION
    assert server_version(tmp_path / 'missing.toml') == 'unknown'


def test_server_info_and_healthz_report_it():
    result = start(STROOM_MCP_HOST='127.0.0.1', STROOM_MCP_PUBLIC_BASE_URL='http://127.0.0.1:8765', code=PROBE)
    assert result.returncode == 0, result.stderr[-2000:]
    reported = json.loads(result.stdout.strip().splitlines()[-1])
    assert reported == {'healthz': f'ok {VERSION}', 'server_info': [VERSION, VERSION]}
