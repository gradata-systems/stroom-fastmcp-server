"""Tools that create and change pipelines in a build."""
import copy
import re
from typing import Annotated, Any
from urllib.parse import quote

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from security.guard import MANAGED, build_tag, copy_of_tag, guard_from
from tools.pipelines import chain_order, merge_layers
from utils.consent import consent_from, edited
from utils.params import ONE_OR_MORE
from utils.stroom import StroomGateway, gateway_from

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed.")]
# Document types a pipeline owns: copied with it, rather than shared with the original.
OWNED_TYPES = {'XSLT', 'TextConverter'}
PARSERS = {'XMLParser', 'XMLFragmentParser', 'JSONParser', 'DSParser', 'CombinedParser'}


def element_id(element_type: str) -> str:
    """The id Stroom's templates give an element of a type: XMLFragmentParser -> xmlFragmentParser, DSParser ->
    dsParser, CombinedParser -> combinedParser (a leading acronym is lowered whole)."""
    lowered = re.sub(r'^[A-Z]+(?=[A-Z][a-z])', lambda m: m.group(0).lower(), element_type)
    return lowered[0].lower() + lowered[1:]


def swap_parser(merged: dict[str, Any], new_type: str) -> tuple[dict[str, Any], str, str]:
    """Pipeline data for a child that replaces the template's parser with one of new_type, linked where the old
    one was: (data, new element id, old element type). A child may remove and add elements as the UI does."""
    if new_type not in PARSERS:
        raise ToolError(f"replace_parser must be one of {sorted(PARSERS)}")
    types = {e['id']: e['type'] for e in merged['elements']}
    old = next((e for e in chain_order(merged['elements'], merged['links']) if types[e] in PARSERS), None)
    if old is None:
        raise ToolError("The template has no parser element to replace")
    new_id = element_id(new_type)
    outgoing = [{'from': link['from'], 'to': link['to']} for link in merged['links'] if link['from'] == old]
    data = {'elements': {'add': [{'id': new_id, 'type': new_type}], 'remove': [{'id': old, 'type': types[old]}]},
            'links': {'add': [{'from': new_id, 'to': link['to']} for link in outgoing], 'remove': outgoing}}
    return data, new_id, types[old]


class PipelineReference(BaseModel):
    """Reference data an XSLT step reads with stroom:lookup(): a feed of Reference streams, loaded by a loader
    pipeline (the standard 'Reference Loader' unless the environment has its own; find_reference_data)."""
    feed: str = Field(description="The reference feed's name.")
    loader_pipeline: str = Field('Reference Loader', description="The loader pipeline's name (or UUID).")
    element: str | None = Field(None, description="The XSLT element that does the lookups; defaults to the "
                                                  "pipeline's translation step.")


async def _doc_ref_by_name(stroom: StroomGateway, doc_type: str, name: str) -> dict[str, Any]:
    if doc_type == 'Feed':
        found = await stroom.get(f'/feed/v1/getDocRefForName/{quote(name, safe="")}')
        if not found:
            raise ToolError(f"No feed named '{name}'")
        return {'type': 'Feed', 'uuid': found['uuid'], 'name': found.get('name') or name}
    if re.fullmatch(r'[0-9a-f-]{36}', name):
        doc = await stroom.get_doc(doc_type, name)
        return {'type': doc_type, 'uuid': doc['uuid'], 'name': doc.get('name')}
    found = await stroom.find_documents(name, [doc_type], 20)
    matches = [v['docRef'] for v in found.get('values') or [] if v['docRef'].get('type') == doc_type and v['docRef'].get('name') == name]
    if len(matches) != 1:
        raise ToolError(f"{'No' if not matches else len(matches)} {doc_type} document(s) named '{name}'" +
                        ("; give the UUID" if len(matches) > 1 else " (find_reference_data lists loaders)"))
    return {'type': doc_type, 'uuid': matches[0]['uuid'], 'name': matches[0].get('name')}


