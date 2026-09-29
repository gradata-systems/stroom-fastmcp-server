"""Startup checks in main.py: TLS is required, keys are long enough, and the settings parse as the chart sets them."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import Settings

ROOT = Path(__file__).resolve().parents[1]
BASE = {
    'STROOM_MCP_STROOM_URL': 'https://stroom.invalid',
    'STROOM_MCP_OIDC_ISSUER_URL': 'https://keycloak.invalid/realms/ci',
    'STROOM_MCP_OIDC_AUDIENCE': 'stroom-mcp',
    'STROOM_MCP_PUBLIC_BASE_URL': 'https://stroom-mcp.example.com',
    'STROOM_MCP_DEV_NO_AUTH': 'false',
    'STROOM_MCP_STROOM_API_KEY': '',
}


def start(**env) -> subprocess.CompletedProcess:
    """Import main (which builds the server but doesn't serve) with only these STROOM_MCP_ settings."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith('STROOM_MCP_')}
    return subprocess.run([sys.executable, '-c', 'import main'], cwd=ROOT, env={**clean, **BASE, **env},
                          capture_output=True, text=True, timeout=60)


def test_refuses_to_start_without_tls():
    result = start()
    assert result.returncode != 0
    assert 'TLS is required' in result.stderr


def test_plain_http_only_on_loopback():
    assert start(STROOM_MCP_HOST='127.0.0.1', STROOM_MCP_PUBLIC_BASE_URL='http://127.0.0.1:8765').returncode == 0
    assert 'TLS is required' in start(STROOM_MCP_HOST='0.0.0.0').stderr


def test_starts_when_tls_is_terminated_upstream():
    assert start(STROOM_MCP_TLS_TERMINATED_UPSTREAM='true').returncode == 0


def test_certificate_needs_its_key():
    result = start(STROOM_MCP_TLS_CERTFILE='/tls/tls.crt')
    assert 'Set both' in result.stderr


def test_public_url_must_be_https():
    result = start(STROOM_MCP_TLS_TERMINATED_UPSTREAM='true', STROOM_MCP_PUBLIC_BASE_URL='http://stroom-mcp')
    assert 'must be an https:// URL' in result.stderr


def test_request_state_keys_must_be_long():
    result = start(STROOM_MCP_TLS_TERMINATED_UPSTREAM='true', STROOM_MCP_REQUEST_STATE_KEYS='short')
    assert 'at least 32 characters' in result.stderr


def test_shared_request_state_keys_start():
    keys = 'a' * 32 + ',' + 'b' * 40
    assert start(STROOM_MCP_TLS_TERMINATED_UPSTREAM='true', STROOM_MCP_REQUEST_STATE_KEYS=keys).returncode == 0


@pytest.mark.parametrize('value, expected', [('k1', ['k1']), ('k1, k2,', ['k1', 'k2']), ('', [])])
def test_request_state_keys_are_comma_separated(monkeypatch, value, expected):
    monkeypatch.setenv('STROOM_MCP_REQUEST_STATE_KEYS', value)
    keys = Settings(stroom_url='https://stroom.invalid').request_state_keys
    assert [k.get_secret_value() for k in keys] == expected

