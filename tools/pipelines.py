"""Tools that read and describe Stroom pipelines."""
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from utils.stroom import gateway_from

PipelineUuid = Annotated[str, Field(description="UUID of the pipeline, e.g. from find_documents.")]


def _doc_ref(ref: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ref:
        return None
    return {'type': ref.get('type'), 'uuid': ref.get('uuid'), 'name': ref.get('name')}


def _value(value: dict[str, Any] | None) -> Any:
    if not value:
        return None
    if value.get('entity'):
        return _doc_ref(value['entity'])
    for kind in ('string', 'integer', 'long', 'boolean'):
        if value.get(kind) is not None:
            return value[kind]
    return None


def _changes(data: dict[str, Any], section: str) -> tuple[list[dict], list[dict]]:
    part = data.get(section) or {}
    return part.get('add') or [], part.get('remove') or []


def merge_layers(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply pipeline layers, root template first, into the effective pipeline.

    Each layer removes then adds elements, links, properties and pipeline references, as Stroom
    does when it builds a pipeline. Property values are keyed by (element, name), so a child
    overrides its parent's value. Removals made by the last layer (the pipeline itself) are kept,
    because they show where it departs from its template.
    """
    elements: dict[str, str] = {}
    links: list[tuple[str, str]] = []
    properties: dict[tuple[str, str], Any] = {}
    references: list[dict[str, Any]] = []
    own_removals: dict[str, list] = {}

    for index, layer in enumerate(layers):
        data = layer.get('pipelineData') or {}
        source = _doc_ref(layer.get('sourcePipeline'))

        added, removed = _changes(data, 'elements')
        for element in removed:
            elements.pop(element['id'], None)
        for element in added:
            elements[element['id']] = element['type']

        added, removed = _changes(data, 'links')
        for link in removed:
            if (link['from'], link['to']) in links:
                links.remove((link['from'], link['to']))
        for link in added:
            if (link['from'], link['to']) not in links:
                links.append((link['from'], link['to']))

        added, removed = _changes(data, 'properties')
        for prop in removed:
            properties.pop((prop['element'], prop['name']), None)
        for prop in added:
            properties[(prop['element'], prop['name'])] = {'value': _value(prop.get('value')), 'from': source}

        added, removed = _changes(data, 'pipelineReferences')
        key = lambda r: (r['element'], r['name'], (r.get('pipeline') or {}).get('uuid'), (r.get('feed') or {}).get('uuid'))
        removed_keys = {key(r) for r in removed}
        references = [r for r in references if key(r) not in removed_keys]
        for ref in added:
            references.append(ref)

        if index == len(layers) - 1:
            own_removals = {
                section: [r for r in _changes(data, section)[1]]
                for section in ('elements', 'links', 'properties', 'pipelineReferences')
                if _changes(data, section)[1]
            }

    # Links whose ends no longer exist are dropped, as Stroom does.
    links = [(a, b) for a, b in links if a in elements and b in elements]
    return {
        'elements': [{'id': eid, 'type': etype} for eid, etype in elements.items()],
        'links': [{'from': a, 'to': b} for a, b in links],
        'properties': [{'element': e, 'name': n, **v} for (e, n), v in properties.items()],
        'references': [{'element': r['element'], 'name': r['name'], 'pipeline': _doc_ref(r.get('pipeline')),
                        'feed': _doc_ref(r.get('feed')), 'stream_type': r.get('streamType')} for r in references],
        'removed_by_this_pipeline': own_removals,
    }


def own_elements(layers: list[dict[str, Any]]) -> set[str]:
    """Elements the pipeline itself adds or configures, as opposed to ones inherited unchanged.

    Errors in these are the pipeline's own doing; errors in the rest come from its template.
    """
    if not layers:
        return set()
    data = layers[-1].get('pipelineData') or {}
    added, _ = _changes(data, 'elements')
    props, _ = _changes(data, 'properties')
    refs, _ = _changes(data, 'pipelineReferences')
    return {e['id'] for e in added} | {p['element'] for p in props} | {r['element'] for r in refs}


def chain_order(elements: list[dict[str, Any]], links: list[dict[str, str]]) -> list[str]:
    """Element ids in processing order, following links from the element nothing feeds into."""
    targets = {link['to'] for link in links}
    children: dict[str, list[str]] = {}
    for link in links:
        children.setdefault(link['from'], []).append(link['to'])
    order: list[str] = []
    stack = [e['id'] for e in elements if e['id'] not in targets][::-1]
    while stack:
        current = stack.pop()
        if current in order:
            continue
        order.append(current)
        stack.extend(reversed(children.get(current, [])))
    return order


async def describe_pipeline(uuid: PipelineUuid, ctx: Context) -> dict[str, Any]:
    """
    Describe a pipeline as Stroom runs it: its template chain (root first), the effective element
    chain in processing order, every property value and which layer set it, reference data loaders,
    and what this pipeline removes from its template (e.g. a decoration step it bypasses).
    Use this before copying or changing a pipeline, so the copy keeps the same structure.
    """
    stroom = gateway_from(ctx)
    doc = await stroom.get(f'/pipeline/v1/{uuid}')
    layers = await stroom.pipeline_layers(uuid)
    merged = merge_layers(layers)
    return {
        'pipeline': {'uuid': uuid, 'name': doc.get('name'), 'description': doc.get('description') or None},
        'template_chain': [_doc_ref(layer.get('sourcePipeline')) for layer in layers],
        'chain': chain_order(merged['elements'], merged['links']),
        **merged,
    }


ALL_TOOLS = [describe_pipeline]
