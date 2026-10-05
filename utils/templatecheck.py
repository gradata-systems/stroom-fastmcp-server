"""Check an Elasticsearch index template against the documents an indexing pipeline writes.

The documents come from stepping the candidate indexing pipeline: its XSLT's JSON XML output, the form
Stroom's ElasticIndexingFilter sends. Each document field is compared with the template's mapping:
missing fields under the template's dynamic setting, values the mapped type cannot take, parent/child
clashes, and fields the template expects but the pipeline never writes (often renamed in the template).
Every mismatch comes back as a change to make, to the pipeline or to the template.
"""
import copy
import difflib
import fnmatch
import ipaddress
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

FN = 'http://www.w3.org/2005/xpath-functions'
KEYWORD_LIKE = {'keyword', 'text', 'wildcard', 'match_only_text', 'constant_keyword', 'version', 'search_as_you_type'}
INTEGER = {'long', 'integer', 'short', 'byte', 'unsigned_long'}
DECIMAL = {'double', 'float', 'half_float', 'scaled_float'}
OBJECT = {'object', 'nested', 'flattened'}
_ISO = re.compile(r'^\d{4}-\d{2}-\d{2}(T\d{2}(:\d{2}(:\d{2}(\.\d{1,9})?)?)?(Z|[+-]\d{2}(:?\d{2})?)?)?$')
_INT = re.compile(r'^-?\d+$')
_NUM = re.compile(r'^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$')


def _json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as e:
        raise ValueError(f"The {what} is not valid JSON: {e}") from e


def parse_template(text: str) -> tuple[str | None, dict[str, Any]]:
    """An Elasticsearch index template as the user pastes it: Dev Tools 'PUT _index_template/name {...}', the body, or
    GET _index_template output; or an existing index's mapping (GET <index>/_mapping, or {mappings: ...}), which has
    no index_patterns, so which indices it covers is not checked."""
    text = text.strip()
    name = None
    first, _, rest = text.partition('\n')
    match = re.match(r'^(PUT|POST)\s+/?_index_template/([^\s?]+)', first.strip(), re.I)
    if match:
        name, text = match.group(2), rest
    body = _json(text, 'index template')
    if isinstance(body, dict) and 'index_templates' in body:
        entry = (body['index_templates'] or [{}])[0]
        name, body = entry.get('name', name), entry.get('index_template', {})
    if isinstance(body, dict) and 'index_patterns' not in body:
        # A mapping: GET <index>/_mapping ({index: {mappings}}), {mappings: ...}, or the mappings themselves.
        if len(body) == 1 and isinstance(next(iter(body.values())), dict) and 'mappings' in next(iter(body.values())):
            name, body = next(iter(body)), next(iter(body.values()))
        mappings = body.get('mappings') if 'mappings' in body else body if 'properties' in body else None
        if isinstance(mappings, dict):
            return name, {'template': {'mappings': mappings}}
    if not isinstance(body, dict) or 'index_patterns' not in body:
        raise ValueError("Expected an Elasticsearch index template (index_patterns and template.mappings), or an "
                         "index's mapping (GET <index>/_mapping)")
    return name, body


def parse_component_templates(texts: list[str]) -> dict[str, dict[str, Any]]:
    """Component templates as the user pastes them, by name: Dev Tools 'PUT _component_template/name {...}', or GET
    _component_template output (one or several). Each needs its name, to be matched to composed_of."""
    found: dict[str, dict[str, Any]] = {}
    for text in texts:
        text = text.strip()
        first, _, rest = text.partition('\n')
        match = re.match(r'^(PUT|POST)\s+/?_component_template/([^\s?]+)', first.strip(), re.I)
        body = _json(rest if match else text, 'component template')
        if isinstance(body, dict) and 'component_templates' in body:
            for entry in body['component_templates'] or []:
                found[entry.get('name')] = entry.get('component_template') or {}
        elif match and isinstance(body, dict):
            found[match.group(2)] = body
        else:
            raise ValueError("Give each component template with its name, as a Dev Tools request "
                             "(PUT _component_template/<name> {...}) or GET _component_template/<name> output, so it can "
                             "be matched to the index template's composed_of")
    return found


