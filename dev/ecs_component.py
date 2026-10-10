"""What Elasticsearch's built-in ecs@mappings component template maps each ECS field as, measured: run against the local
stack's Elasticsearch (the elastic profile), it writes conventions/ecs_component.json, the ECS fields it maps otherwise
than ECS says (or not at all). An ECS plan's index template leaves the rest to the component (asked for by the user:
"relying upon the ECS component template for standard ECS fields and only mapping those that aren't").

Each field is written as the indexing XSLT writes its plan type (a JSON number for long and double, a boolean, else a
string), into one document under a template composing ecs@mappings with dynamic mapping on, as an ECS plan's template
is; the mapping Elasticsearch made is read back and compared with the bundled schema (conventions/ecs_fields.json).

    uv run python dev/ecs_component.py [http://127.0.0.1:19200]
"""
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import ecs  # noqa: E402

VALUES = {'keyword': 'x', 'text': 'x y', 'date': '2026-10-11T09:00:00.000Z', 'long': 1, 'double': 1.5,
          'boolean': True, 'ip': '10.0.0.1'}
PROBE = 'ecs-component-probe'


def flatten(properties: dict, prefix: str = '') -> dict[str, str]:
    out = {}
    for name, spec in properties.items():
        path = f'{prefix}.{name}' if prefix else name
        if 'properties' in spec:
            out.update(flatten(spec['properties'], path))
        else:
            out[path] = spec.get('type', 'object')
    return out


def main(url: str) -> None:
    fields = {n: t for n in ecs.schema()['fields'] if (t := ecs.plan_type(n))}
    doc: dict = {}
    skipped = []
    for name, plan in sorted(fields.items()):
        node, parts = doc, name.split('.')
        try:
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    raise TypeError
            if parts[-1] in node:
                raise TypeError
            node[parts[-1]] = VALUES[plan]
        except TypeError:
            skipped.append(name)        # a leaf with fields below it (none in ECS today)
    with httpx.Client(base_url=url, timeout=60) as es:
        version = es.get('/').json()['version']['number']
        es.delete(f'/{PROBE}')
        es.delete(f'/_index_template/{PROBE}')
        es.put(f'/_index_template/{PROBE}', json={
            'index_patterns': [PROBE], 'priority': 500, 'composed_of': ['ecs@mappings'],
            'template': {'settings': {'index.mapping.total_fields.limit': 10000},
                         'mappings': {'dynamic': True}}}).raise_for_status()
        es.put(f'/{PROBE}/_doc/1?refresh=true', json=doc).raise_for_status()
        mapped = flatten(es.get(f'/{PROBE}/_mapping').json()[PROBE]['mappings']['properties'])
        es.delete(f'/{PROBE}')
        es.delete(f'/_index_template/{PROBE}')
    differs = {n: mapped.get(n, 'unmapped') for n in sorted(fields)
               if n not in skipped and mapped.get(n) != ecs.field(n)['type']}
    out = {'elasticsearch': version, 'ecs': ecs.version(), 'fields_checked': len(fields) - len(skipped),
           'differs': differs}
    (ROOT / 'conventions' / 'ecs_component.json').write_text(json.dumps(out, indent=1, sort_keys=True) + '\n',
                                                             encoding='utf-8')
    print(f"Elasticsearch {version}, ECS {ecs.version()}: {out['fields_checked']} fields, {len(differs)} mapped "
          f"otherwise than ECS says")
    for name, kind in list(differs.items())[:40]:
        print(f"  {name}: {kind} (ECS {ecs.field(name)['type']})")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:19200')
