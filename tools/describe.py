"""What a feed holds and what one of its fields means, for anyone asking about the data rather than building it (asked
for by the user: an agent in OpenWebUI, or in Stroom itself, answering "what is the User.DomainName field, and how do
I search for a domain ending in example.com?"). Read only: from the documentation the server generated for the feed's
pipelines and indexes (found by their mcp-generated tag), the mapping kept with the events XSLT, and the indexes' own
field lists as Stroom has them from Elasticsearch or Lucene."""
import re
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import GENERATED
from utils.stroom import body_text, doc_link, gateway_from

PURPOSE_CHARS = 2_000     # a doc's Purpose and data section, up to this
ROWS_SHOWN = 12           # rows about the field, across the docs
SIMILAR_SHOWN = 10        # index fields named like the one asked about, when none is it


def norm(name: str) -> str:
    """A field name for comparison: User.DomainName, user_domain_name and EventSource/User/DomainName's tail alike."""
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def words(name: str) -> set[str]:
    """A field name's words, lower-cased: User.DomainName, user_domain_name and userDomainName give user, domain, name."""
    return {w.lower() for w in re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+', name or '')}


def similar(field: str, names: list[str]) -> list[str]:
    """The names sharing the most words with the field, most first (for a name the user misremembered)."""
    want = words(field)
    scored = [(len(want & words(n)), n) for n in names]
    return [n for score, n in sorted(scored, key=lambda x: -x[0]) if score][:SIMILAR_SHOWN]


def section(markdown: str, heading: str) -> str:
    """The text of a `## heading` section, its own `###` subsections included."""
    found = re.search(rf'^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)', markdown or '', re.M | re.S)
    return found.group(1).strip() if found else ''


def tables(markdown: str) -> list[list[dict[str, str]]]:
    """Each Markdown table in the text, as rows keyed by their header; a cell's escaped bars and <br>s undone."""
    out: list[list[dict[str, str]]] = []
    lines = (markdown or '').splitlines()
    n = 0
    while n < len(lines):
        if lines[n].lstrip().startswith('|') and n + 1 < len(lines) and re.match(r'^\s*\|[\s:|-]+\|\s*$', lines[n + 1]):
            head = _cells(lines[n])
            rows = []
            n += 2
            while n < len(lines) and lines[n].lstrip().startswith('|'):
                cells = _cells(lines[n])
                rows.append({h: (cells[i] if i < len(cells) else '') for i, h in enumerate(head)})
                n += 1
            out.append(rows)
        else:
            n += 1
    return out


def _cells(line: str) -> list[str]:
    inner = line.strip()[1:]
    inner = inner[:-1] if inner.endswith('|') and not inner.endswith('\\|') else inner
    return [c.strip().replace('\\|', '|').replace('<br>', '\n') for c in re.split(r'(?<!\\)\|', inner)]


def _named(cell: str) -> list[str]:
    """The names or paths a cell gives in backticks (EventDetail lines: `Authenticate/User/Id` <- user = "bob")."""
    return re.findall(r'`([^`]+)`', cell or '')


def _event_path(path: str) -> str:
    """An event-logging path as the events doc writes it, without Event/ and its Data name: comparable to an index
    plan's source."""
    return re.sub(r"\[@Name=.*$", '', path.strip('/').removeprefix('Event/'))


def _path_matches(source: str, path: str) -> bool:
    """An index field's source (EventDetail/*/User/Id) against a path the events doc gives (Authenticate/User/Id in
    the event types table, EventDetail/ left off)."""
    from utils.fieldplan import source_matches
    source, path = _event_path(source), _event_path(path)
    return any(source_matches(source, p) for p in (path, 'EventDetail/' + path))


def rows_about(markdown: str, field: str, sources: list[str]) -> list[dict[str, str]]:
    """The Field mapping rows about a field: an index doc's row naming it; an events doc's row (the EventSource
    table's, or a rule's, with only its EventDetail lines about the field) whose path is one of the field's sources,
    ends with the field's name, or writes it as a Data name, or that reads it as its source field (`x` <- `field`)."""
    want = norm(field)
    found = []
    for table in tables(section(markdown, 'Field mapping') or markdown):
        for row in table:
            cells = list(row.items())
            if not cells:
                continue
            if any(norm(n) == want for n in _named(cells[0][1])):
                found.append(row)
                continue
            matched: dict[str, list[str]] = {}
            for header, cell in cells:
                for line in cell.splitlines():
                    named = _named(line)
                    path = named[0] if named and '/' in named[0] else None
                    data = re.search(r"\[@Name='([^']+)'\]", path or '')
                    read = _named(line.split('<-', 1)[1]) if '<-' in line else named if header.startswith('From') else []
                    if (path and sources and any(_path_matches(s, path) for s in sources))                             or (path and not sources and norm(_event_path(path)).endswith(want))                             or (data and norm(data.group(1)) == want) or any(norm(n) == want for n in read):
                        matched.setdefault(header, []).append(line.strip())
            if matched:
                # A rule's EventDetail cell cut to the lines about the field; the other cells as they are.
                found.append({k: '\n'.join(matched[k]) if k in matched and '<-' in ''.join(matched[k]) else v
                              for k, v in row.items()})
    return found


def mentions(markdown: str, field: str) -> list[str]:
    """Lines outside the tables that name the field, as a word (a list of extracted fields, an open item)."""
    pattern = re.compile(rf'(?<![\w.]){re.escape(field)}(?![\w])', re.I)
    return [line.strip()[:300] for line in (markdown or '').splitlines()
            if pattern.search(line) and not line.lstrip().startswith(('|', '#'))]


def search_advice(backend: str, kind: str, analyzer: str = '', case_sensitive: bool = True) -> list[str]:
    """How to search a field of this type through Stroom (a dashboard or query term), from what Stroom 7.13 was found
    to do (see verify_index's checks): what works, and what quietly finds nothing."""
    kind, analyzer = (kind or '').lower(), (analyzer or '').upper()
    if kind in ('ip', 'ipv4_address'):
        return ["An IP address field: EQUALS an address ('10.1.2.3'), or a CIDR range for a network ('10.1.0.0/16').",
                "A wildcard ('10.1.*') finds nothing on an ip field; use the CIDR range."]
    if kind in ('date', 'date_field'):
        return ["A date: BETWEEN 'from,to' (ISO 8601, e.g. '2026-10-01T00:00:00.000Z,2026-10-02T00:00:00.000Z'), "
                "GREATER_THAN or LESS_THAN; relative times work too ('day()-7d,now()')."]
    if kind in ('long', 'integer', 'int', 'id', 'float', 'double', 'long_field', 'integer_field', 'id_field',
                'float_field', 'double_field'):
        return ["A number: EQUALS, GREATER_THAN, LESS_THAN, BETWEEN 'low,high', or IN 'a,b,c'."]
    if kind in ('boolean', 'boolean_field'):
        return ["A boolean: EQUALS 'true' or 'false'."]
    if backend == 'elasticsearch' and kind == 'text':
        return ["A text field: Elasticsearch splits the value into words and lower-cases them, so EQUALS matches a "
                "word of it, not the whole value, and a wildcard matches within one word.",
                "For the whole value (equals, starts or ends with), search its keyword sub-field when the index has one "
                "(listed under sub_fields)."]
    case = 'case-sensitive' if case_sensitive else 'not case-sensitive'
    exact = [f"Exact ({case}): EQUALS the whole value. Wildcards in EQUALS for the rest: ends with x is "
             "EQUALS '*x', starts with x is EQUALS 'x*', contains x is EQUALS '*x*'.",
             "Has any value: EQUALS '*'. Several values: IN 'a,b,c' (commas between them)."]
    if backend == 'elasticsearch':
        return exact + ["Through Stroom on Elasticsearch, STARTS_WITH and CONTAINS find nothing, and IS_NULL and "
                        "IS_NOT_NULL match every document: use the wildcards above. A leading wildcard ('*x') reads "
                        "every value in the index, so narrow the time range first on a large index."]
    if kind in ('text', 'text_field') and analyzer and analyzer != 'KEYWORD':
        return [f"A text field (analyzer {analyzer.lower()}): split into words, so EQUALS matches a word of it; "
                f"CONTAINS works; ranges (BETWEEN, GREATER_THAN) find nothing. A value holding a space in IN finds "
                f"nothing: search it with EQUALS."]
    return exact + ["On a Lucene index STARTS_WITH, ENDS_WITH and CONTAINS find nothing on a field like this: use the "
                    "wildcards above. A value holding a space in IN finds nothing: search it with EQUALS."]


async def _text(stroom, uuid: str) -> str | None:
    """A Documentation doc's body; None when Stroom no longer has it (seen: the explorer's search still listing a
    deleted doc)."""
    try:
        return body_text(await stroom.get_doc('Documentation', uuid))
    except ToolError:
        return None


async def _generated_doc(stroom, guard, name: str, folder: str | None) -> dict[str, Any] | None:
    """The Documentation doc named after a pipeline or index doc: the generated one (tagged mcp-generated) in its
    folder first, then any generated one of that name, then one written by hand beside it."""
    from tools.builds import _path
    tagged = [v for v in (await stroom.find_documents(f'tag:{GENERATED} {name}', ['Documentation'], 20)).get('values') or []
              if v['docRef'].get('name') == name]
    for v in sorted(tagged, key=lambda v: _path(v.get('path')) != folder):
        if not _path(v.get('path')).startswith(f'System/{guard.workspace}'):    # not a draft in a build, unpromoted
            return {**v['docRef'], 'generated': True, 'path': _path(v.get('path'))}
    beside = [v for v in (await stroom.find_documents(name, ['Documentation'], 20)).get('values') or []
              if v['docRef'].get('name') == name and _path(v.get('path')) == folder]
    return {**beside[0]['docRef'], 'generated': False, 'path': folder} if beside else None


async def _pipelines_reading(stroom, feed: str) -> list[dict[str, Any]]:
    """Pipelines whose processor filters read the feed (named, in a dictionary, or through its streams' ids), each
    with whether it reads another pipeline's output (an indexing or CEF pipeline) rather than the raw data."""
    from tools.coverage import filter_feeds, filter_terms
    body = await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})
    found: dict[str, dict[str, Any]] = {}
    known: dict[Any, Any] = {}
    for row in (body or {}).get('values') or []:
        f = row.get('processorFilter') or {}
        if f.get('deleted') or not f.get('pipelineUuid'):
            continue
        ts = filter_terms((f.get('queryData') or {}).get('expression'))
        if feed.lower() not in await filter_feeds(stroom, ts, known):
            continue
        events = any((t.get('field') == 'Type' and t.get('value') == 'Events') or t.get('field') == 'Pipeline' for t in ts)
        entry = found.setdefault(f['pipelineUuid'], {'uuid': f['pipelineUuid'], 'name': f.get('pipelineName'),
                                                     'reads_events': events})
        entry['reads_events'] = entry['reads_events'] and events
    return list(found.values())


