"""Tools for finding template pipelines, how their children specialise them, and what they expect."""
import asyncio
import fnmatch
import re
import time
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.policy import DEFAULT_MARKERS, AccessPolicy, StageMarkers
from tools.pipelines import chain_order, merge_layers
from utils.stroom import StroomGateway, gateway_from

Stage = Literal['translation', 'indexing', 'discovery', 'reference']
# The property that makes each element type do something; unset means a child must supply it.
KEY_PROPERTIES = {'XSLTFilter': ('xslt',), 'DSParser': ('textConverter',), 'CombinedParser': ('textConverter',),
                  'XMLFragmentParser': ('textConverter',),
                  'IndexingFilter': ('index',), 'ElasticIndexingFilter': ('cluster', 'indexName'),
                  'SchemaFilter': ('schemaGroup',)}
_INDEXING = {'IndexingFilter': 'lucene', 'ElasticIndexingFilter': 'elasticsearch'}
_RAW_PARSERS = {'JSONParser', 'DSParser', 'CombinedParser', 'XMLFragmentParser'}
_INDEX_TTL = 300


def _path(value: str | None) -> str:
    return (value or '').replace(' / ', '/')


async def _pipeline_index(ctx: Context) -> dict[str, dict[str, Any]]:
    """Every visible pipeline with its parent and folder, cached for a few minutes.

    Stroom has no "children of" query, so this reads each pipeline doc once.
    """
    cached = ctx.lifespan_context.get('pipeline_index')
    if cached and time.monotonic() - cached[0] < _INDEX_TTL:
        return cached[1]
    stroom = gateway_from(ctx)
    found = await stroom.find_all_documents('type:Pipeline', ['Pipeline'])
    refs = [v for v in found if v['docRef'].get('type') == 'Pipeline']
    semaphore = asyncio.Semaphore(8)

    async def load(value):
        async with semaphore:
            doc = await stroom.get(f"/pipeline/v1/{value['docRef']['uuid']}")
        parent = doc.get('parentPipeline') or {}
        return value['docRef']['uuid'], {'uuid': value['docRef']['uuid'], 'name': doc.get('name'),
                                         'path': _path(value.get('path')), 'parent_uuid': parent.get('uuid')}
    index = dict(await asyncio.gather(*(load(v) for v in refs)))
    ctx.lifespan_context['pipeline_index'] = (time.monotonic(), index)
    return index


def _classify(elements: dict[str, str], properties: dict[tuple[str, str], Any],
              markers: dict[str, StageMarkers] | None = None) -> tuple[str, str | None]:
    indexers = [t for t in elements.values() if t in _INDEXING]
    if indexers:
        raw_input = any(t in _RAW_PARSERS for t in elements.values())
        return ('discovery' if raw_input else 'indexing'), _INDEXING[indexers[0]]
    if 'ReferenceDataFilter' in elements.values():
        return 'loader', None
    groups = {v for (e, n), v in properties.items() if n == 'schemaGroup'}
    types = {v for (e, n), v in properties.items() if n == 'streamType'}
    for stage, marker in (markers or DEFAULT_MARKERS).items():
        if groups & set(marker.schema_groups) or types & set(marker.stream_types):
            return stage, None
    return 'other', None


async def _shape(stroom: StroomGateway, uuid: str, markers: dict[str, StageMarkers] | None = None) -> dict[str, Any]:
    merged = merge_layers(await stroom.pipeline_layers(uuid))
    elements = {e['id']: e['type'] for e in merged['elements']}
    properties = {(p['element'], p['name']): p['value'] for p in merged['properties']}
    stage, backend = _classify(elements, properties, markers)
    chain = chain_order(merged['elements'], merged['links'])
    open_slots, shared = [], []
    for element in chain:
        etype = elements[element]
        for key in KEY_PROPERTIES.get(etype, ()):
            _slot(element, etype, key, properties.get((element, key)), open_slots, shared)
    return {'stage': stage, 'backend': backend, 'chain': chain, 'parser': elements.get(chain[0]) if chain else None,
            'child_must_supply': open_slots, 'shared': shared,
            'reference_data': [f"{(r.get('feed') or {}).get('name')} via {(r.get('pipeline') or {}).get('name')}"
                               for r in merged['references']],
            'properties': {f'{e}.{n}': v for (e, n), v in properties.items()}}