async def reference_entries(stroom: StroomGateway, merged: dict[str, Any], references: list[PipelineReference]
                            ) -> list[dict[str, Any]]:
    """pipelineReferences entries for the references, on the element asked for or the first XSLT step."""
    xslt_steps = [e for e in chain_order(merged['elements'], merged['links'])
                  if {x['id']: x['type'] for x in merged['elements']}.get(e) == 'XSLTFilter']
    out = []
    for ref in references:
        element = ref.element or (xslt_steps[0] if xslt_steps else None)
        if not element:
            raise ToolError("The pipeline has no XSLT step to attach reference data to")
        out.append({'element': element, 'name': 'pipelineReference',
                    'pipeline': await _doc_ref_by_name(stroom, 'Pipeline', ref.loader_pipeline),
                    'feed': await _doc_ref_by_name(stroom, 'Feed', ref.feed), 'streamType': 'Reference'})
    return out


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


async def _template_uuid(stroom: StroomGateway, template: str | None) -> str:
    """A template given as a UUID or as its exact name."""
    if not template:
        raise ToolError("Give template_uuid (or template: the template's UUID or name), from find_pipeline_templates stage=translation")
    if re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', template):
        return template
    return (await _doc_ref_by_name(stroom, 'Pipeline', template))['uuid']


async def open_slots(stroom: StroomGateway, merged: dict[str, Any], replace_parser: str | None = None) -> list[dict[str, str]]:
    """The properties a child must supply for the template's chain to run: the parser's text converter (DSParser,
    XMLFragmentParser, CombinedParser) and the first XSLT step's xslt, where the template leaves them unset."""
    from tools.templates import KEY_PROPERTIES
    types = {e['id']: e['type'] for e in merged['elements']}
    if replace_parser:
        old = next((e for e in chain_order(merged['elements'], merged['links']) if types[e] in PARSERS), None)
        if old:
            types.pop(old, None)
        types[element_id(replace_parser)] = replace_parser
    values = {(p['element'], p['name']): p.get('value') for p in merged['properties']}
    slots, seen_xslt = [], False
    known = {e['id'] for e in merged['elements']}
    for element in chain_order(merged['elements'], merged['links']) + [e for e in types if e not in known]:
        etype = types.get(element)
        for key in KEY_PROPERTIES.get(etype, ()):
            if key == 'xslt':
                if seen_xslt:
                    continue   # a second XSLT step (decoration) is optional
                seen_xslt = True
            if key in ('xslt', 'textConverter') and values.get((element, key)) in (None, ''):
                slots.append({'element': element, 'type': etype, 'property': key})
    return slots


async def fill_open_slots(ctx: Context, build: str, merged: dict[str, Any], replace_parser: str | None,
                          properties: list[PropertyValue]) -> tuple[list[PropertyValue], list[str], list[str]]:
    """Properties with the template's open slots filled from the build's own documents when there is exactly one
    candidate: (properties, what was filled, what is still open). A parser with no converter in the build is
    refused: the pipeline could not parse anything."""
    from utils.mappingstore import read_mapping
    stroom = gateway_from(ctx)
    given = {(p.element, p.name) for p in properties}
    docs = await guard_from(ctx).folder_contents(build)
    filled, still_open = [], []
    for slot in await open_slots(stroom, merged, replace_parser):
        if (slot['element'], slot['property']) in given:
            continue
        if slot['property'] == 'textConverter':
            candidates = [d for d in docs if d['type'] == 'TextConverter']
            if not candidates:
                raise ToolError(f"The template's {slot['type']} ({slot['element']}) needs a text converter and build '{build}' has "
                                f"none: build_data_splitter with the sample (save_as=<name>), or save_text_converter, then "
                                f"create the pipeline (or pass {slot['element']}.textConverter in set_properties).")
            doc_type = 'TextConverter'
        else:
            xslts = [d for d in docs if d['type'] == 'XSLT']
            with_mapping = []
            for d in xslts:
                kept = read_mapping((await stroom.get_doc('XSLT', d['uuid'])).get('description'))
                if kept and kept[0] == 'translation':
                    with_mapping.append(d)
            candidates = with_mapping or xslts
            doc_type = 'XSLT'
            if not candidates:
                still_open.append(f"{slot['element']}.xslt")
                continue
        if len(candidates) > 1:
            names = ', '.join(f"{c['name']} ({c['uuid']})" for c in candidates)
            raise ToolError(f"{slot['element']}.{slot['property']} is not set and build '{build}' has {len(candidates)} "
                            f"{doc_type} documents ({names}): pass the right one in set_properties.")
        chosen = candidates[0]
        properties = [*properties, PropertyValue(element=slot['element'], name=slot['property'], doc_uuid=chosen['uuid'], doc_type=doc_type)]
        filled.append(f"{slot['element']}.{slot['property']} = {chosen['name']} (the build's only {doc_type})")
    return properties, filled, still_open


