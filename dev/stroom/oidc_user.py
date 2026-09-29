"""Give the dev Keycloak's test user (analyst) a Stroom user in the Administrators group.

    uv run python dev/stroom/oidc_user.py

Uses the admin API key from dev/stroom/.env. Stroom identifies Keycloak users by their `sub` claim, so the
user is created with that subject id; the Keycloak realm must be running (dev/keycloak).
"""
import base64
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
STROOM = 'http://127.0.0.1:18080/api'
REALM = 'http://127.0.0.1:18180/realms/stroom'


def main() -> None:
    env = dict(line.split('=', 1) for line in (ROOT / 'dev' / 'stroom' / '.env').read_text().splitlines() if '=' in line)
    admin = {'Authorization': f"Bearer {env['STROOM_ADMIN_API_KEY'].strip()}"}
    token = httpx.post(f'{REALM}/protocol/openid-connect/token', data={
        'grant_type': 'password', 'client_id': 'stroom-mcp-vscode', 'username': 'analyst', 'password': 'analyst-dev',
        'scope': 'openid'}).json()['access_token']
    part = token.split('.')[1]
    claims = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    subject = claims['sub']

    existing = httpx.get(f'{STROOM}/users/v1/fetchBySubjectId/{subject}', headers=admin)
    if existing.status_code == 200 and existing.content:
        user = existing.json()
    else:
        user = httpx.post(f'{STROOM}/users/v1/createUser', headers=admin, json={
            'subjectId': subject, 'displayName': claims.get('preferred_username', 'analyst'), 'fullName': 'Dev Analyst'}).json()
    groups = httpx.post(f'{STROOM}/users/v1/find', headers=admin, json={
        'pageRequest': {'offset': 0, 'length': 100},
        'expression': {'type': 'operator', 'op': 'AND', 'children': []}}).json()
    administrators = next(g for g in groups.get('values') or [] if g.get('group') and g.get('subjectId') == 'Administrators')
    added = httpx.put(f"{STROOM}/users/v1/{user['uuid']}/{administrators['uuid']}", headers=admin)
    print(f"Stroom user {user['displayName']} (subject {subject}) in Administrators: HTTP {added.status_code}")


if __name__ == '__main__':
    main()