def _slot(element: str, etype: str, key: str, value: Any, open_slots: list[dict], shared: list[dict]) -> None:
    if value not in (None, ''):
        shared.append({'element': element, 'type': etype, 'property': key, 'value': value})
        return
    # Stroom treats an XSLTFilter with no XSLT as a no-op: records pass through unchanged. The first
    # unset one is where the child's translation goes; later ones (e.g. an empty decoration step) are optional.
    optional = etype == 'XSLTFilter' and any(s['type'] == 'XSLTFilter' for s in open_slots)
    open_slots.append({'element': element, 'type': etype, 'property': key,
                       **({'optional': 'passes records through unchanged if left unset'} if optional else {})})


async def find_pipeline_templates(
        ctx: Context,
        stage: Annotated[Stage, Field(description="translation (Raw Events to Events; always the first pipeline for a "
                                                  "new source), indexing (Events to an index), discovery (raw structured "
                                                  "data straight to an index) or reference (a Raw Reference feed to "
                                                  "the reference-data maps stroom:lookup() reads).")],
) -> dict[str, Any]:
    """
    Stroom pipeline templates (pipelines a new pipeline inherits from; not Elasticsearch index templates, which
    propose_index_template builds): candidates for this stage, best first: configured template folders,
    then pipelines that working pipelines already inherit from, then Stroom's standard templates. For each:
    its element chain, what a child must supply (e.g. its XSLT), shared elements it already configures
    (e.g. a decoration XSLT or an Elastic cluster), reference loaders, backend, and how many children use it.
    """
    stroom = gateway_from(ctx)
    policy: AccessPolicy = ctx.lifespan_context['policy']
    source = policy.pipeline_templates.get(stage)
    index = await _pipeline_index(ctx)
    children: dict[str, int] = {}
    for p in index.values():
        if p['parent_uuid']:
            children[p['parent_uuid']] = children.get(p['parent_uuid'], 0) + 1

    def configured(p):
        return source is not None and (any(p['path'] == f.rstrip('/') for f in source.folders)
                                       and (not source.names or any(fnmatch.fnmatchcase(p['name'], n) for n in source.names)))

    ranked = []
    for p in index.values():
        why = ('configured' if configured(p) else 'inherited_by_others' if children.get(p['uuid'], 0) >= 2
               else 'standard' if p['path'].startswith('System/Template Pipelines') else None)
        if why:
            ranked.append((why, p))
    order = {'configured': 0, 'inherited_by_others': 1, 'standard': 2}
    candidates = []
    for why, p in sorted(ranked, key=lambda x: (order[x[0]], -children.get(x[1]['uuid'], 0))):
        shape = await _shape(stroom, p['uuid'], policy.markers())
        if shape['stage'] != stage:
            continue
        candidates.append({'uuid': p['uuid'], 'name': p['name'], 'path': p['path'], 'source': why,
                           'children': children.get(p['uuid'], 0), **{k: v for k, v in shape.items() if k != 'properties'}})
    result: dict[str, Any] = {'stage': stage, 'candidates': candidates[:10]}
    if not candidates:
        result['hint'] = "No template found for this stage; ask the user which pipeline to base it on."
    elif stage == 'translation' and not any(c['parser'] in ('XMLFragmentParser', 'CombinedParser') for c in candidates):
        result['xml_fragments'] = ("No template parses XML fragments (several root elements, e.g. one <Event> per "
                                   "line). For such data, create_pipeline from the Event Data (XML) template with "
                                   "replace_parser='XMLFragmentParser' and an XML_FRAGMENT text converter on "
                                   "xmlFragmentParser.textConverter (profile_sample gives the wrapper).")
    return result


async def template_reason(ctx: Context, uuid: str) -> str | None:
    """Why a pipeline counts as a template (to inherit from, not to copy): it sits in a configured template folder
    or Stroom's standard templates, or other pipelines inherit from it. None for an ordinary pipeline."""
    policy: AccessPolicy = ctx.lifespan_context.get('policy') if isinstance(ctx.lifespan_context, dict) else None
    index = await _pipeline_index(ctx)
    entry = index.get(uuid)
    if not entry:
        return None
    if entry['path'].startswith('System/Template Pipelines'):
        return "it is one of the template pipelines"
    for stage, source in (policy.pipeline_templates.items() if policy else []):
        if any(entry['path'] == f.rstrip('/') for f in source.folders) and (
                not source.names or any(fnmatch.fnmatchcase(entry['name'], n) for n in source.names)):
            return f"it is a configured {stage} template"
    children = [p['name'] for p in index.values() if p['parent_uuid'] == uuid]
    if children:
        return f"{len(children)} pipeline(s) inherit from it ({', '.join(children[:3])}{'...' if len(children) > 3 else ''})"
    return None


