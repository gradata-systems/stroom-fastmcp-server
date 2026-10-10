"""The Elastic Common Schema, as Elastic publishes it (conventions/ecs_fields.json, from ecs_flat.yml by
dev/update_ecs.py): asked for by the user, as the server had known ECS only through the few names its convention
profile mapped. An index plan that follows ECS is checked against it, field by field; and a Data element whose name
is an ECS field's (src_ip as source.ip) is drafted under that name.
"""
import difflib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_FILE = Path(__file__).resolve().parents[1] / 'conventions' / 'ecs_fields.json'
# An index plan's types, and the ECS types each stands for.
_PLAN_TYPES = {
    'keyword': {'keyword', 'constant_keyword', 'wildcard', 'flattened'},
    'text': {'text', 'match_only_text'},
    'long': {'long', 'integer', 'short', 'byte', 'unsigned_long'},
    'double': {'double', 'float', 'half_float', 'scaled_float'},
    'date': {'date'},
    'boolean': {'boolean'},
    'ip': {'ip'},
}
# Fields Stroom needs in every document, whatever the convention.
_STROOM = {'StreamId', 'EventId'}


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    return json.loads(_FILE.read_text(encoding='utf-8'))


def version() -> str:
    return schema()['version']


def field(name: str) -> dict[str, Any] | None:
    return schema()['fields'].get(name)


def plan_type(name: str) -> str | None:
    """The index plan type for an ECS field (None for an object, nested or geo field)."""
    spec = field(name)
    return next((t for t, kinds in _PLAN_TYPES.items() if spec and spec['type'] in kinds), None)


def fields_in(prefix: str, limit: int = 200) -> dict[str, str]:
    """ECS fields under a field set or prefix (process, user.name), each with its type and short description."""
    prefix = prefix.strip().rstrip('.')
    found = {n: f"{s['type']}: {s['short']}" for n, s in schema()['fields'].items()
             if n == prefix or n.startswith(prefix + '.')}
    return dict(list(found.items())[:limit])


def check(name: str, type_: str) -> str | None:
    """Why a planned field doesn't follow ECS, or None. A name outside ECS's field sets is a custom field, which ECS
    allows; one inside a field set must be an ECS field, of a type that maps to ECS's."""
    if name in _STROOM:
        return None
    spec = field(name)
    if spec:
        kinds = _PLAN_TYPES.get(type_, set())
        if spec['type'] not in kinds and type_ != 'id':
            fits = next((t for t, k in _PLAN_TYPES.items() if spec['type'] in k), None)
            return (f"{name} is typed {type_}, where ECS {version()} has {spec['type']}"
                    + (f" (plan type {fits})" if fits else ''))
        return None
    top = name.split('.')[0]
    if top in schema()['field_sets'] and '.' in name:
        under = [n for n in schema()['fields'] if n.startswith(top + '.')]
        near = difflib.get_close_matches(name, under, n=3, cutoff=0.6)
        return (f"{name} is in ECS's {top} field set but isn't an ECS {version()} field"
                + (f": did you mean {', '.join(near)}?" if near else '')
                + " (a field of your own goes outside ECS's field sets, e.g. under labels or your organisation's name)")
    return None


def problems(fields: list[Any]) -> list[str]:
    """Each planned field's departure from ECS (fields with name and type)."""
    return [p for f in fields if (p := check(f.name, f.type))]


def for_data_name(data_name: str) -> str | None:
    """The ECS field a Data element's name is, written in another case or with other separators: src_ip is not
    source.ip (that would be guessing), but source_ip, sourceIp and Source-IP are. None when it isn't one."""
    words = re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+', data_name.replace('.', ' '))
    if not words:
        return None
    snake = '_'.join(w.lower() for w in words)
    candidates = [data_name.lower(), snake] + [snake.replace('_', '.', n) for n in range(1, snake.count('_') + 1)]
    for candidate in candidates:
        if field(candidate) and plan_type(candidate):
            return candidate
    # Dots only between ECS field set and field: user_full_name as user.full_name, not user.full.name.
    parts = snake.split('_')
    for split in range(1, len(parts)):
        candidate = '.'.join(['_'.join(parts[:split]), '_'.join(parts[split:])])
        if field(candidate) and plan_type(candidate):
            return candidate
    return None