async def _own_documents(ctx: Context, build: str, properties: list[PropertyValue], allowed: bool) -> None:
    """The XSLT and text converter a child supplies must be the build's own (made with save_xslt /
    save_text_converter), not another source's or a library's: those are inherited from the template."""
    if allowed:
        return
    guard = guard_from(ctx)
    for prop in properties:
        if prop.name not in ('xslt', 'textConverter') or not prop.doc_uuid:
            continue
        ref = {'type': prop.doc_type, 'uuid': prop.doc_uuid}
        tags = await guard.tags(ref)
        if MANAGED not in tags or build_tag(build) not in tags:
            raise ToolError(f"{prop.element}.{prop.name}: {prop.doc_type} {prop.doc_uuid} is not a document of build "
                            f"'{build}'. A new pipeline's translation is written for its own source: "
                            f"build_translation_xslt from a mapping, saved in the build (build=, name=), and a text "
                            f"converter with build_data_splitter and save_text_converter. Shared libraries are "
                            f"xsl:imported or inherited from the template, not set on the child. If the user says this "
                            f"existing document is the right one, call again with reuse_existing_docs=true.")


async def _parser_reads_sample(ctx: Context, build: str, merged: dict[str, Any], replace_parser: str | None,
                               allowed: bool) -> None:
    """The template's parser (or the replacement) must read the format of the build's sample streams."""
    if allowed:
        return
    from tools.plan import PARSER_FOR_FORMAT, sample_format
    from tools.pipelines import chain_order
    types = {e['id']: e['type'] for e in merged['elements']}
    chain = chain_order(merged['elements'], merged['links'])
    parser = replace_parser or next((types[e] for e in chain if types[e] in PARSERS), None)
    if parser is None:
        return
    sample = await sample_format(ctx, build)
    if sample is None:
        return
    readers = PARSER_FOR_FORMAT.get(sample['format'])
    if readers and parser not in readers:
        raise ToolError(f"The build's sample (stream {sample['stream_id']} of feed {sample['feed']}) is {sample['format']}, "
                        f"which this template's {parser} cannot read; it needs {readers[0]} ({sample['suggested_parser']}). "
                        f"Choose the template with that parser (find_pipeline_templates stage=translation), or "
                        f"replace_parser for XML fragments. If the user insists on this template, call again with "
                        f"accept_parser_mismatch=true.")