async def list_template_children(
        ctx: Context,
        template_uuid: Annotated[str, Field(description="UUID of a template pipeline.")],
) -> dict[str, Any]:
    """
    Pipelines that inherit directly from a template, each with what it overrides (properties set, elements
    removed or re-linked, reference loaders added) and the feeds its processor filters cover. Shows how
    this environment specialises the template, as examples for a new child.
    """
    stroom = gateway_from(ctx)
    index = await _pipeline_index(ctx)
    kids = [p for p in index.values() if p['parent_uuid'] == template_uuid]
    filters = await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})
    feeds_by_pipeline: dict[str, set[str]] = {}
    for row in filters.get('values') or []:
        pf = row.get('processorFilter')
        if pf and not pf.get('deleted'):
            terms = ((pf.get('queryData') or {}).get('expression') or {}).get('children') or []
            names = {t.get('value') for t in terms if t.get('field') == 'Feed'}
            feeds_by_pipeline.setdefault(pf.get('pipelineUuid'), set()).update(n for n in names if n)
    result = []
    for kid in sorted(kids, key=lambda k: k['name']):
        try:
            layers = await stroom.pipeline_layers(kid['uuid'])
        except ToolError as e:
            # One pipeline Stroom cannot load (a property its element lacks, say) must not hide the others.
            result.append({'uuid': kid['uuid'], 'name': kid['name'], 'path': kid['path'], 'unreadable': str(e)[:300]})
            continue
        own = (layers[-1].get('pipelineData') or {}) if layers else {}
        props = [f"{p['element']}.{p['name']}" for p in (own.get('properties') or {}).get('add') or []]
        added = [f"{e['id']} ({e['type']})" for e in (own.get('elements') or {}).get('add') or []]
        removed = [e['id'] for e in (own.get('elements') or {}).get('remove') or []]
        relinked = [f"{l['from']} -> {l['to']}" for l in (own.get('links') or {}).get('add') or []]
        refs = [f"{(r.get('feed') or {}).get('name')} via {(r.get('pipeline') or {}).get('name')}"
                for r in (own.get('pipelineReferences') or {}).get('add') or []]
        result.append({'uuid': kid['uuid'], 'name': kid['name'], 'path': kid['path'], 'sets': props,
                       'adds_elements': added, 'removes': removed, 'adds_links': relinked, 'reference_data': refs,
                       'feeds': sorted(feeds_by_pipeline.get(kid['uuid'], set()))})
    return {'template_uuid': template_uuid, 'children': result}


_XPATH_ATTRS = ('select', 'match', 'test', 'group-by')
_NAME = re.compile(r'[A-Z][A-Za-z]+')


async def describe_template_contract(
        ctx: Context,
        template_uuid: Annotated[str, Field(description="UUID of a template pipeline.")],
) -> dict[str, Any]:
    """
    What a child's output must contain for the template's shared elements to work: the XPath expressions
    and element names that the template's own XSLTs (e.g. a decoration step) read, plus its schema group
    and output stream type. Pass this to whoever drafts the child's translation.
    """
    from lxml import etree
    stroom = gateway_from(ctx)
    try:
        shape = await _shape(stroom, template_uuid)
    except ToolError as e:
        raise ToolError(f"Stroom cannot load this pipeline template: {e}. Its stored settings are invalid in Stroom "
                        f"(e.g. a property its element type doesn't have); an administrator fixes the template in the "
                        f"Stroom UI. Nothing to set or change here: choose another pipeline template, or ask the user.") from e
    readers = []
    for slot in shape['shared']:
        if slot['type'] != 'XSLTFilter' or not isinstance(slot.get('value'), dict):
            continue
        doc = await stroom.get(f"/xslt/v1/{slot['value']['uuid']}")
        root = etree.fromstring((doc.get('data') or '').encode('utf-8'))
        expressions = sorted({node.get(a) for node in root.iter() if isinstance(node.tag, str)
                              for a in _XPATH_ATTRS if node.get(a)})
        names = sorted({n for e in expressions for n in _NAME.findall(e)})
        readers.append({'element': slot['element'], 'xslt': doc.get('name'), 'reads_elements': names,
                        'expressions': expressions[:60]})
    props = shape['properties']
    return {'template_uuid': template_uuid, 'stage': shape['stage'], 'backend': shape['backend'],
            'child_must_supply': shape['child_must_supply'], 'shared_readers': readers,
            'schema_group': props.get('schemaFilter.schemaGroup'),
            'output_stream_type': props.get('streamAppender.streamType'),
            'reference_data': shape['reference_data'],
            'hint': None if readers else "No shared XSLT reads the child's output; only the schema applies."}