def _merge(into: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    for key, value in other.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = copy.deepcopy(value)
    return into


def compose(body: dict[str, Any], components: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """(body, missing): the index template with its component templates' mappings merged in as Elasticsearch composes
    them (each of composed_of in order, then the template's own mappings over them), and those of composed_of not
    given."""
    merged: dict[str, Any] = {}
    missing = []
    for name in body.get('composed_of') or []:
        if name in components:
            _merge(merged, ((components[name].get('template') or {}).get('mappings')) or {})
        else:
            missing.append(name)
    _merge(merged, ((body.get('template') or {}).get('mappings')) or {})
    return {**body, 'template': {**(body.get('template') or {}), 'mappings': merged}}, missing


@dataclass
class Mapping:
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)   # path -> spec (type, format, ...)
    dynamic: dict[str, str] = field(default_factory=dict)             # object path ('' for root) -> setting

    def dynamic_for(self, path: str) -> str:
        parts = path.split('.')
        for n in range(len(parts) - 1, -1, -1):
            setting = self.dynamic.get('.'.join(parts[:n]))
            if setting is not None:
                return str(setting).lower()
        return 'true'


def read_mapping(mappings: dict[str, Any]) -> Mapping:
    out = Mapping()
    if 'dynamic' in mappings:
        out.dynamic[''] = mappings['dynamic']

    def walk(properties: dict[str, Any], prefix: str) -> None:
        for name, spec in (properties or {}).items():
            path = f'{prefix}.{name}' if prefix else name
            if 'dynamic' in spec:
                out.dynamic[path] = spec['dynamic']
            if 'properties' in spec:
                out.fields[path] = {'type': spec.get('type', 'object')}
                walk(spec['properties'], path)
            else:
                out.fields[path] = spec
    walk(mappings.get('properties'), '')
    return out


# Per-field parameters that describe a style (how this environment maps a type), as opposed to one field's own.
_STYLE_PARAMS = ('type', 'ignore_above', 'fields', 'format', 'norms', 'doc_values', 'index', 'normalizer', 'analyzer',
                 'null_value', 'scaling_factor')
# Naming styles, judged per dotted segment: 'lower' (one lower-case word) fits ECS-style and camelCase names alike.
_SEGMENT = (('pascal', re.compile(r'[A-Z][A-Za-z0-9]*')), ('camel', re.compile(r'[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*')),
            ('snake', re.compile(r'[a-z0-9]+(?:_[a-z0-9]+)+')), ('lower', re.compile(r'[a-z0-9]+')))
STYLE_NAMES = {'ecs': 'lower case and snake_case (ECS-style, e.g. user.name, source.ip)',
               'pascal': 'PascalCase (e.g. User.Id, TypeId)', 'camel': 'camelCase (e.g. user.userId)'}
# Named so for Stroom (or Elasticsearch), whatever the index's style.
_EXEMPT = ('StreamId', 'EventId')


def _segment(part: str) -> str:
    return next((style for style, pattern in _SEGMENT if pattern.fullmatch(part)), 'other')


def naming_style(name: str) -> str | None:
    """'ecs', 'pascal', 'camel' or 'other' for a field name, judging each dotted part; None for names exempt from
    any style: @timestamp, _id and the like, StreamId and EventId."""
    if name.startswith(('@', '_')) or name in _EXEMPT:
        return None
    parts = {_segment(p) for p in name.split('.')}
    if parts <= {'lower', 'snake'}:
        return 'ecs'
    if parts == {'pascal'}:
        return 'pascal'
    if parts <= {'lower', 'camel'}:
        return 'camel'
    return 'other'


def fits_style(name: str, style: str) -> bool:
    found = naming_style(name)
    return found is None or found == style or (style == 'camel' and found == 'ecs' and '_' not in name)


def example_naming(fields: list[str]) -> str | None:
    """The style most of an example's field names follow, or None when none does."""
    names = [f for f in fields if naming_style(f) is not None]
    if not names:
        return None
    exact = {style: sum(naming_style(f) == style for f in names) for style in STYLE_NAMES}
    fitting = [s for s in STYLE_NAMES if sum(fits_style(f, s) for f in names) * 2 > len(names)]
    return max(fitting, key=lambda s: exact[s]) if fitting else None


def example_dotted(fields: list[str]) -> bool:
    """Whether the example nests its names (User.Id) rather than running them together (UserId)."""
    names = [f for f in fields if naming_style(f) is not None]
    return sum('.' in f for f in names) * 2 > len(names)


# Event-logging path segments too general to name a field on their own.
_GENERIC = {'id', 'name', 'value', 'type', 'text', 'data', 'code', 'state', 'number', 'description'}
_ROOTS = ('EventSource', 'EventDetail', 'EventTime', 'EventChain')
_PLAN_TYPE = {'keyword': 'keyword', 'constant_keyword': 'keyword', 'wildcard': 'keyword', 'text': 'text',
              'match_only_text': 'text', 'date': 'date', 'date_nanos': 'date', 'long': 'long', 'integer': 'long',
              'short': 'long', 'byte': 'long', 'unsigned_long': 'long', 'double': 'double', 'float': 'double',
              'half_float': 'double', 'scaled_float': 'double', 'boolean': 'boolean', 'ip': 'ip'}


def _squash(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.lower())


def _segments(source: str) -> list[str]:
    """An event-logging path's element names below the root, e.g. EventSource/User/Id -> [User, Id]."""
    parts = [re.sub(r'\[.*?\]', '', p) for p in source.split('/') if p and not p.startswith('@')]
    return parts[1:] if len(parts) > 1 and parts[0] in _ROOTS else parts


def _match(source: str, example: dict[str, dict[str, Any]], known: set[str]) -> str | None:
    """The example field that holds this event-logging path: one whose name is the path's trailing elements run
    together (user.id, User.Id, UserId <- EventSource/User/Id; TypeId <- EventDetail/TypeId), longest first, else
    one a convention names for the path (ECS's user.name for the user id). The path's own elements come first: an
    example with user.id and user.name maps the id to user.id, not to the convention's user.name. A single generic
    element (Id, Name) is not enough on its own."""
    leaves = [f for f, spec in example.items() if 'properties' not in spec and spec.get('type') != 'object']
    parts = _segments(source)
    by_squash = {}
    for name in leaves:
        by_squash.setdefault(_squash(name), name)
    for i in range(len(parts)):
        run = parts[i:]
        if len(run) == 1 and (_squash(run[0]) in _GENERIC or len(run[0]) < 4):
            continue
        hit = by_squash.get(_squash(''.join(run)))
        if hit:
            return hit
    return next((name for name in leaves if name in known), None)


def _words(part: str) -> list[str]:
    """IPAddress -> [IP, Address]; createdOn -> [created, On]."""
    return re.findall(r'[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+', part) or [part]


def _camel(words: list[str]) -> str:
    return words[0].lower() + ''.join(w[:1].upper() + w[1:].lower() for w in words[1:])


def _derive(source: str, style: str, dotted: bool, taken: set[str]) -> str | None:
    """A name in the example's style for a path it has no field for, from the path's last elements:
    Client.IPAddress or ClientIPAddress (PascalCase), client.ipAddress or clientIpAddress (camelCase),
    client.ip_address or client_ip_address (ECS-style)."""
    parts = _segments(source)
    if not parts or style not in STYLE_NAMES:
        return None

    def build(run: list[str]) -> str:
        if style == 'pascal':
            names = [p[:1].upper() + p[1:] for p in run]
            return '.'.join(names) if dotted else ''.join(names)
        if style == 'camel':
            return '.'.join(_camel(_words(p)) for p in run) if dotted else _camel([w for p in run for w in _words(p)])
        names = ['_'.join(w.lower() for w in _words(p)) for p in run]
        return '.'.join(names) if dotted else '_'.join(names)
    for take in range(min(2, len(parts)), len(parts) + 1):
        run = parts[-take:]
        if take == 1 and _squash(run[0]) in _GENERIC and len(parts) > 1:
            continue
        name = build(run)
        if name not in taken:
            return name
    return None


# Path elements that only lead to the value, left out of a nested name: Update/After/Configuration/Type is
# Update.Configuration.Type, Network/*/Source/Device/IPAddress is Source.IPAddress.
_FILLER = {'After', 'Before', 'Device', '*'}


def field_group(source: str) -> tuple[str, list[str]] | None:
    """(group, the rest) of an event-detail path, the group being the element its fields belong together under:
    the action element (Alert, Authenticate), or for a network event the side (Source, Destination)."""
    parts = _segments(source)
    if not parts or not source.strip('/').startswith('EventDetail/') or len(parts) < 2:
        return None
    if parts[0] == 'Network' and len(parts) >= 4:
        group, rest = parts[2], parts[3:]
    else:
        group, rest = parts[0], parts[1:]
    rest = [p for p in rest if p not in _FILLER]
    return (group, rest) if rest else None


def _together(source: str) -> str | None:
    """Which fields count together when deciding to nest: a connection's two sides are one (Source.Port nests, so
    Destination.IPAddress does), the rest by their group."""
    grouped = field_group(source)
    return None if not grouped else 'Network' if source.strip('/').startswith('EventDetail/Network/') else grouped[0]


def nested_name(source: str, style: str) -> str | None:
    """A nested name in the example's style for a path whose group has other fields: Alert.Type, Source.Port."""
    grouped = field_group(source)
    if not grouped or style not in STYLE_NAMES:
        return None
    group, rest = grouped
    if style == 'pascal':
        return '.'.join(p[:1].upper() + p[1:] for p in [group, *rest])
    if style == 'camel':
        return '.'.join(_camel(_words(p)) for p in [group, *rest])
    return '.'.join('_'.join(w.lower() for w in _words(p)) for p in [group, *rest])


def names_from_example(fields: list[dict[str, str]], example: dict[str, dict[str, Any]],
                       convention_names: dict[str, set[str]], populated: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """(fields, notes): the field plan's fields named as the user's example names them. A field takes the example's
    name for its event-logging path (_match); one the example has no field for is named in the example's style
    (_derive); populated paths the plan leaves out but the example maps are added, typed as the example types
    them. StreamId, EventId and @timestamp keep their names."""
    leaves = [f for f, spec in example.items() if 'properties' not in spec and spec.get('type') != 'object']
    style, dotted = example_naming(leaves), example_dotted(leaves)
    # An example that nests any of its names (User.Id) nests fields that belong together: a user asked for Alert.Type
    # and Alert.Severity, then the same wherever fields share an element.
    nests = any('.' in f for f in leaves if naming_style(f) is not None)
    groups = Counter(g for g in (_together(f['source']) for f in fields) if g)

    def fits(name: str) -> bool:
        # A dotted name in an example that runs its names together (userName) does not fit it either.
        return fits_style(name, style) and (dotted or '.' not in name)
    out, notes, renamed, derived, added, unlike, used = [], [], [], [], [], [], set()
    taken = {f['name'] for f in fields}
    for field in fields:
        name = field['name']
        if naming_style(name) is None:
            out.append(field)
            used.add(name)
            continue
        hit = _match(field['source'], {k: v for k, v in example.items() if k not in used},
                     convention_names.get(field['source'], set()))
        last = (_segments(field['source']) or [''])[-1]
        if hit and _squash(hit) == _squash(last) and sum(
                1 for f in fields if (_segments(f['source']) or [''])[-1] == last) > 1:
            # The example's IpAddress names an address, but which? Seen: it took the source's of two, and the
            # destination's was named apart. Both are named in the example's style instead.
            hit = None
        if hit:
            new = hit
        elif style and nests and _together(field['source']) and groups[_together(field['source'])] > 1 \
                and nested_name(field['source'], style) not in taken | used:
            new = nested_name(field['source'], style)
            derived.append(f"{name} -> {new}")
        elif style and not fits(name):
            # A convention's name for the path in the example's style (host.ip), else one built from the path.
            conventional = sorted(n for n in convention_names.get(field['source'], set()) if fits(n)
                                  and naming_style(n) is not None and ('.' in n) == dotted and n not in taken | used)
            new = conventional[0] if conventional else _derive(field['source'], style, dotted, taken | used)
            if new:
                derived.append(f"{name} -> {new}")
            else:
                unlike.append(name)
                new = name
        else:
            new = name
        if hit and hit != name:
            renamed.append(f"{name} -> {hit}")
        used.add(new)
        out.append({**field, 'name': new})
    from utils.fieldplan import any_action, source_matches
    sources = {f['source'] for f in out}
    # Network paths once, whichever action: the example's field takes every Permit's and Deny's address, not one.
    for path in dict.fromkeys(any_action(p) for p in populated):
        if path.startswith('@') or any(source_matches(s, path) or s == path for s in sources):
            continue
        hit = _match(path, {k: v for k, v in example.items() if k not in used}, convention_names.get(path, set()))
        if hit:
            out.append({'name': hit, 'type': _PLAN_TYPE.get(example[hit].get('type'), 'keyword'), 'source': path})
            used.add(hit)
            added.append(f"{hit} <- {path}")
    if renamed:
        notes.append(f"named as the example names them: {renamed}")
    if derived:
        notes.append(f"not in the example, named in its style ({STYLE_NAMES[style]}): {derived}")
    if added:
        notes.append(f"in the sample and mapped by the example, so added: {added}")
    if unlike:
        notes.append(f"named unlike the example ({STYLE_NAMES.get(style, style)}), with no name derived: {unlike}; "
                     f"agree names with the user and give them as extra_fields")
    left = [f for f, spec in example.items() if f not in used and 'properties' not in spec
            and spec.get('type') != 'object' and naming_style(f) is not None]
    if left:
        notes.append(f"in the example but not in this source's sample, so not in this plan: {left[:15]}"
                     + (f" (+{len(left) - 15})" if len(left) > 15 else ''))
    return out, notes


def type_styles(fields: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The way an example maps each type: for each type, its most common set of style parameters (keyword with
    ignore_above 1024, text with a .keyword sub-field, date with a format...)."""
    by_type: dict[str, Counter] = {}
    specs: dict[str, dict[str, Any]] = {}
    for spec in fields.values():
        if 'type' not in spec or 'properties' in spec or spec.get('type') == 'object':
            continue
        style = {k: spec[k] for k in _STYLE_PARAMS if k in spec}
        key = json.dumps(style, sort_keys=True)
        by_type.setdefault(spec['type'], Counter())[key] += 1
        specs[key] = style
    return {t: specs[c.most_common(1)[0][0]] for t, c in by_type.items()}


_MAPPING_PARAMS = ('dynamic', 'dynamic_templates', 'date_detection', 'numeric_detection', 'subobjects', '_source', '_routing')


def read_mapping_fields(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """path -> spec for an index template body's mappings (objects included, as {'type': 'object'})."""
    return read_mapping(((body.get('template') or {}).get('mappings')) or {}).fields


def object_nodes(mappings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """path -> an object field's own parameters (type, dynamic, enabled, subobjects), for each node with
    properties in a mapping."""
    out: dict[str, dict[str, Any]] = {}

    def walk(properties: dict[str, Any], prefix: str) -> None:
        for name, spec in (properties or {}).items():
            if isinstance(spec, dict) and 'properties' in spec:
                path = f'{prefix}.{name}' if prefix else name
                out[path] = {k: copy.deepcopy(v) for k, v in spec.items() if k != 'properties'}
                walk(spec['properties'], path)
    walk(mappings.get('properties'), '')
    return out


def _nest(fields: dict[str, dict[str, Any]], objects: dict[str, dict[str, Any]] | None = None,
          typed: bool = False) -> dict[str, Any]:
    """Mapping properties from dotted paths: {'source.ip': {...}} -> {'source': {'properties': {'ip': {...}}}}.
    An object the examples define gets their parameters for it; with typed, a new object is declared
    "type": "object" as the examples declare theirs."""
    properties: dict[str, Any] = {}
    objects = objects or {}
    for path, spec in fields.items():
        node = properties
        parts = path.split('.')
        for i, part in enumerate(parts[:-1]):
            where = '.'.join(parts[:i + 1])
            if part not in node:
                params = copy.deepcopy(objects.get(where)) if where in objects else ({'type': 'object'} if typed else {})
                node[part] = {**params, 'properties': {}}
            node = node[part].setdefault('properties', {})
        node[parts[-1]] = copy.deepcopy(spec)
    return properties


def from_example(planned: dict[str, Any], example: dict[str, Any], components: dict[str, dict[str, Any]],
                 discovery: bool = False) -> tuple[dict[str, Any], list[str]]:
    """(body, notes): the final index template for the new index, built from the field plan's template and the
    user's example (an index template or an index's mapping, with the component templates it is composed of): the
    new index's own pattern; the example's composed_of, settings, priority, data_stream and top-level mapping
    parameters; a field a component template defines is left to it, a field the example maps keeps the example's
    type, and the rest are typed from the plan. Aliases and _meta name the example's own indices: not copied."""
    notes = []
    own = ((example.get('template') or {}).get('mappings')) or {}
    composed_of = list(example.get('composed_of') or [])
    from_components: dict[str, dict[str, Any]] = {}
    for name in composed_of:
        if name in components:
            from_components.update(read_mapping(((components[name].get('template') or {}).get('mappings')) or {}).fields)
    missing = [n for n in composed_of if n not in components]
    in_example = read_mapping(own).fields
    plan_fields = read_mapping(((planned.get('template') or {}).get('mappings')) or {}).fields
    # The plan's values: not its object nodes (with subobjects: false, time and time.min are both values).
    leaves = {path: spec for path, spec in plan_fields.items() if spec.get('type') not in (None, 'object')}
    every = {**from_components, **in_example}
    # A discovery index keeps the source's names and Elasticsearch's dynamic mapping: the example gives its
    # settings and components, not its field conventions.
    styles = {} if discovery else type_styles(every)
    objects: dict[str, dict[str, Any]] = {}
    for name in composed_of:
        if name in components:
            objects.update(object_nodes(((components[name].get('template') or {}).get('mappings')) or {}))
    objects.update(object_nodes(own))
    typed = bool(objects) and sum('type' in o for o in objects.values()) * 2 > len(objects)
    final, left, kept, new, styled, aliased = {}, [], [], [], [], []
    for path, spec in leaves.items():
        if path in from_components:
            left.append(path)
        elif path in in_example and in_example[path].get('type') == 'alias':
            # Seen in VS Code: the example's User.Id was an alias of User.Name. Copied, the new index had an alias of a
            # field it lacks (Elasticsearch refused the template), and documents can't write to an alias anyway: the
            # pipeline writes this field, so it is a field here, typed as the example types the alias's target.
            target = in_example.get(in_example[path].get('path') or '') or {}
            final[path] = (copy.deepcopy(target) if target.get('type') not in (None, 'alias', 'object') else spec)
            aliased.append(path)
        elif path in in_example:
            final[path] = in_example[path]
            kept.append(path)
        elif spec.get('type') in styles and styles[spec['type']] != {'type': spec['type']}:
            # A field new to the examples, mapped the way they map its type.
            final[path] = copy.deepcopy(styles[spec['type']])
            styled.append(path)
        else:
            final[path] = spec
            new.append(path)
    named = [f for f, spec in every.items() if spec.get('type') != 'object']
    style = None if discovery else example_naming(named)
    unlike = [p for p in leaves if p not in every and not (fits_style(p, style) and (example_dotted(named) or '.' not in p))]         if style else []
    planned_mappings = (planned.get('template') or {}).get('mappings') or {}
    if discovery:
        mappings = {k: copy.deepcopy(v) for k, v in planned_mappings.items() if k != 'properties'}
        mappings.update({k: copy.deepcopy(own[k]) for k in ('_source', '_routing') if k in own})
    else:
        mappings = {k: copy.deepcopy(own[k]) for k in _MAPPING_PARAMS if k in own}
        mappings.setdefault('dynamic', planned_mappings.get('dynamic', False))
    # subobjects: false keeps each dotted name a field of its own (time beside time.min): no objects to nest.
    mappings['properties'] = ({path: copy.deepcopy(spec) for path, spec in final.items()}
                              if mappings.get('subobjects') is False else _nest(final, objects, typed))
    template: dict[str, Any] = {'mappings': mappings}
    settings = (example.get('template') or {}).get('settings')
    if settings:
        notes.append(f"settings from the example: {sorted(settings if 'index' not in settings else settings['index'])}")
    if settings or (planned.get('template') or {}).get('settings'):
        # The plan's own settings (a discovery index's guardrails) over the example's.
        merged = _merge(copy.deepcopy(settings or {}), (planned.get('template') or {}).get('settings') or {})
        template = {'settings': merged, **template}
    body: dict[str, Any] = {'index_patterns': planned['index_patterns'],
                            'priority': example.get('priority', planned.get('priority', 200))}
    if composed_of:
        body['composed_of'] = composed_of
    if 'data_stream' in example:
        body['data_stream'] = copy.deepcopy(example['data_stream'])
        notes.append("a data stream, as the example is")
    body['template'] = template
    if left:
        notes.append(f"left to the component templates (they map them): {left}")
    if kept:
        notes.append(f"typed as in the example: {kept}")
    if aliased:
        notes.append(f"aliases in the example, but written by this pipeline, so fields here (typed as each alias's "
                     f"target): {aliased}")
    if styled:
        notes.append(f"new to this index, mapped in the example's style for their type: {styled}")
    if new:
        notes.append(f"new to this index, typed from the field plan: {new}")
    if typed:
        notes.append('objects declared "type": "object", as the example declares them')
    left_out = [f for f, spec in in_example.items() if f not in leaves and spec.get('type') != 'object'
                and not any(other.startswith(f + '.') for other in in_example)]
    if discovery:
        notes.append("a discovery index: the source's fields are mapped dynamically as documents arrive (strings as "
                     "keywords); the example's own fields and mapping rules are not copied")
    elif left_out:
        notes.append(f"in the example but not written by this pipeline, so not in this template: {left_out[:15]}"
                     + (f" (+{len(left_out) - 15})" if len(left_out) > 15 else ''))
    if unlike:
        notes.append(f"named unlike the example ({STYLE_NAMES[style]}): {unlike}. The indexing XSLT writes the plan's "
                     f"names: draft_index_mapping again with example_template (and the component templates), so the "
                     f"plan takes the example's names, save the XSLT from it, step, and build this again")
    if missing:
        notes.append(f"composed_of {missing} not given: fields they map may be repeated here with the plan's types; ask "
                     f"the user for them (GET _component_template/<name>) and build again")
    if (example.get('template') or {}).get('aliases'):
        notes.append("the example's aliases are not copied: they name its own indices")
    return body, notes


def json_xml_documents(xml: str) -> list[dict[str, Any]]:
    """Documents from an indexing XSLT's output: <array> of <map>, or a single <map>."""
    try:
        root = etree.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', xml).encode('utf-8'))
    except etree.XMLSyntaxError:
        return []

    def value(node: etree._Element) -> Any:
        kind = etree.QName(node).localname
        if kind == 'map':
            return {c.get('key'): value(c) for c in node if isinstance(c.tag, str)}
        if kind == 'array':
            return [value(c) for c in node if isinstance(c.tag, str)]
        if kind == 'null':
            return None
        text = node.text or ''
        return ('number', text) if kind == 'number' else ('boolean', text) if kind == 'boolean' else ('string', text)
    data = value(root)
    return [d for d in (data if isinstance(data, list) else [data]) if isinstance(d, dict)]


def flatten_document(doc: dict[str, Any], prefix: str = '') -> dict[str, list[Any]]:
    """path -> values, with dotted keys and nested maps both becoming dotted paths; maps as 'object'."""
    out: dict[str, list[Any]] = {}

    def add(path: str, item: Any) -> None:
        if isinstance(item, dict):
            out.setdefault(path, []).append(('object', None))
            for key, sub in item.items():
                add(f'{path}.{key}', sub)
        elif isinstance(item, list):
            for sub in item:
                add(path, sub)
        elif item is not None:
            out.setdefault(path, []).append(item)
    for key, item in doc.items():
        add(f'{prefix}.{key}' if prefix else key, item)
    return out


def _fits(spec: dict[str, Any], kind: str, text: str) -> str | None:
    """None if the value suits the mapped type, else why not."""
    mapped = spec.get('type', 'object')
    if mapped in OBJECT:
        return None if kind == 'object' else f"is a single value, but the template maps it as {mapped}"
    if kind == 'object':
        return f"is an object, but the template maps it as {mapped}"
    if mapped in KEYWORD_LIKE:
        if mapped == 'constant_keyword' and spec.get('value') is not None and text != spec['value']:
            return f"is {text!r}, but constant_keyword only takes {spec['value']!r}"
        return None
    if mapped in INTEGER:
        return None if _INT.match(text) else f"is {text!r}, not a whole number ({mapped})"
    if mapped in DECIMAL:
        return None if _NUM.match(text) else f"is {text!r}, not a number ({mapped})"
    if mapped == 'boolean':
        return None if text in ('true', 'false') else f"is {text!r}, not true/false"
    if mapped == 'ip':
        try:
            ipaddress.ip_address(text)
            return None
        except ValueError:
            return f"is {text!r}, not an IP address"
    if mapped in ('date', 'date_nanos'):
        formats = [f.strip() for f in str(spec.get('format', 'strict_date_optional_time||epoch_millis')).split('||')]
        checks = {'strict_date_optional_time': _ISO, 'date_optional_time': _ISO, 'strict_date_time': _ISO,
                  'date_time': _ISO, 'strict_date_optional_time_nanos': _ISO, 'epoch_millis': _INT, 'epoch_second': _INT}
        known = [checks[f] for f in formats if f in checks]
        if len(known) < len(formats):
            return None   # a custom format: reported separately as unchecked
        return None if any(p.match(text) for p in known) else f"is {text!r}, which date format {'||'.join(formats)} rejects"
    return None


def compare(body: dict[str, Any], docs: list[dict[str, Any]], index_name: str | None) -> dict[str, Any]:
    blocking, changes, notes = [], [], []
    patterns = body.get('index_patterns') or []
    patterns = [patterns] if isinstance(patterns, str) else patterns
    if 'index_patterns' not in body:
        notes.append("a mapping, not an index template: which indices it covers is not checked")
    elif index_name and not any(fnmatch.fnmatchcase(index_name, p) for p in patterns):
        blocking.append(f"index_patterns {patterns} do not match the pipeline's index '{index_name}', so this template "
                        f"would not apply to it")
        changes.append({'field': None, 'problem': f"template does not cover index '{index_name}'",
                        'change': f"change index_patterns to include '{index_name}*', or set the indexing pipeline's "
                                  f"indexName to an index they match"})
    mapping = read_mapping((body.get('template') or {}).get('mappings') or {})
    for path, spec in mapping.fields.items():
        target = spec.get('path')
        if spec.get('type') == 'alias' and (not target or mapping.fields.get(target, {}).get('type') in (None, 'alias', 'object')):
            # Not checked before: a template with such an alias was agreed, and Elasticsearch refused it.
            blocking.append(f"{path}: an alias of '{target}', which this template doesn't map as a field, so "
                            f"Elasticsearch refuses the template ('an alias must refer to an existing field')")
            changes.append({'field': path, 'problem': f"alias of a field the template lacks ('{target}')",
                            'change': f"drop the alias '{path}' from the template, or map '{target}'"})
        fmt = spec.get('format')
        if spec.get('type') in ('date', 'date_nanos') and fmt and any(
                f.strip() not in ('strict_date_optional_time', 'date_optional_time', 'strict_date_time', 'date_time',
                                  'strict_date_optional_time_nanos', 'epoch_millis', 'epoch_second')
                for f in str(fmt).split('||')):
            notes.append(f"{path}: custom date format {fmt!r} not checked here; stepping output is ISO 8601")
    emitted: dict[str, list[Any]] = {}
    for doc in docs:
        for path, values in flatten_document(doc).items():
            emitted.setdefault(path, []).extend(values)
    if body.get('data_stream') is not None and '@timestamp' not in emitted:
        blocking.append("the template makes a data stream, which needs @timestamp, but the pipeline does not write it")
        changes.append({'field': '@timestamp', 'problem': 'missing for a data stream',
                        'change': "write @timestamp from EventTime/TimeCreated in the indexing XSLT"})

    unmapped = []
    for path in sorted(emitted):
        values = emitted[path]
        spec = mapping.fields.get(path)
        if spec is not None and spec.get('type') == 'alias':
            blocking.append(f"{path}: written by the pipeline, but mapped as an alias (of '{spec.get('path')}'), and "
                            f"documents can't write to an alias, so they are rejected")
            changes.append({'field': path, 'problem': 'written, but mapped as an alias',
                            'change': f"map '{path}' as a field in the template (as '{spec.get('path')}' is mapped), or "
                                      f"stop writing it in the indexing XSLT"})
            continue
        if spec is None:
            parent = next((p for p in _parents(path) if p in mapping.fields
                           and mapping.fields[p].get('type', 'object') not in OBJECT), None)
            if parent:
                blocking.append(f"{path}: the template maps '{parent}' as {mapping.fields[parent]['type']}, so it "
                                f"cannot also hold '{path}'")
                changes.append({'field': path, 'problem': f"'{parent}' is a {mapping.fields[parent]['type']}",
                                'change': f"rename '{path}' in the indexing XSLT, or map '{parent}' as an object"})
                continue
            if all(kind == 'object' for kind, _ in values):
                continue   # an object: its leaves are checked on their own
            unmapped.append(path)
            continue
        bad = next(((text, why) for kind, text in values for why in [_fits(spec, kind, text or '')] if why), None)
        if bad:
            blocking.append(f"{path} {bad[1]}")
            changes.append({'field': path, 'problem': bad[1],
                            'change': f"change the indexing XSLT to write '{path}' as a valid {spec.get('type')}, or "
                                      f"map it with a type that takes these values"})

    expected = [p for p, s in mapping.fields.items() if s.get('type', 'object') not in OBJECT and s.get('type') != 'alias'
                and p not in emitted]
    for path in unmapped:
        dynamic = mapping.dynamic_for(path)
        renamed = difflib.get_close_matches(path, expected, n=1, cutoff=0.6)
        if renamed:
            changes.append({'field': path, 'problem': f"not in the template, which has '{renamed[0]}' instead",
                            'change': f"rename '{path}' to '{renamed[0]}' in the indexing XSLT"})
            expected.remove(renamed[0])
            if dynamic == 'strict':
                blocking.append(f"{path}: not in the template and dynamic is strict, so documents are rejected")
            continue
        if dynamic == 'strict':
            blocking.append(f"{path}: not in the template and dynamic is strict, so documents are rejected")
            changes.append({'field': path, 'problem': 'not mapped (dynamic: strict)',
                            'change': f"add '{path}' to the template, or stop writing it in the indexing XSLT"})
        elif dynamic in ('false', 'runtime'):
            notes.append(f"{path}: written by the pipeline but not mapped (dynamic: {dynamic}), so it is kept in "
                         f"_source but not searchable")
        else:
            notes.append(f"{path}: not mapped; Elasticsearch will add it with a guessed type (dynamic mapping)")
    for path in expected:
        changes.append({'field': path, 'problem': 'in the template but the pipeline never writes it',
                        'change': f"add '{path}' to the indexing XSLT (from the right event-logging path), or drop "
                                  f"it from the template"})
    return {'compatible': not blocking, 'blocking': blocking, 'pipeline_changes': changes, 'notes': notes,
            'documents_checked': len(docs), 'fields_written': sorted(emitted)}


def _parents(path: str) -> list[str]:
    parts = path.split('.')
    return ['.'.join(parts[:n]) for n in range(len(parts) - 1, 0, -1)]
