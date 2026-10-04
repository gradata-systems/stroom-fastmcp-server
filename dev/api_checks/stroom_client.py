"""Minimal synchronous Stroom client for the Stroom API checks (local Docker stack only)."""
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

ENV_FILE = Path(__file__).resolve().parents[1] / 'dev' / 'stroom' / '.env'
BASE = os.environ.get('API_CHECKS_STROOM_URL', 'http://127.0.0.1:18080')


def _credential() -> str:
    """The admin API key if one has been created, else the insecure test credential.

    Explorer endpoints reject the test credential's processing identity, so the checks create an
    admin API key with it once (see stroom_apis.py) and uses that from then on.
    """
    env = dict(line.split('=', 1) for line in ENV_FILE.read_text().splitlines() if '=' in line)
    key = env.get('STROOM_ADMIN_API_KEY') or env.get('STROOM_TEST_CREDENTIAL')
    if not key:
        raise SystemExit(f'No credentials in {ENV_FILE}; run dev/stroom/init-env.sh')
    return key.strip()


class Stroom:
    def __init__(self):
        assert '127.0.0.1' in BASE or 'localhost' in BASE, 'the API checks only run against the local stack'
        self.http = httpx.Client(base_url=BASE, timeout=120,
                                 headers={'Authorization': f'Bearer {_credential()}', 'Accept': 'application/json'})

    def call(self, method: str, path: str, body: Any = None, **kw) -> Any:
        r = self.http.request(method, '/api' + path, json=body, **kw)
        if r.is_error:
            raise RuntimeError(f'{method} {path} -> {r.status_code}: {r.text[:800]}')
        return r.json() if r.content else None

    def get(self, path):
        return self.call('GET', path)

    def post(self, path, body):
        return self.call('POST', path, body)

    def put(self, path, body):
        return self.call('PUT', path, body)

    # --- explorer ---------------------------------------------------------------------------
    @staticmethod
    def view_filter(name='*', types=None):
        return {'includedTypes': types, 'includedRootTypes': None, 'tags': None, 'nodeFlags': None,
                'requiredPermissions': ['VIEW'], 'nameFilter': name, 'nameFilterChange': False, 'recentItems': None}

    def find(self, name='*', types=None, limit=200):
        body = self.post('/explorer/v2/find', {'filter': self.view_filter(name, types),
                                               'pageRequest': {'offset': 0, 'length': limit}})
        return [{**v['docRef'], 'path': v.get('path')} for v in body.get('values', [])
                if not types or v['docRef']['type'] in types]

    def system_node(self):
        roots = self.post('/explorer/v2/fetchExplorerNodes', {
            'openItems': [], 'temporaryOpenedItems': [], 'filter': self.view_filter(None),
            'minDepth': 1, 'ensureVisible': None, 'showAlerts': False})['rootNodes']
        return next(r for r in roots if r['type'] == 'System')

    def create(self, doc_type: str, name: str, folder_node: dict) -> dict:
        return self.post('/explorer/v2/create', {'docType': doc_type, 'docName': name,
                                                 'destinationFolder': folder_node,
                                                 'permissionInheritance': 'DESTINATION'})

    # --- data -------------------------------------------------------------------------------
    def datafeed(self, feed: str, data: bytes, path='/stroom/datafeed') -> httpx.Response:
        return self.http.post(path, content=data, headers={'Feed': feed, 'Type': 'Raw Events'})

    def find_meta(self, *terms, length=50) -> list[dict]:
        children = [{'type': 'term', 'field': f, 'condition': c, 'value': str(v)} for f, c, v in terms]
        body = self.post('/meta/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': children},
                                           'pageRequest': {'offset': 0, 'length': length}})
        return [row['meta'] for row in body.get('values', [])]

    def wait(self, what: str, check, timeout=240, every=3):
        started = time.time()
        while time.time() - started < timeout:
            result = check()
            if result:
                return result
            time.sleep(every)
        raise TimeoutError(f'timed out waiting for {what}')


def show(title: str, value: Any = None, limit: int = 1500):
    print(f'\n=== {title}')
    if value is not None:
        text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str)
        print(text[:limit] + (' ...' if len(text) > limit else ''))