async def find_similar_translations(
        ctx: Context,
        text: Annotated[str, Field(description="Text to look for in XSLT content, e.g. a vendor name, a source "
                                               "field name or an event id.")],
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """
    Find existing XSLTs whose content mentions the text, as examples of how this environment translates
    similar sources.
    """
    stroom = gateway_from(ctx)
    body = await stroom.post('/explorer/v2/findInContent', {
        'filter': {'matchType': 'CONTAINS', 'pattern': text, 'caseSensitive': False},
        'pageRequest': {'offset': 0, 'length': limit * 3}})
    matches = []
    for value in body.get('values') or []:
        match = value.get('docContentMatch') or {}
        ref = match.get('docRef') or {}
        if ref.get('type') == 'XSLT':
            matches.append({'uuid': ref.get('uuid'), 'name': ref.get('name'), 'path': value.get('path'),
                            'sample': (match.get('sample') or '')[:300]})
    if not matches and not body.get('values'):
        raise ToolError(f"No documents mention '{text}'")
    return {'text': text, 'xslts': matches[:limit]}


async def shared_xslt_usage(ctx: Context, pipelines: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """The shared XSLTs (xsl:import / xsl:include) the pipelines' own XSLTs use: for each named template called,
    where (the element it writes, below Event or the document map) and by which pipelines."""
    from tools.pipelines import translation_docs
    from utils.sharedxslt import describe, imports_of, usage
    stroom = gateway_from(ctx)
    texts: dict[str, str | None] = {}
    docs: dict[str, dict[str, Any]] = {}

    async def shared(href: str) -> str | None:
        # Stroom resolves an import's href to the XSLT document of that name: read it the same way.
        if href not in texts:
            found = [v['docRef'] for v in (await stroom.find_documents(href, ['XSLT'], 20)).get('values') or []
                     if v['docRef'].get('name') == href]
            texts[href] = (await stroom.get_doc('XSLT', found[0]['uuid'])).get('data') if found else None
            if found:
                docs[href] = {'uuid': found[0]['uuid'], 'name': href, **describe(texts[href])}
        return texts[href]
    seen: dict[tuple, dict[str, Any]] = {}
    for pipeline in pipelines[:limit]:
        try:
            layers = await stroom.pipeline_layers(pipeline['uuid'])
        except ToolError:
            continue
        for entry in translation_docs(pipeline['uuid'], layers):
            if entry['inherited_from_template'] or entry['doc'].get('type') != 'XSLT':
                continue
            text = (await stroom.get_doc('XSLT', entry['doc']['uuid'])).get('data') or ''
            hrefs = imports_of(text)
            if not hrefs:
                continue
            for use in usage(text, {h: await shared(h) for h in hrefs}):
                key = (use['href'], use.get('template'), tuple(use.get('at') or []))
                row = seen.setdefault(key, {**{k: v for k, v in use.items() if k != 'within'}, 'used_by': []})
                row['used_by'].append(pipeline['name'])
    for row in seen.values():
        if row['href'] in docs:
            row['document'] = {'uuid': docs[row['href']]['uuid'], 'name': row['href']}
    usages = sorted(seen.values(), key=lambda r: -len(r['used_by']))
    # Everything each shared document offers, called or not (describe_document reads its full text).
    return usages + [{'href': href, 'document_contents': doc} for href, doc in docs.items()]


async def describe_template(
        ctx: Context,
        template_uuid: Annotated[str, Field(description="UUID of a template pipeline (find_pipeline_templates).")],
) -> dict[str, Any]:
    """
    How this environment uses a Stroom pipeline template (not an Elasticsearch index template), before making a
    child of it: the pipelines that inherit from it,
    each with what it overrides (properties set, elements removed or re-linked, reference data added) and
    the feeds it covers, as examples; and the template's contract, what a child's output must contain for
    the shared elements (a decoration XSLT, say) to work: the elements and expressions they read, the schema
    group and the output stream type. Also the shared XSLTs (xsl:import) those pipelines' XSLTs use: each named
    template they call, where, and what it writes, to call the same way in the new XSLT.
    """
    children = await list_template_children(ctx, template_uuid)
    contract = await describe_template_contract(ctx, template_uuid)
    result = {'template_uuid': template_uuid, 'children': children['children'],
              **{k: v for k, v in contract.items() if k != 'template_uuid'}}
    shared = await shared_xslt_usage(ctx, [c for c in children['children'] if 'unreadable' not in c])
    if shared:
        result['shared_xslt'] = shared
        result['shared_xslt_hint'] = (
            "These pipelines' XSLTs call named templates from shared XSLTs. Use the same ones in the new XSLT where "
            "they are called (the mapping's or field plan's shared entries: href, template, at), and do not map the "
            "elements they write: an element written twice fails schema validation.")
    return result


ALL_TOOLS = [find_pipeline_templates, describe_template]