async def create_pipeline(
        ctx: Context,
        name: Annotated[str, Field(description="Pipeline name following the environment's convention.")],
        template_uuid: Annotated[str | None, Field(description="Parent template, from find_pipeline_templates (its UUID; "
                                                               "`template` takes a UUID or a name too).")] = None,
        set_properties: Annotated[list[PropertyValue] | str, ONE_OR_MORE, Field(
            description="What the child supplies, e.g. translationFilter.xslt and dsParser.textConverter. May be left "
                        "for update_pipeline once the XSLT and converter are saved.")] = [],
        build: Annotated[str | None, Field(description="The build this pipeline belongs to; defaults to the build this "
                                                       "session is working on (start_onboarding / start_build).")] = None,
        template: Annotated[str | None, Field(description="The template's UUID or exact name, instead of template_uuid.")] = None,
        description: Annotated[str, Field(description="What the pipeline does, kept on the pipeline doc.")] = '',
        replace_parser: Annotated[str | None, Field(
            description="Parser element type to use instead of the template's, e.g. 'XMLFragmentParser' for XML "
                        "fragments (several root elements) when no template has one. It takes the template's "
                        "parser's place and links, with the id of its type (xmlFragmentParser), which set_properties "
                        "may address (xmlFragmentParser.textConverter).")] = None,
        references: Annotated[list[PipelineReference] | str, ONE_OR_MORE, Field(
            description="Reference data the translation looks up (the mapping's lookup entries): the feed and its "
                        "loader pipeline, from find_reference_data.")] = [],
        reuse_existing_docs: Annotated[bool, Field(
            description="Only when the user says an XSLT or text converter that already exists outside this build is "
                        "the right one for this pipeline. Otherwise the child's documents must be ones this build made.")] = False,
        accept_parser_mismatch: Annotated[bool, Field(
            description="Only when the user says to use this template although the build's sample is not in a format "
                        "its parser reads.")] = False,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Create a new pipeline as a child of a template, setting only what the child supplies. It keeps the
    template's structure and defaults (including any optional steps such as an empty decoration XSLT),
    unless replace_parser swaps the parser. References attach reference data for stroom:lookup(). It refuses
    an XSLT or text converter from outside the build (a new source gets its own, from build_translation_xslt
    and build_data_splitter) and a template whose parser cannot read the build's sample streams. The user
    confirms the template and name first.
    """
    from tools.plan import resolve_build
    stroom = gateway_from(ctx)
    build = resolve_build(ctx, build, 'create_pipeline')
    template_uuid = await _template_uuid(stroom, template_uuid or template)
    template = await stroom.get_doc('Pipeline', template_uuid)
    merged = merge_layers(await stroom.pipeline_layers(template_uuid))
    elements = {e['id'] for e in merged['elements']}
    data: dict[str, Any] = {}
    if replace_parser:
        data, new_id, old_type = swap_parser(merged, replace_parser)
        elements = (elements - {data['elements']['remove'][0]['id']}) | {new_id}
    unknown = sorted({p.element for p in set_properties} - elements)
    if unknown:
        raise ToolError(f"The pipeline has no element(s) {unknown}; its elements are {sorted(elements)}")
    _keep_validation({e['id']: e['type'] for e in merged['elements']}, set_properties)
    properties, filled, still_open = await fill_open_slots(ctx, build, merged, replace_parser, list(set_properties))
    await _own_documents(ctx, build, properties, reuse_existing_docs)
    await _parser_reads_sample(ctx, build, merged, replace_parser, accept_parser_mismatch)
    refs = await reference_entries(stroom, merged, references)
    details = {'build': build, 'pipeline name': name, 'template': template.get('name'),
               'sets': [f'{p.element}.{p.name}' for p in properties],
               **({'filled from the build': filled} if filled else {}),
               **({'still to set': still_open} if still_open else {}),
               **({'parser': f"{replace_parser} in place of the template's {old_type}"} if replace_parser else {}),
               **({'reference data': [f"{r['feed']['name']} via {r['pipeline']['name']} on {r['element']}" for r in refs]}
                  if refs else {})}
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'create_pipeline',
                                           f"Create pipeline '{name}' from template '{template.get('name')}'",
                                           details, confirmation_id, editable={'name': ('Pipeline name', name)})
    if gate:
        return gate
    name = edited(ctx, 'name', name)       # the user may have corrected it in the form
    ref = await guard_from(ctx).create('Pipeline', name, build)
    doc = await stroom.get_doc('Pipeline', ref['uuid'])
    doc['parentPipeline'] = {'type': 'Pipeline', 'uuid': template_uuid, 'name': template.get('name')}
    if description:
        doc['description'] = description
    for prop in properties:
        _set_property(data, prop.element, prop.name, await _value(stroom, prop))
    if refs:
        data.setdefault('pipelineReferences', {})['add'] = refs
    doc['pipelineData'] = data
    doc = await stroom.put_doc(doc)
    from tools.plan import with_next
    return await with_next(ctx, build, {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': doc['name'], 'template': template.get('name'),
                                        'sets': [f'{p.element}.{p.name}' for p in properties],
                                        **({'filled_from_build': filled} if filled else {}),
                                        **({'still_to_set': still_open, 'hint': f"The pipeline cannot run until {still_open} is set: "
                                            f"save the translation XSLT (build_translation_xslt build=, name=) and update_pipeline with "
                                            f"set_properties=[{{element, name: 'xslt', doc_uuid, doc_type: 'XSLT'}}]"} if still_open else {}),
                                        **({'reference_data': [f"{r['feed']['name']} via {r['pipeline']['name']}" for r in refs]} if refs else {})})


async def copy_pipeline(
        ctx: Context,
        build: Build,
        source_uuid: Annotated[str, Field(description="Pipeline to copy.")],
        new_name: Annotated[str, Field(description="Name of the copy, e.g. 'Acme-Door-V1.3-Events'.")],
        rename: Annotated[dict[str, str] | None, Field(
            description="Text replacements applied to the names of the copied XSLTs and text converters, "
                        "e.g. {'V1.2': 'V1.3'}.")] = None,
        set_properties: Annotated[list[PropertyValue] | str, ONE_OR_MORE, Field(
            description="Properties to change on the copy, e.g. elasticIndexingFilter.indexName for a new "
                        "index version.")] = [],
        working_copy: Annotated[bool, Field(
            description="True when the copy will be written back over the original on promotion (an in-place "
                        "change); False for a new version that is promoted alongside it.")] = False,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Copy an existing source's pipeline (not a template: those are inherited with create_pipeline) into the build
    with exactly the original's structure and settings: same parent template,
    same element changes (removed or re-linked steps, extra XSLT steps), reference loaders and property
    values. The XSLTs and text converters it owns are copied too and the copy is rewired to them; other
    documents (shared libraries, indexes, clusters) stay shared. The user confirms the names first.
    """
    stroom = gateway_from(ctx)
    guard = guard_from(ctx)
    source = await stroom.get_doc('Pipeline', source_uuid)
    from tools.templates import template_reason
    reason = await template_reason(ctx, source_uuid)
    if reason:
        raise ToolError(f"'{source.get('name')}' is a template ({reason}): templates are inherited, not copied. A new source's "
                        f"pipeline is a child of it: create_pipeline(name=..., template_uuid='{source_uuid}'), which keeps the "
                        f"template's structure and takes later fixes to it. copy_pipeline is for a new version or a working "
                        f"copy of a source's own pipeline (e.g. Acme-Door-V1.2-Events).")
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


VALIDATION = {'SchemaFilter'}


def _keep_validation(types: dict[str, str], props: list[Any]) -> None:
    """Refuse a property on a schema filter: validation is the template's, there to catch output that is wrong.
    When it fails, the output is what to fix (e.g. a root element that does not say which schema it follows)."""
    touched = sorted({p.element for p in props if types.get(p.element) in VALIDATION})
    if touched:
        raise ToolError(f"{touched} validate the pipeline's output against its schema; their settings are not changed "
                        f"here. Fix the output instead: an Elasticsearch indexing XSLT's root must say which JSON schema "
                        f"it follows (<array xsi:schemaLocation=\"http://www.w3.org/2005/xpath-functions "
                        f"file://xpath-functions.xsd\">), which save_xslt index_plan=... writes; an events XSLT must "
                        f"produce valid event-logging. If the template's validation itself is wrong, that is for the "
                        f"template's owner to change.")


async def set_pipeline_property(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        prop: Annotated[PropertyValue, Field(description="The property to set.")],
) -> dict[str, Any]:
    """Set one element property on a pipeline this server created, e.g. schemaFilter.schemaGroup."""
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Pipeline', pipeline_uuid)
    await guard_from(ctx).check_managed({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': doc.get('name')})
    types = {e['id']: e['type'] for e in merge_layers(await stroom.pipeline_layers(pipeline_uuid))['elements']}
    elements = set(types)
    if prop.element not in elements:
        raise ToolError(f"No element '{prop.element}' in this pipeline; elements are {sorted(elements)}")
    _keep_validation(types, [prop])
    data = doc.get('pipelineData') or {}
    _set_property(data, prop.element, prop.name, await _value(stroom, prop))
    doc['pipelineData'] = data
    doc = await stroom.put_doc(doc)
    return {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': doc['name'], 'set': f'{prop.element}.{prop.name}'}


async def set_pipeline_references(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        references: Annotated[list[PipelineReference] | str, ONE_OR_MORE, Field(description="Reference data to attach (added to any "
                                                                        "the pipeline already has).")],
) -> dict[str, Any]:
    """
    Attach reference data to a pipeline this server created, so its XSLT's stroom:lookup() calls find the maps
    (find_reference_data names feeds and loaders; the mapping's lookup entries name the maps).
    """
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Pipeline', pipeline_uuid)
    await guard_from(ctx).check_managed({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': doc.get('name')})
    merged = merge_layers(await stroom.pipeline_layers(pipeline_uuid))
    new = await reference_entries(stroom, merged, references)
    data = doc.get('pipelineData') or {}
    existing = data.setdefault('pipelineReferences', {}).setdefault('add', [])
    key = lambda r: (r['element'], r['feed']['uuid'], r['pipeline']['uuid'])
    have = {key(r) for r in existing}
    added = [r for r in new if key(r) not in have]
    existing.extend(added)
    doc['pipelineData'] = data
    doc = await stroom.put_doc(doc)
    return {'type': 'Pipeline', 'uuid': doc['uuid'], 'name': doc['name'],
            'reference_data': [f"{r['feed']['name']} via {r['pipeline']['name']} on {r['element']}" for r in existing],
            'added': len(added)}


async def update_pipeline(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="A pipeline this server created.")],
        set_properties: Annotated[list[PropertyValue] | str, ONE_OR_MORE, Field(description="Element properties to set, e.g. "
                                                                    "schemaFilter.schemaGroup or jsonParser.addRootObject.")] = [],
        references: Annotated[list[PipelineReference] | str, ONE_OR_MORE, Field(description="Reference data to attach (added to any "
                                                                        "the pipeline already has), for stroom:lookup().")] = [],
) -> dict[str, Any]:
    """
    Change a pipeline this server created: set element properties, and attach reference data (feed and loader
    pipeline, from find_reference_data) so its XSLT's lookups find the maps.
    """
    if not set_properties and not references:
        raise ToolError("Give set_properties, references, or both")
    result: dict[str, Any] = {'uuid': pipeline_uuid, 'set': [], 'reference_data': None}
    for prop in set_properties:
        outcome = await set_pipeline_property(ctx, pipeline_uuid, prop)
        result['name'], result['type'] = outcome['name'], 'Pipeline'
        result['set'].append(outcome['set'])
    if references:
        outcome = await set_pipeline_references(ctx, pipeline_uuid, references)
        result['name'], result['type'] = outcome['name'], 'Pipeline'
        result['reference_data'] = outcome['reference_data']
    return result


ALL_TOOLS = [create_pipeline, copy_pipeline, update_pipeline]