async def _indexes_written(ctx: Context, stroom, pipeline_uuid: str) -> list[dict[str, Any]]:
    """The index docs an indexing pipeline writes to: a Lucene IndexingFilter's doc, or the Elastic Index docs whose
    index name its Elasticsearch filter writes."""
    from tools.indexing import writes_to
    from tools.pipelines import merge_layers
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    properties = {(q['element'], q['name']): q['value'] for q in merged['properties']}
    out = []
    for (_, name), held in properties.items():
        value = held.get('value') if isinstance(held, dict) and 'value' in held else held
        if name == 'index' and isinstance(value, dict) and value.get('uuid'):
            out.append({'type': 'Index', 'uuid': value['uuid'], 'name': value.get('name')})
    if any(name == 'indexName' for _, name in properties):
        for v in (await stroom.find_documents('*', ['ElasticIndex'], 200)).get('values') or []:
            ref = v['docRef']
            if ref.get('type') != 'ElasticIndex':
                continue
            doc = await stroom.get_doc('ElasticIndex', ref['uuid'])
            if writes_to(properties, 'ElasticIndex', ref['uuid'], doc.get('indexName')):
                out.append({'type': 'ElasticIndex', 'uuid': ref['uuid'], 'name': ref.get('name'),
                            'index_name': doc.get('indexName'), 'doc': doc})
    return out


