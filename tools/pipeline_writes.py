"""Tools that create and change pipelines in a build."""
import copy
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from security.guard import copy_of_tag, guard_from
from tools.pipelines import merge_layers
from utils.consent import consent_from
from utils.stroom import StroomGateway, gateway_from

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed.")]
# Document types a pipeline owns: copied with it, rather than shared with the original.
OWNED_TYPES = {'XSLT', 'TextConverter'}


class PropertyValue(BaseModel):
    element: str = Field(description="Element id, e.g. 'translationFilter'.")
    name: str = Field(description="Property name, e.g. 'xslt', 'textConverter', 'index', 'indexName'.")
    doc_uuid: str | None = Field(None, description="For document properties: the document's UUID.")
    doc_type: str | None = Field(None, description="For document properties: e.g. 'XSLT', 'TextConverter', 'Index'.")
    value: str | int | bool | None = Field(None, description="For plain properties: the value.")


async def _value(stroom: StroomGateway, prop: PropertyValue) -> dict[str, Any]:
    if prop.doc_uuid:
        if not prop.doc_type:
            raise ToolError(f"{prop.element}.{prop.name}: give doc_type with doc_uuid")
        doc = await stroom.get_doc(prop.doc_type, prop.doc_uuid)
        return {'entity': {'type': prop.doc_type, 'uuid': prop.doc_uuid, 'name': doc.get('name')}}
    if isinstance(prop.value, bool):
        return {'boolean': prop.value}
    if isinstance(prop.value, int):
        return {'integer': prop.value}
    if prop.value is None:
        raise ToolError(f"{prop.element}.{prop.name}: give value or doc_uuid")
    return {'string': prop.value}


def _set_property(data: dict[str, Any], element: str, name: str, value: dict[str, Any]) -> None:
    props = data.setdefault('properties', {})
    adds = [p for p in props.get('add') or [] if (p['element'], p['name']) != (element, name)]
    props['add'] = adds + [{'element': element, 'name': name, 'value': value}]


