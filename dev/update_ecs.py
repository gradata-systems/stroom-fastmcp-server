"""Refresh conventions/ecs_fields.json from Elastic's published ECS schema (ecs_flat.yml, Apache-2.0).

    uv run python dev/update_ecs.py [version]      # default: the version the file holds now

Each field keeps what the server checks and shows: its type, level (core, extended, custom), a short description,
whether it is an array, and the values ECS allows for it (event.outcome's success, failure, unknown). The field sets
(event, user, source...) come with it, so a name in an ECS field set that ECS doesn't define can be told from a
custom field. Pin a release at least a few weeks old.
"""
import json
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'conventions' / 'ecs_fields.json'
SOURCE = 'https://raw.githubusercontent.com/elastic/ecs/v{version}/generated/ecs/ecs_flat.yml'


def compact(flat: dict, version: str) -> dict:
    fields = {}
    for name, spec in sorted(flat.items()):
        entry = {'type': spec.get('type'), 'level': spec.get('level'),
                 'short': ' '.join((spec.get('short') or '').split())}
        if 'array' in (spec.get('normalize') or []):
            entry['array'] = True
        allowed = [a['name'] for a in spec.get('allowed_values') or [] if a.get('name')]
        if allowed:
            entry['allowed'] = allowed
        fields[name] = entry
    sets = sorted({name.split('.')[0] for name in fields if '.' in name})
    return {'version': version, 'source': SOURCE.format(version=version), 'licence': 'Apache-2.0 (Elastic ECS)',
            'field_sets': sets, 'fields': fields}


def main(version: str | None) -> None:
    version = version or json.loads(OUT.read_text(encoding='utf-8'))['version']
    text = httpx.get(SOURCE.format(version=version), follow_redirects=True, timeout=60).raise_for_status().text
    data = compact(yaml.safe_load(text), version)
    OUT.write_text(json.dumps(data, indent=0, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f"ECS {version}: {len(data['fields'])} fields in {len(data['field_sets'])} field sets -> {OUT}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
