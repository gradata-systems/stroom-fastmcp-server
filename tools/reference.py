"""Reference data an environment already loads: the maps stroom:lookup() can read, where they come from, and the
loader pipelines that serve them. Reference-data content is written with the ordinary tools: a feed of stream
type Raw Reference (create_feed), a child of the reference-data template (create_pipeline, stage=reference) with
an XSLT from build_reference_xslt, and the events pipeline naming the feed as a pipeline reference."""
import re
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field

from tools.pipelines import merge_layers
from tools.templates import _shape
from utils.stroom import gateway_from

REF_NS = 'reference-data:2'
XSL = 'http://www.w3.org/1999/XSL/Transform'
LOADER_TYPE = 'ReferenceDataFilter'


def _text_or_select(node: etree._Element) -> str:
    """A <map> or <key> element's content as the reader would say it: literal text, or the select expression."""
    literal = (node.text or '').strip()
    if literal:
        return literal
    for child in node.iter(f'{{{XSL}}}value-of', f'{{{XSL}}}sequence'):
        return child.get('select') or ''
    return ''.join(node.itertext()).strip()


def maps_in_xslt(xslt: str) -> list[dict[str, Any]]:
    """The reference maps an XSLT writes: for each <reference>, the map name (literal, or the expression it
    comes from), the key expression and the value's shape (text, or its element names)."""
    try:
        root = etree.fromstring(xslt.encode('utf-8'))
    except etree.XMLSyntaxError:
        return []
    found: dict[str, dict[str, Any]] = {}
    for reference in root.iter(f'{{{REF_NS}}}reference'):
        name_node = reference.find(f'{{{REF_NS}}}map')
        key_node = reference.find(f'{{{REF_NS}}}key')
        value_node = reference.find(f'{{{REF_NS}}}value')
        name = _text_or_select(name_node) if name_node is not None else '?'
        name = re.sub(r"^'(.*)'$", r'\1', name)
        elements = []
        if value_node is not None:
            for child in value_node.iter():
                if child is value_node or not isinstance(child.tag, str):
                    continue
                if child.tag == f'{{{XSL}}}element':
                    elements.append(child.get('name'))
                elif not child.tag.startswith(f'{{{XSL}}}'):
                    elements.append(etree.QName(child).localname)
        entry = found.setdefault(name, {'map': name, 'key': _text_or_select(key_node) if key_node is not None else '',
                                        'value': 'text', 'ranges': False})
        if elements:
            entry['value'] = sorted(set(elements))
        if reference.find(f'{{{REF_NS}}}range') is not None:
            entry['ranges'] = True
    return list(found.values())


async def find_reference_data(ctx: Context) -> dict[str, Any]:
    """
    The reference maps this environment loads, for stroom:lookup() in a translation (a mapping's `lookup`):
    each map's name, key and value shape, the pipeline and feed(s) that load it, the loader pipeline to name
    as a pipeline reference, and which pipelines already use that feed. Also the loader pipelines themselves
    (pipelines with a ReferenceDataFilter, whatever they're called) and the template for new reference-data
    pipelines (by its output). New reference data: create_feed (stream_type 'Raw Reference'), build_reference_xslt,
    create_pipeline from that template.
    """
    stroom = gateway_from(ctx)
    # XSLTs that write reference data, then the pipelines that run each (their JSON names the XSLT's uuid).
    hits = await stroom.post('/explorer/v2/findInContent', {
        'filter': {'matchType': 'CONTAINS', 'pattern': REF_NS, 'caseSensitive': True},
        'pageRequest': {'offset': 0, 'length': 200}})
    xslts = {}
    for value in hits.get('values') or []:
        ref = (value.get('docContentMatch') or {}).get('docRef') or {}
        if ref.get('type') == 'XSLT' and ref.get('uuid') not in xslts:
            xslts[ref['uuid']] = {'uuid': ref['uuid'], 'name': ref.get('name'), 'path': value.get('path')}
    filters = await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})
    feeds_by_pipeline: dict[str, set[str]] = {}
    for row in filters.get('values') or []:
        pf = row.get('processorFilter')
        if pf and not pf.get('deleted'):
            terms = ((pf.get('queryData') or {}).get('expression') or {}).get('children') or []
            feeds_by_pipeline.setdefault(pf.get('pipelineUuid'), set()).update(
                t.get('value') for t in terms if t.get('field') == 'Feed' and t.get('value'))
    maps: dict[str, dict[str, Any]] = {}
    for xslt in xslts.values():
        doc = await stroom.get(f"/xslt/v1/{xslt['uuid']}")
        written = maps_in_xslt(doc.get('data') or '')
        if not written:
            continue
        users = await stroom.post('/explorer/v2/findInContent', {
            'filter': {'matchType': 'CONTAINS', 'pattern': xslt['uuid'], 'caseSensitive': True},
            'pageRequest': {'offset': 0, 'length': 50}})
        pipelines = [(v.get('docContentMatch') or {}).get('docRef') for v in users.get('values') or []
                     if ((v.get('docContentMatch') or {}).get('docRef') or {}).get('type') == 'Pipeline']
        for entry in written:
            m = maps.setdefault(entry['map'], {**entry, 'xslt': xslt['name'], 'loaded_by': [], 'feeds': []})
            for p in pipelines:
                if p['name'] not in m['loaded_by']:
                    m['loaded_by'].append(p['name'])
                m['feeds'] += sorted(feeds_by_pipeline.get(p['uuid'], set()) - set(m['feeds']))
    # Loader pipelines, and the (feed, loader) pairs pipelines already reference.
    loaders = []
    referencing = await stroom.post('/explorer/v2/findInContent', {
        'filter': {'matchType': 'CONTAINS', 'pattern': 'pipelineReference', 'caseSensitive': True},
        'pageRequest': {'offset': 0, 'length': 200}})
    used: dict[str, dict[str, Any]] = {}
    for value in referencing.get('values') or []:
        ref = (value.get('docContentMatch') or {}).get('docRef') or {}
        if ref.get('type') != 'Pipeline':
            continue
        merged = merge_layers(await stroom.pipeline_layers(ref['uuid']))
        for r in merged['references']:
            feed = (r.get('feed') or {}).get('name')
            loader = r.get('pipeline') or {}
            entry = used.setdefault(feed, {'feed': feed, 'loader': loader.get('name'), 'loader_uuid': loader.get('uuid'),
                                           'used_by': []})
            if ref['name'] not in entry['used_by']:
                entry['used_by'].append(ref['name'])
            if loader.get('uuid') and loader['uuid'] not in {l['uuid'] for l in loaders}:
                loaders.append({'uuid': loader['uuid'], 'name': loader.get('name')})
    for m in maps.values():
        m['used_by'] = sorted({u for feed in m['feeds'] for u in (used.get(feed) or {}).get('used_by', [])})
        m['loader'] = next(((used.get(feed) or {}).get('loader') for feed in m['feeds'] if used.get(feed)), None)
    # Loaders and the template by what they are, not what they're called (no 'Reference Loader' or 'Reference
    # Data' is assumed to exist): a complete pipeline with a ReferenceDataFilter loads reference data; a template whose
    # output is reference data makes it.
    for loader in await loader_pipelines(stroom):
        if loader['uuid'] not in {l['uuid'] for l in loaders}:
            loaders.append(loader)
    from tools.templates import find_pipeline_templates
    found = (await find_pipeline_templates(ctx, 'reference'))['candidates']
    template = ({k: found[0][k] for k in ('uuid', 'name', 'path', 'child_must_supply')} if found else None)
    return {'maps': sorted(maps.values(), key=lambda m: m['map']),
            'references_in_use': sorted(used.values(), key=lambda u: u['feed'] or ''),
            'loaders': loaders, 'reference_data_template': template,
            'hint': ("A mapping entry {lookup: {map, field}} reads a map; the pipeline must name the map's feed and loader "
                     "as a reference (create_pipeline references=[{feed, loader_pipeline}] or update_pipeline (references=...)). "
                     "No map for what you need: build the reference data (create_feed stream_type='Raw Reference', "
                     "upload_sample stream_type='Raw Reference', build_reference_xslt, create_pipeline from the "
                     "reference-data template (reference_data_template; find_pipeline_templates stage=reference), "
                     "process, wait_for_processing), or keep a small "
                     "static table in a Dictionary (save_dictionary) and use `dictionary` in the mapping.")}