async def create_pipeline(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Pipeline name following the environment's convention.")],
        template_uuid: Annotated[str, Field(description="Parent template, from find_pipeline_templates.")],
        properties: Annotated[list[PropertyValue], Field(
            description="What the child supplies, e.g. translationFilter.xslt and dsParser.textConverter.")],
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Create a new pipeline as a child of a template, setting only what the child supplies. It keeps the
    template's structure and defaults (including any optional steps such as an empty decoration XSLT).
    The user confirms the template and name first.
    """
    stroom = gateway_from(ctx)
    template = await stroom.get_doc('Pipeline', template_uuid)
    elements = {e['id'] for e in merge_layers(await stroom.pipeline_layers(template_uuid))['elements']}
    unknown = sorted({p.element for p in properties} - elements)
    if unknown:
        raise ToolError(f"The template has no element(s) {unknown}; its elements are {sorted(elements)}")
    details = {'build': build, 'pipeline name': name, 'template': template.get('name'),
               'sets': [f'{p.element}.{p.name}' for p in properties]}
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'create_pipeline',
                                           f"Create pipeline '{name}' from template '{template.get('name')}'",
                                           details, confirmation_id)
    if gate:
        return gate
    ref = await guard_from(ctx).create('Pipeline', name, build)
    doc = await stroom.get_doc('Pipeline', ref['uuid'])
    doc['parentPipeline'] = {'type': 'Pipeline', 'uuid': template_uuid, 'name': template.get('name')}
    data: dict[str, Any] = {}
    for prop in properties:
        _set_property(data, prop.element, prop.name, await _value(stroom, prop))
    doc['pipelineData'] = data
    doc = await stroom.put_doc(doc)
    return {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': doc['name'], 'template': template.get('name')}


async def copy_pipeline(
        ctx: Context,
        build: Build,
        source_uuid: Annotated[str, Field(description="Pipeline to copy.")],
        new_name: Annotated[str, Field(description="Name of the copy, e.g. 'Keycloak-V1.3-Events'.")],
        rename: Annotated[dict[str, str] | None, Field(
            description="Text replacements applied to the names of the copied XSLTs and text converters, "
                        "e.g. {'V1.2': 'V1.3'}.")] = None,
        set_properties: Annotated[list[PropertyValue], Field(
            description="Properties to change on the copy, e.g. elasticIndexingFilter.indexName for a new "
                        "index version.")] = [],
        working_copy: Annotated[bool, Field(
            description="True when the copy will be written back over the original on promotion (an in-place "
                        "change); False for a new version that is promoted alongside it.")] = False,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Copy a pipeline into the build with exactly the original's structure and settings: same parent template,
    same element changes (removed or re-linked steps, extra XSLT steps), reference loaders and property
    values. The XSLTs and text converters it owns are copied too and the copy is rewired to them; other
    documents (shared libraries, indexes, clusters) stay shared. The user confirms the names first.
    """
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    source = await stroom.get_doc('Pipeline', source_uuid)
    data = copy.deepcopy(source.get('pipelineData') or {})
    owned = [p['value']['entity'] for p in (data.get('properties') or {}).get('add') or []
             if (p.get('value') or {}).get('entity', {}).get('type') in OWNED_TYPES]
    names = {}
    for entity in owned:
        name = entity['name']
        for old, new in (rename or {}).items():
            name = name.replace(old, new)
        names[entity['uuid']] = name
    details = {'build': build, 'copy of': source.get('name'), 'pipeline name': new_name,
               'copied documents': sorted(set(names.values())), 'working copy': working_copy,
               'changes': [f'{p.element}.{p.name}' for p in set_properties]}
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'copy_pipeline',
                                           f"Copy pipeline '{source.get('name')}' as '{new_name}'", details, confirmation_id)
    if gate:
        return gate

    copies: dict[str, dict[str, Any]] = {}
    for entity in owned:
        if entity['uuid'] in copies:
            continue
        original = await stroom.get_doc(entity['type'], entity['uuid'])
        tags = [copy_of_tag(entity['uuid'])] if working_copy else []
        ref = await guard.create(entity['type'], names[entity['uuid']], build, tags)
        doc = await stroom.get_doc(entity['type'], ref['uuid'])
        for key in ('data', 'converterType', 'description'):
            if key in original:
                doc[key] = original[key]
        doc = await stroom.put_doc(doc)
        copies[entity['uuid']] = {'type': entity['type'], 'uuid': doc['uuid'], 'name': doc['name']}
    for prop in (data.get('properties') or {}).get('add') or []:
        entity = (prop.get('value') or {}).get('entity')
        if entity and entity.get('uuid') in copies:
            prop['value'] = {'entity': copies[entity['uuid']]}
    for prop in set_properties:
        _set_property(data, prop.element, prop.name, await _value(stroom, prop))

    ref = await guard.create('Pipeline', new_name, build, [copy_of_tag(source_uuid)] if working_copy else [])
    doc = await stroom.get_doc('Pipeline', ref['uuid'])
    doc['parentPipeline'] = source.get('parentPipeline')
    doc['description'] = source.get('description')
    doc['pipelineData'] = data
    doc = await stroom.put_doc(doc)
    return {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': doc['name'], 'copy_of': source.get('name'),
            'working_copy': working_copy, 'copied_documents': list(copies.values())}


async def set_pipeline_property(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        prop: Annotated[PropertyValue, Field(description="The property to set.")],
) -> dict[str, Any]:
    """Set one element property on a pipeline this server created, e.g. schemaFilter.schemaGroup."""
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Pipeline', pipeline_uuid)
    await guard_from(ctx).check_managed({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': doc.get('name')})
    elements = {e['id'] for e in merge_layers(await stroom.pipeline_layers(pipeline_uuid))['elements']}
    if prop.element not in elements:
        raise ToolError(f"No element '{prop.element}' in this pipeline; elements are {sorted(elements)}")
    data = doc.get('pipelineData') or {}
    _set_property(data, prop.element, prop.name, await _value(stroom, prop))
    doc['pipelineData'] = data
    doc = await stroom.put_doc(doc)
    return {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': doc['name'], 'set': f'{prop.element}.{prop.name}'}


ALL_TOOLS = [create_pipeline, copy_pipeline, set_pipeline_property]