async def _index_fields(stroom, index: dict[str, Any]) -> list[dict[str, Any]]:
    """The index's fields as Stroom has them, with Elasticsearch's own type (keyword, text, ip...) where the Elastic
    Index doc lists it, and a Lucene field's analyzer."""
    from tools.indexing import _index_fields as listed
    ref = {k: index[k] for k in ('type', 'uuid', 'name')}
    fields = await listed(stroom, index['type'], ref)
    if index['type'] == 'ElasticIndex':
        native = {f.get('fldName'): (f.get('nativeType') or '').lower()
                  for f in (index.get('doc') or {}).get('fields') or [] if f.get('fldName')}
        for f in fields:
            if native.get(f['name']):
                f['elasticsearch_type'] = native[f['name']]
    else:
        found = await stroom.post('/index/v2/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 2000}})
        listed_fields = {f.get('fldName'): f for f in found.get('values') or []}
        for f in fields:
            held = listed_fields.get(f['name']) or {}
            if held.get('analyzerType'):
                f['analyzer'] = held['analyzerType']
            if held.get('caseSensitive') is False:
                f['case_sensitive'] = False     # seen: the plan's Lucene fields, where EQUALS Bob finds bob
    return fields


def _event_kinds(payload: dict[str, Any]) -> list[str]:
    """What the events pipeline writes, a line a rule: its name, the action element, and when it applies."""
    from utils.fielddoc import condition_text
    from utils.xsltgen import TranslationMapping
    try:
        mapping = TranslationMapping.model_validate(payload)
    except Exception:
        return []
    out = []
    for rule in mapping.events:
        when = ' and '.join(condition_text(c) for c in rule.when) or 'any other record'
        if rule.drop:
            out.append(f"{rule.name}: left untranslated ({when})")
            continue
        actions = {f.path.strip('/').split('/')[1] for f in [*mapping.common, *rule.fields]
                   if f.path.strip('/').startswith('EventDetail/') and f.path.strip('/').count('/') >= 1
                   and f.path.strip('/').split('/')[1] not in ('TypeId', 'Description')}
        out.append(f"{rule.name}: {', '.join(sorted(actions)) or 'EventDetail'} ({when})")
    return out


async def describe_feed(
        ctx: Context,
        feed: Annotated[str, Field(description="The feed's name, as the user gives it (case does not matter).")],
        field: Annotated[str | None, Field(description=(
            "A field to explain, as the user names it: an index field (User.DomainName, UserId), an event-logging "
            "path (EventSource/User/Id) or a source field. Omit for an overview of the feed."))] = None,
) -> dict[str, Any]:
    """
    What a feed holds and, with field, what one of its fields means and how to search it: for answering questions
    about the data (e.g. "what is User.DomainName, and how do I find domains ending in example.com?"), not for
    building. Read only. From the documentation generated for the feed's pipelines and indexes (the events
    pipeline's kinds of event, purpose and field mapping; each index's fields with their descriptions and the
    event-logging path each comes from) and the indexes' own field lists with their Elasticsearch or Lucene types,
    which give how a field can be searched through Stroom. Each doc comes with its link, to give the user.
    """
    from tools.builds import _folder_of, _path, _shape_stage, kept_mapping
    from tools.coverage import follow_on_pipelines
    from tools.feeds import feeds_named
    from security.guard import guard_from
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    named = await feeds_named(stroom, feed.strip())
    if not named:
        near = [v['docRef']['name'] for v in (await stroom.find_documents(feed.strip(), ['Feed'], 10)).get('values') or []
                if v['docRef'].get('type') == 'Feed']
        raise ToolError(f"No feed named '{feed}'" + (f"; feeds with a name like it: {', '.join(near)}" if near else
                                                       "; find_documents type=Feed lists them"))
    feed_doc = await stroom.get_doc('Feed', named[0]['uuid'])
    name = feed_doc.get('name') or named[0]['name']
    out: dict[str, Any] = {'feed': {'name': name, 'uuid': feed_doc.get('uuid'),
                                    **{k: feed_doc[k] for k in ('description', 'classification', 'streamType', 'status')
                                       if feed_doc.get(k)}}}

    # The feed's pipelines: those reading its raw data, then those reading their Events (indexing, CEF).
    pipelines = await _pipelines_reading(stroom, name)
    for p in [p for p in pipelines if not p['reads_events']]:
        for follow in await follow_on_pipelines(ctx, p, {name}):
            if all(q['uuid'] != follow['uuid'] for q in pipelines):
                pipelines.append({'uuid': follow['uuid'], 'name': follow['name'], 'reads_events': True})
    described, indexes, kinds, purposes, docs_text = [], [], [], [], []
    for p in pipelines:
        try:
            pipeline = await stroom.get_doc('Pipeline', p['uuid'])
        except ToolError:
            continue
        kept = await kept_mapping(ctx, p['uuid'])
        # The plan kept with its XSLT says what it is; an XSLT written by hand, the pipeline's elements.
        kind = (kept or {}).get('kind') or await _shape_stage(stroom, p['uuid'])
        kind = {'translation': 'events', 'index': 'indexing', 'cef': 'CEF output', 'forwarding': 'CEF output',
                'discovery': 'discovery index'}.get(kind, kind)
        entry: dict[str, Any] = {'pipeline': pipeline.get('name'), 'uuid': p['uuid'], 'kind': kind}
        if kept and kept['kind'] == 'translation':
            kinds += _event_kinds(kept['payload'])
        doc_ref = await _generated_doc(stroom, guard, pipeline['name'], await _folder_of(stroom, {**pipeline, 'type': 'Pipeline'}))
        text = doc_ref and await _text(stroom, doc_ref['uuid'])
        if text is not None and doc_ref:
            entry['documentation'] = {'name': doc_ref['name'], 'link': doc_link(stroom.settings, 'Documentation', doc_ref['uuid']),
                                      'generated': doc_ref['generated']}
            docs_text.append((doc_ref['name'], text))
            purpose = section(text, 'Purpose and data')
            if purpose:
                purposes.append({'doc': doc_ref['name'], 'text': purpose[:PURPOSE_CHARS]
                                 + (' ...' if len(purpose) > PURPOSE_CHARS else '')})
        if kind in ('indexing', 'discovery index'):
            try:
                for index in await _indexes_written(ctx, stroom, p['uuid']):
                    if all(i['uuid'] != index['uuid'] for i in indexes):
                        indexes.append(index)
            except ToolError:
                pass
        described.append(entry)
    out['pipelines'] = described
    if kinds:
        out['event_kinds'] = kinds
    if purposes:
        out['purpose'] = purposes

    index_out = []
    sources: list[str] = []
    field_hits: list[dict[str, Any]] = []
    for index in indexes:
        entry = {'name': index['name'], 'uuid': index['uuid'],
                 'backend': 'elasticsearch' if index['type'] == 'ElasticIndex' else 'lucene',
                 **({'index_name': index['index_name']} if index.get('index_name') else {})}
        doc_ref = await _generated_doc(stroom, guard, index['name'], await _folder_of(stroom, index))
        text = doc_ref and await _text(stroom, doc_ref['uuid'])
        if text is not None and doc_ref:
            entry['documentation'] = {'name': doc_ref['name'], 'link': doc_link(stroom.settings, 'Documentation', doc_ref['uuid']),
                                      'generated': doc_ref['generated']}
            docs_text.append((doc_ref['name'], text))
        try:
            fields = await _index_fields(stroom, index)
        except ToolError as e:
            entry['fields_error'] = f"Stroom could not list the index's fields: {e}"
            fields = []
        entry['fields'] = len(fields)
        if field:
            want = norm(field)
            same = [f for f in fields if norm(f['name']) == want]
            for f in same:
                subs = [g for g in fields if g['name'].lower().startswith(f['name'].lower() + '.')]
                kind = f.get('elasticsearch_type') or f['type']
                hit = {'index': index['name'], 'field': f['name'], 'type': kind,
                       **({'analyzer': f['analyzer']} if f.get('analyzer') else {}),
                       **({'sub_fields': [f"{g['name']} ({g.get('elasticsearch_type') or g['type']})" for g in subs]} if subs else {}),
                       'how_to_search': search_advice(entry['backend'], kind, f.get('analyzer', ''),
                                                      f.get('case_sensitive', True))}
                for sub in subs:
                    if (sub.get('elasticsearch_type') or sub['type']) == 'keyword' and kind == 'text':
                        hit['how_to_search'].append(f"Its keyword sub-field {sub['name']}: "
                                                    + ' '.join(search_advice(entry['backend'], 'keyword')))
                        break
                field_hits.append(hit)
            if not same:
                like = similar(field, [f['name'] for f in fields])
                if like:
                    entry['similar_fields'] = like
        index_out.append(entry)
    if index_out:
        out['indexes'] = index_out

    if not described and not docs_text:
        # No processor filter names the feed: generated docs whose names hold it (pipelines are named after their feed).
        for v in (await stroom.find_documents(f'tag:{GENERATED} {name}', ['Documentation'], 10)).get('values') or []:
            ref = v['docRef']
            text = await _text(stroom, ref['uuid'])
            if text is None or _path(v.get('path')).startswith(f'System/{guard.workspace}'):
                continue
            docs_text.append((ref['name'], text))
            out.setdefault('documentation', []).append({'name': ref['name'], 'link': doc_link(stroom.settings, 'Documentation', ref['uuid'])})

    if field:
        # Index docs first: they give the field's source path, which then finds it in the events doc.
        for doc_name, text in docs_text:
            for row in rows_about(text, field, []):
                for key, cell in row.items():
                    if key.startswith('From'):
                        sources += [s for s in _named(cell) if '/' in s]
        rows = []
        about: dict[str, Any] = {'name': field}
        paths = list(sources)
        for doc_name, text in docs_text:
            for row in rows_about(text, field, sources):
                rows.append({'doc': doc_name, **{k: v for k, v in row.items() if v}})
                # An events doc's row is the path itself (an index doc written by hand has no From column).
                for key, cell in row.items():
                    if key in ('XPath', 'EventDetail'):
                        paths += [('EventDetail/' if key == 'EventDetail' else '') + p
                                  for line in cell.splitlines() for p in _named(line)[:1] if '/' in p]
            said = mentions(text, field)
            if said:
                about.setdefault('mentioned', []).append({'doc': doc_name, 'lines': said[:6]})
        if field_hits:
            about['in_indexes'] = field_hits
        if paths:
            about['event_logging_paths'] = list(dict.fromkeys(paths))
        if rows:
            about['documented'] = rows[:ROWS_SHOWN]
            if len(rows) > ROWS_SHOWN:
                about['note'] = f"{len(rows) - ROWS_SHOWN} more rows in the docs: give the user the links"
        if not field_hits and not rows and not about.get('mentioned'):
            about['found'] = False
            about['hint'] = (f"Neither the feed's documentation nor its indexes name '{field}'. Check the spelling "
                             f"against similar_fields, or ask the user where they saw it.")
        out['field'] = about
    if not described and not docs_text:
        out['note'] = (f"No pipeline's processor filter reads '{name}', and no generated documentation is named after "
                       f"it: what the feed holds is not documented yet.")
    out['hint'] = ("Answer the user in plain words from this: what the feed is, the kinds of event it has, and for a "
                   "field its meaning (the documented rows' descriptions), where it comes from (the event-logging path "
                   "and the source field), and how to search it (how_to_search, with the user's value put in). Give "
                   "the documentation links for more.")
    return out


ALL_TOOLS = [describe_feed]