async def loader_pipelines(stroom) -> list[dict[str, Any]]:
    """Pipelines that load reference data: a ReferenceDataFilter reading streams as they are (no XSLT left for a
    child to set), whatever they're called and wherever they are."""
    found = await stroom.post('/explorer/v2/findInContent', {
        'filter': {'matchType': 'CONTAINS', 'pattern': 'ReferenceDataFilter', 'caseSensitive': True},
        'pageRequest': {'offset': 0, 'length': 200}})
    out: list[dict[str, Any]] = []
    for value in found.get('values') or []:
        ref = (value.get('docContentMatch') or {}).get('docRef') or {}
        if ref.get('type') != 'Pipeline' or ref.get('uuid') in {o['uuid'] for o in out}:
            continue
        try:
            shape = await _shape(stroom, ref['uuid'])
        except ToolError:
            continue    # Stroom can't build it (seen: a StateFilter this Stroom lacks): no loader to use
        # A loader reads Reference streams as they are: no XSLT left for a child (a parser's text converter may be
        # unset; Stroom's own loader leaves its CombinedParser's empty).
        if shape['stage'] == 'loader' and not any(s['property'] == 'xslt' for s in shape['child_must_supply']):
            out.append({'uuid': ref['uuid'], 'name': ref.get('name')})
    return out


async def resolve_loader(stroom, feed: str) -> str:
    """The loader for a reference feed, when the agent names none: the one other pipelines already load that feed
    with, else the environment's only loader. Several, and none in use: the agent asks which."""
    referencing = await stroom.post('/explorer/v2/findInContent', {
        'filter': {'matchType': 'CONTAINS', 'pattern': 'pipelineReference', 'caseSensitive': True},
        'pageRequest': {'offset': 0, 'length': 200}})
    for value in referencing.get('values') or []:
        ref = (value.get('docContentMatch') or {}).get('docRef') or {}
        if ref.get('type') != 'Pipeline':
            continue
        for r in merge_layers(await stroom.pipeline_layers(ref['uuid']))['references']:
            if (r.get('feed') or {}).get('name') == feed and (r.get('pipeline') or {}).get('uuid'):
                return r['pipeline']['uuid']
    loaders = await loader_pipelines(stroom)
    if len(loaders) == 1:
        return loaders[0]['uuid']
    raise ToolError(f"Which pipeline loads reference feed {feed}? "
                    + (f"This Stroom has several loaders: {', '.join(l['name'] for l in loaders)}; name one as "
                       f"loader_pipeline." if loaders else
                       "This Stroom has no reference-data loader (a pipeline with a ReferenceDataFilter): ask the user."))


ALL_TOOLS = [find_reference_data]
