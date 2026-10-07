"""Indexing tools for either backend: Stroom's Lucene index or Elasticsearch."""
import asyncio
import fnmatch
import logging
import re
import json
import time
import uuid as uuidlib
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from security.guard import guard_from
from tools.explorer import _redact
from tools.pipeline_writes import PropertyValue, create_pipeline
from tools.processing_writes import elastic_destination, indexing_xslt_digest
from tools.pipelines import merge_layers
from tools.stepping import _outputs, _Pipeline, remember_verified
from tools.streams import _meta, summarise_events
from tools.templates import _shape
from utils.consent import consent_from, edited
from utils.fielddoc import index_field_mapping_markdown
from utils.fieldplan import Backend, Discovery, FieldPlan, PlannedField, any_action, population_of, source_matches
from utils.xsltgen import SharedTemplate
from utils.mappingstore import read_mapping, with_agreed_template
from utils.params import ONE_OR_MORE
from utils.stroom import doc_link, gateway_from, set_body_text
from utils.sharedxslt import json_values
from utils.templatecheck import (compare, compose, from_example, json_xml_documents, names_from_example,
                                 parse_component_templates, read_mapping_fields,
                                 parse_template)

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed.")]
INDEX_TYPE = {'lucene': 'Index', 'elasticsearch': 'ElasticIndex'}


def _conventions(ctx: Context) -> dict[str, dict[str, Any]]:
    folder: Path = gateway_from(ctx).settings.conventions_dir
    out = {}
    for path in sorted(folder.glob('*.yaml')) if folder.is_dir() else []:
        profile = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        out[profile.get('name', path.stem)] = profile
    return out


logger = logging.getLogger(__name__)
_PROFILE_LABELS = {'ecs': 'ECS (Elastic Common Schema)', 'stroom-flat': 'Stroom flat'}
EXAMPLE_SUFFIX = ' example index template'
_EXAMPLE_BLOCK = re.compile(r'<!-- stroom-mcp example\n(.*?)\n-->', re.S)


async def _build_of_stream(ctx: Context, stream_id: int) -> str | None:
    """The build whose pipeline wrote an Events stream, if any."""
    from tools.plan import build_of
    try:
        found = await gateway_from(ctx).find_meta(
            [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(stream_id)}], 1)
        row = (found.get('values') or [{}])[0]
        uuid = (row.get('meta') or row).get('pipelineUuid')
        return await build_of(ctx, {'type': 'Pipeline', 'uuid': uuid, 'name': None}) if uuid else None
    except Exception:
        return None


async def _keep_example(ctx: Context, build: str, index_name: str, example: str, components: list[str]) -> None:
    """Keep the example index template the user pasted (and its components) in the build, verbatim: a summarised
    conversation lost it, and the agent proposed a template of its own as the user's example."""
    stroom, guard = gateway_from(ctx), guard_from(ctx)
    text = (f"# Example index template for {index_name}\n\nAs the user pasted it, kept so the index template follows "
            f"it even if the conversation no longer holds it.\n\n```\n{example.strip()}\n```\n"
            + ''.join(f"\n```\n{c.strip()}\n```\n" for c in components)
            + f"\n<!-- stroom-mcp example\n{json.dumps({'example': example, 'components': components})}\n-->\n")
    name = f"{index_name}{EXAMPLE_SUFFIX}"

    async def write(ref: dict[str, Any]) -> dict[str, Any]:
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        set_body_text(doc, text)
        return await stroom.put_doc(doc)
    there = next((d for d in await guard.folder_contents(build) if d['type'] == 'Documentation' and d['name'] == name), None)
    if there:
        await write(there)
    else:
        await guard.create_filled('Documentation', name, build, write)


async def _kept_example(ctx: Context, pipeline_uuid: str, index_names: list[str]) -> tuple[str, list[str]] | None:
    """The example kept in the indexing pipeline's build when the plan was drafted: (template, components)."""
    from tools.plan import build_of
    try:
        build = await build_of(ctx, {'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': None})
        if not build:
            return None
        names = {f"{n}{EXAMPLE_SUFFIX}" for n in index_names if n}
        doc = next((d for d in await guard_from(ctx).folder_contents(build)
                    if d['type'] == 'Documentation' and d['name'] in names), None)
        if not doc:
            return None
        found = _EXAMPLE_BLOCK.search((await gateway_from(ctx).get_doc('Documentation', doc['uuid'])).get('data') or '')
        kept = json.loads(found.group(1)) if found else None
        return (kept['example'], list(kept.get('components') or [])) if kept else None
    except Exception:
        return None


def _is_template(text: str) -> bool:
    try:
        parse_template(text)
        return True
    except ValueError:
        return False
_STROOM_TO_ES = {'keyword': 'keyword', 'text': 'text', 'long': 'long', 'integer': 'integer', 'id': 'long',
                  'float': 'float', 'double': 'double', 'date': 'date', 'ipv4_address': 'ip', 'boolean': 'boolean'}


async def _example_from_index(ctx: Context, index: str) -> tuple[str, str, dict[str, Any]]:
    """An existing Elastic Index doc's fields as an example index template, a note for the draft, and what was read
    (for the user's confirmation). The doc's own field list holds Elasticsearch's types (nativeType: keyword, ip,
    date...); Stroom's findFields, the fallback, only its own (KEYWORD, LONG...). Neither gives the index template
    itself: its settings, component templates, keyword sub-fields or ignore_above."""
    stroom = gateway_from(ctx)
    ref = None
    try:
        doc = await stroom.get_doc('ElasticIndex', index)
        ref = {'type': 'ElasticIndex', 'uuid': index, 'name': doc.get('name')}
    except ToolError:
        found = [v['docRef'] for v in (await stroom.find_documents(index, ['ElasticIndex'], 20)).get('values') or []
                 if v['docRef'].get('name') == index]
        if len(found) != 1:
            raise ToolError(f"No single Elastic Index doc named '{index}': give its uuid (get_field_conventions "
                            f"backend=elasticsearch lists them)")
        ref, doc = found[0], await stroom.get_doc('ElasticIndex', found[0]['uuid'])
    typed = [(f.get('fldName'), (f.get('nativeType') or '').lower()) for f in doc.get('fields') or []
             if f.get('fldName') and f.get('nativeType')]
    source = "the index doc's field list, with Elasticsearch's own types"
    if not typed:
        try:
            listed = (await stroom.post('/dataSource/v1/findFields', {
                'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 2000}})).get('values') or []
        except ToolError:
            listed = []
        typed = [(f.get('fldName'), _STROOM_TO_ES.get((f.get('fldType') or '').lower())) for f in listed]
        source = "Stroom's field list for it, with Stroom's types (Elasticsearch's own were not in the doc)"
    properties: dict[str, Any] = {}
    kept = []
    for name, kind in typed:
        if not kind or not name or kind in ('object', 'nested'):
            continue
        node = properties
        *parents, leaf = name.split('.')
        for part in parents:
            node = node.setdefault(part, {'properties': {}}).setdefault('properties', {})
        node[leaf] = {'type': kind}
        kept.append((name, kind))
    if not properties:
        raise ToolError(f"Stroom lists no fields for Elastic Index doc '{ref['name']}' (its cluster may be unreachable, "
                        f"or it has never been searched): ask the user to paste its index template into the chat "
                        f"(GET _index_template/<name>, with any component templates) and draft with example_template")
    name = doc.get('indexName') or ref['name']
    text = f"PUT _index_template/{name}\n" + json.dumps({'index_patterns': [f'{name}*'],
                                                          'template': {'mappings': {'properties': properties}}})
    read = {'doc': ref['name'], 'index': doc.get('indexName'), 'fields': len(kept), 'source': source,
            'examples': [f"{n} ({k})" for n, k in kept[:6]]}
    return text, (f"followed the fields of Elastic Index doc '{ref['name']}' ({len(kept)} fields, read from {source}); "
                  f"its index template itself (settings, component templates) is not visible through Stroom"), read


async def _choose(ctx: Context, question: str, options: list[str]) -> Any:
    """The user's pick in a form, None where the client has no form (the agent asks instead), or on a modern
    connection the form itself, for the tool to return."""
    store = (getattr(ctx, 'lifespan_context', None) or {}).get('consent')
    return await store.choose(ctx, 'get_field_conventions', question, options) if store else None


async def get_field_conventions(
        ctx: Context,
        name: Annotated[str | None, Field(description="Convention profile to use; omit to list them.")] = None,
        backend: Annotated[Backend | None, Field(description="The index's backend: for Elasticsearch, the user's example "
                                                             "comes first (an index template, or an existing index in "
                                                             "Stroom), a convention profile only without one.")] = None,
) -> dict[str, Any]:
    """
    Field naming conventions for indexes. Without a name (and no configured default) this lists the profiles
    and returns needs_guidance: the agent must ask the user which convention to follow, point at reference
    index docs, or describe one. It never picks a convention itself. With a name it returns the profile's field
    map plus the fields and types of its reference index docs (Lucene Index or Elastic Index docs, read
    through Stroom).
    """
    profiles = _conventions(ctx)
    templates = None
    if not name and not backend and not gateway_from(ctx).settings.default_convention:
        # Seen: an agent asked without the backend, got the profiles, then looked the backend up and asked again. When
        # every indexing template has the same backend, that's the index's backend.
        try:
            from tools.templates import find_pipeline_templates
            found = (await find_pipeline_templates(ctx, 'indexing')).get('candidates') or []
            backends = {c.get('backend') for c in found if c.get('backend')}
            if len(backends) == 1:
                backend = backends.pop()
                templates = [{k: c.get(k) for k in ('uuid', 'name', 'path', 'backend')} for c in found]
        except Exception:   # the lookup is a shortcut; without it, the agent is told to find the backend itself
            pass
    if not name and backend == 'elasticsearch':
        # Field names, types and structure come from what the environment already indexes: the user's example.
        found = await gateway_from(ctx).find_documents('*', ['ElasticIndex'], 60)
        existing = [{'name': v['docRef'].get('name'), 'uuid': v['docRef'].get('uuid'), 'path': v.get('path')}
                    for v in found.get('values') or [] if v['docRef'].get('type') == 'ElasticIndex']
        # One option per choice the user is offered, in order, each with the label to show: an agent offered
        # "follow an existing index", ECS and Stroom flat, and dropped the template the user had.
        options = [
            {'choice': 'From an index template', 'option': 'example index template',
             'how': "The user pastes the Elasticsearch index template a similar source's index uses (Kibana Dev Tools: "
                    "GET _index_template/<name>, or GET <index>/_mapping), with any component templates, into the "
                    "chat: ask for it there and end your turn, as a choice form cannot carry it. Once pasted: "
                    "draft_index_mapping example_template= it, exactly as given, and keep it for "
                    "propose_index_template. The only choice that follows the template's settings and components."},
            {'choice': 'Follow an existing index in Stroom', 'option': 'follow an existing index in Stroom',
             'how': "The user picks one of existing_indexes (a similar source's): draft_index_mapping like_index=<its "
                    "uuid> reads its field names and Elasticsearch types through Stroom, with nothing to paste, and "
                    "the user confirms. Its index template itself (settings, component templates, keyword sub-fields) "
                    "is not readable through Stroom: for those, the template is pasted (the first choice).",
             'existing_indexes': existing}]
        for profile_name, profile in profiles.items():
            options.append({'choice': f"{_PROFILE_LABELS.get(profile_name, profile_name)} convention",
                            'option': f'convention profile {profile_name}', 'description': profile.get('description'),
                            'how': f"Only when the user has no example: draft_index_mapping convention={profile_name} "
                                   f"without_example=true, which the user confirms in a form."})
        chosen = await _choose(ctx, "How should the new index's fields be named?", [o['choice'] for o in options])
        if chosen is not None and not isinstance(chosen, str):
            return chosen       # the form, on a modern connection: the answer comes with the repeated call
        if chosen:
            option = next(o for o in options if o['choice'] == chosen)
            result = {'status': 'chosen', 'choice': chosen, 'how': option['how'],
                      **({'indexing_templates': templates} if templates else {})}
            if option['option'] == 'follow an existing index in Stroom' and existing:
                labels = {f"{e['name']} ({e['path']})": e for e in existing}
                which = None if len(existing) == 1 else await _choose(
                    ctx, 'Which existing index should it follow?', list(labels))
                if which is not None and not isinstance(which, str):
                    return which
                pick = existing[0] if len(existing) == 1 else labels.get(which or '')
                if pick:
                    result.update(like_index=pick['uuid'], hint=f"The user chose to follow '{pick['name']}': "
                                  f"draft_index_mapping like_index={pick['uuid']} (with the events streams).")
            elif option['option'] == 'example index template':
                result['hint'] = ("Ask the user to paste the index template (and any component templates) into the "
                                  "chat, and end your turn: a form can't carry it. Then draft_index_mapping "
                                  "example_template= it, exactly as given.")
            else:
                profile_name = option['option'].removeprefix('convention profile ')
                result['hint'] = (f"draft_index_mapping convention={profile_name} without_example=true (with the "
                                  f"events streams).")
            return result
        return {'status': 'needs_guidance', 'options': options,
                **({'backend': 'elasticsearch (every indexing template is an Elasticsearch one)',
                    'indexing_templates': templates} if templates else {}),
                'hint': f"Ask the user which, with exactly these {len(options)} choices, in this order and with these "
                        f"labels ({'; '.join(o['choice'] for o in options)}), and recommend none: it is their call, "
                        f"not yours. Draft nothing until they answer; if they choose the index template, wait for "
                        f"them to paste it. like_index and without_example are confirmed by the user in a form."}
    name = name or gateway_from(ctx).settings.default_convention
    if not name:
        # Without the backend, an agent took this for the whole choice and went for a convention, though the user
        # had their own index template (seen in VS Code).
        first = (f"The backend is {backend}: every indexing template is a {backend} one. " if templates else
                 "" if backend else
                 "Which backend first (find_pipeline_templates stage=indexing says): for Elasticsearch, call again with "
                 "backend=elasticsearch, whose choices start with the user's own index template; these profiles are "
                 "for Lucene, or for Elasticsearch only when the user has no template. ")
        if backend == 'lucene' and profiles:
            labels = {f"{_PROFILE_LABELS.get(n, n)} convention": n for n in profiles}
            chosen = await _choose(ctx, "How should the new index's fields be named?", list(labels))
            if chosen is not None and not isinstance(chosen, str):
                return chosen
            if chosen:
                return {'status': 'chosen', 'choice': chosen, 'convention': labels[chosen],
                        'hint': f"get_field_conventions name={labels[chosen]} for its field map, then "
                                f"draft_index_mapping convention={labels[chosen]} (with the events streams)."}
        return {'status': 'needs_guidance', 'profiles': {n: p.get('description') for n, p in profiles.items()},
                **({'indexing_templates': templates} if templates else {}),
                'hint': first + "Ask the user which convention to use, which existing index docs to follow, "
                                "or how fields should be named. Do not assume one."}
    if name not in profiles:
        raise ToolError(f"No convention profile '{name}'. Profiles: {', '.join(profiles) or 'none'}")
    profile = profiles[name]
    stroom = gateway_from(ctx)
    reference: dict[str, dict[str, str]] = {}
    for doc_name in profile.get('reference_index_docs') or []:
        found = await stroom.find_documents(doc_name, ['Index', 'ElasticIndex'], 20)
        for value in found.get('values') or []:
            ref = value['docRef']
            if ref.get('name') == doc_name and ref.get('type') in ('Index', 'ElasticIndex'):
                fields = await stroom.post('/dataSource/v1/findFields', {
                    'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 500}})
                reference[f"{ref['type']} {doc_name}"] = {f['fldName']: f['fldType'] for f in fields.get('values') or []}
    return {'name': name, 'profile': profile, 'reference_fields': reference}


def _draft_discovery(backend: str, index_name: str, discovery: Discovery) -> dict[str, Any]:
    """Nothing is read: Elasticsearch maps the source's fields as documents arrive. The fields Stroom needs are
    the only explicit ones."""
    if backend != 'elasticsearch':
        raise ToolError("A discovery index is Elasticsearch: its fields are mapped dynamically as documents arrive")
    plan = FieldPlan.for_discovery(index_name, discovery)
    try:
        xslt = plan.xslt()
    except ValueError as e:
        raise ToolError(str(e)) from e
    return {'plan': plan.model_dump(exclude_none=True), 'xslt': xslt,
            'rendered': plan.elastic_template(index_name),
            'hint': "Save the XSLT with save_xslt index_plan=plan and no code, create_indexing_pipeline from the "
                    "discovery template, step_sample on the raw streams (the documents show the source's fields), then "
                    "propose_index_template with the plan and the user's example template for its settings."}


async def draft_index_mapping(
        ctx: Context,
        backend: Annotated[Backend, Field(description="From the chosen indexing template (find_pipeline_templates).")],
        index_name: Annotated[str, Field(description="Lucene index doc name, or ES index / data stream name.")],
        convention: Annotated[str | None, Field(description="Convention profile the user chose (get_field_conventions); "
                                                     "none for a discovery index.")] = None,
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams from stage 1, to see which "
                                                                  "event-logging paths are actually populated.")] = [],
        extra_fields: Annotated[list[PlannedField] | str, ONE_OR_MORE, Field(
            description="Fields the user asked for beyond the convention's map.")] = [],
        drop_when: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="XPath tests on an Event for events the user wants kept out of the index, e.g. "
                        "\"EventDetail/TypeId = 'Heartbeat'\"; any that holds drops the event.")] = [],
        like_index: Annotated[str | None, Field(
            description="Elasticsearch: an existing Elastic Index doc (uuid or exact name) whose field names and types "
                        "to follow, read through Stroom, when the user has no index template to paste.")] = None,
        example_template: Annotated[str | None, Field(
            description="Elasticsearch: the user's example index template or index mapping (as for "
                        "propose_index_template); field names then follow it.")] = None,
        component_templates: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Only if the example lists any in composed_of: those component templates, as the user gave "
                        "them.")] = [],
        shared: Annotated[list[SharedTemplate] | str, ONE_OR_MORE, Field(
            description="Named templates from shared XSLTs that sibling indexing XSLTs call (describe_template's "
                        "shared_xslt): each writes a field (at, e.g. 'guid'), which the XSLT then does not write.")] = [],
        discovery: Annotated[Discovery | None, Field(
            description="A discovery index instead: raw data (JSON, delimited text or XML) indexed as it is into "
                        "Elasticsearch, with no convention and no Events. Give what the user confirmed: the input, the "
                        "timestamp field (and XML's record element), any stream meta to add, fields to drop.")] = None,
        without_example: Annotated[bool, Field(
            description="Elasticsearch: only when the user has no example index template and no existing index to "
                        "follow; they confirm it in a form, and the convention alone names the fields.")] = False,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Draft the index for the build: a field plan (name, type and source path per field) from the chosen
    convention, limited to paths the sample events actually populate, plus StreamId and EventId. With the
    user's example Elasticsearch index template, fields take the example's names for the same data (User.Id,
    TypeId...), others are named in its style, and sample paths the example maps are added. Returns the plan
    rendered for the backend (Lucene field list or Elasticsearch index template) and a draft indexing XSLT in
    the output form that backend's indexing filter reads, leaving out events drop_when names. With discovery,
    a discovery index instead: nothing is read; raw JSON records are copied as they are and Elasticsearch maps
    their fields dynamically. Nothing is saved.
    """
    if discovery:
        return _draft_discovery(backend, index_name, discovery)
    if backend == 'elasticsearch' and not (example_template or like_index):
        # Names, types and structure come from what the environment already indexes: the agent asks the user,
        # and indexing from a convention alone is the user's call, made in a form, not the agent's.
        if not without_example:
            options = await get_field_conventions(ctx, backend='elasticsearch')
            return {**options, 'drafted': False,
                    'hint': "Not drafted: ask the user for their example first, offering these options. Then "
                            "draft_index_mapping with example_template=... (pasted) or like_index=<uuid>; only if they "
                            "have neither, with convention=... and without_example=true, which they confirm."}
        gate = await consent_from(ctx).require(
            ctx, 'confirmation', 'draft_index_mapping',
            f"Index '{index_name}' into Elasticsearch without an example index template",
            {'field names and types from': f"the '{convention}' convention" if convention else 'no convention given',
             'index template': "built from Elasticsearch defaults, not from an index you already have",
             'instead': "paste an example index template, or name an existing index in Stroom to follow"},
            confirmation_id)
        if gate:
            return gate
    if not events_stream_ids:
        raise ToolError("Give events_stream_ids: the Events streams the index will hold, to see which paths they populate")
    await require_events(gateway_from(ctx), events_stream_ids)
    profiles = _conventions(ctx)
    pasted = example_template
    like_note = None
    if like_index and not example_template:
        # Following another source's index is the user's choice, as the example is: its fields are read through
        # Stroom first, so the user confirms what they'd get, and what only the template itself would give.
        example_template, like_note, read = await _example_from_index(ctx, like_index)
        gate = await consent_from(ctx).require(
            ctx, 'confirmation', 'draft_index_mapping',
            f"Name the fields of index '{index_name}' after an existing index in Stroom", {
                'follow': f"Elastic Index doc '{read['doc']}'" + (f" (index {read['index']})" if read['index'] else ''),
                'read through Stroom': f"{read['fields']} fields from {read['source']}, e.g. {', '.join(read['examples'])}",
                'not readable through Stroom': "its index template itself: settings (shards, refresh), component "
                                               "templates, keyword sub-fields and ignore_above. Elasticsearch defaults "
                                               "for those, unless you paste the template",
                'or': "paste its index template (with any component templates) into the chat to follow it whole"},
            confirmation_id)
        if gate:
            return gate
    if not convention and example_template:
        # The example names the fields; a profile only says which event paths are worth indexing.
        convention = 'ecs' if 'ecs' in profiles else next(iter(profiles), None)
    if convention not in profiles:
        raise ToolError(f"No convention profile '{convention}'; ask the user for an example index template (or an "
                        f"existing index to follow), or a convention: get_field_conventions backend=elasticsearch")
    profile = profiles[convention]
    events = await summarise_events(ctx, events_stream_ids, 200)
    populated = events['path_population']
    fields = [PlannedField(name='StreamId', type='id', source='@StreamId'),
              PlannedField(name='EventId', type='id', source='@EventId')]
    unused = []
    for path, spec in (profile.get('field_map') or {}).items():
        # A name is planned once, from the first of its paths the sample populates (the client's address, else the
        # source address of whichever Network action the event records).
        if population_of(path, populated) and not any(f.name == spec['name'] for f in fields):
            fields.append(PlannedField(name=spec['name'], type=spec['type'], source=path))
        elif not population_of(path, populated):
            unused.append(path)
    # What happened, from the event's action element (Alert's Type and Severity, Authenticate's Action and Outcome,
    # Process, Update), planned by default: a user asked for these each time. Network's are the profile's.
    from utils.templatecheck import _derive, field_group, nested_name
    nested = profile.get('structure') == 'nested'
    added = 0
    for path in sorted(populated):
        if (added >= 20 or not populated[path] or not path.startswith('EventDetail/') or 'Data' in path.split('/')
                or path.startswith('EventDetail/Network/') or not field_group(path)
                or any(source_matches(f.source, path) for f in fields)):
            continue
        taken = {f.name for f in fields}
        name = nested_name(path, 'ecs') if nested else _derive(path, 'pascal', False, taken)
        if not name or name in taken:
            continue
        fields.append(PlannedField(name=name, source=path,
                                   type='boolean' if path.endswith('/Success') else 'long' if path.endswith('/Port')
                                   else 'keyword'))
        added += 1
    example_notes, subobjects = [], True
    if example_template:
        try:
            _, example = parse_template(example_template)
            components = parse_component_templates([component_templates] if isinstance(component_templates, str)
                                                   else list(component_templates))
        except ValueError as e:
            raise ToolError(str(e)) from e
        composed, _ = compose(example, components)
        known: dict[str, set[str]] = {}
        for other in profiles.values():
            for path, spec in (other.get('field_map') or {}).items():
                known.setdefault(path, set()).add(spec['name'])
        named, example_notes = names_from_example(
            [f.model_dump() for f in fields], read_mapping_fields(composed), known, sorted(p for p in populated if populated[p]))
        fields = [PlannedField(**f) for f in named]
        if like_note:
            example_notes.insert(0, like_note)
        # Documents are written nested whatever the example; subobjects: false changes how the index maps them.
        if ((composed.get('template') or {}).get('mappings') or {}).get('subobjects') is False:
            subobjects = False
            example_notes.append('the example sets subobjects: false: the index maps each dotted name (user.id) as a '
                                 'field of its own; documents are still written nested')
    fields += [f for f in extra_fields if f.name not in {x.name for x in fields}]
    time_field = next((f.name for f in fields if f.source == 'EventTime/TimeCreated'), 'EventTime')
    if backend == 'elasticsearch' and not any(f.name == '@timestamp' for f in fields):
        fields.append(PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'))
        time_field = '@timestamp'
    stroom = gateway_from(ctx)
    for use in shared:
        # The shared template writes the fields: they stay in the plan (the index maps them) but the XSLT calls the
        # template instead of writing them, so each is written once. Which fields, read from the shared XSLT itself,
        # found by name as Stroom resolves the import.
        found = [v['docRef'] for v in (await stroom.find_documents(use.href, ['XSLT'], 20)).get('values') or []
                 if v['docRef'].get('name') == use.href]
        values = json_values((await stroom.get_doc('XSLT', found[0]['uuid'])).get('data') or '', use.template)             if found else {}
        values = {k: v for k, v in values.items() if k == use.at or k.startswith(use.at + '.')} or {use.at: 'string'}
        for name, element in values.items():
            if not any(f.name == name for f in fields):
                fields.append(PlannedField(name=name, type={'number': 'long', 'boolean': 'boolean'}.get(element, 'keyword'),
                                           source=f'shared:{use.template}'))
        example_notes.append(f"{sorted(values)}: written by the shared template {use.template} ({use.href})"
                             + ('' if found else '; not found as an XSLT document, so typed as a keyword'))
    plan = FieldPlan(backend=backend, index_name=index_name, time_field=time_field, fields=fields, drop_when=drop_when,
                     subobjects=subobjects, shared=list(shared))
    if pasted and backend == 'elasticsearch':
        build = await _build_of_stream(ctx, events_stream_ids[0])
        if build:
            try:
                await _keep_example(ctx, build, index_name, pasted, [component_templates] if isinstance(
                    component_templates, str) else list(component_templates))
                example_notes.append(f"the example is kept in build {build} ('{index_name}{EXAMPLE_SUFFIX}'): "
                                     f"propose_index_template follows it even if it isn't given again")
            except Exception as e:   # keeping it is a convenience; the plan stands without it
                logger.warning("Couldn't keep the example index template in build %s: %s", build, e)
    rendered = plan.lucene_fields() if backend == 'lucene' else plan.elastic_template(index_name)
    # Network paths once, whichever action: one field for a source address, not one per Permit and Deny.
    unmapped = sorted(dict.fromkeys(any_action(p) for p in populated
                                    if populated[p] and not any(source_matches(f.source, p) for f in fields)))
    # No XSLT here: save_xslt index_plan= generates it, and half the reply was code the agent never reads.
    return {'plan': plan.model_dump(), 'problems': plan.required(), 'rendered': rendered,
            'convention_paths_not_in_sample': unused, 'populated_paths_not_mapped': unmapped[:40],
            'field_mapping': index_field_mapping_markdown(plan, populated),
            **({'from_example': example_notes} if example_template or shared else {}),
            'hint': "Review unmapped paths with the user; add any they want as extra_fields and draft again. Save the "
                    "XSLT with save_xslt index_plan=plan and no code (it is generated from the plan), so "
                    "write_documentation generates the Field mapping section."}


async def set_index_fields(
        ctx: Context,
        index_uuid: Annotated[str, Field(description="A Lucene Index doc this server created.")],
        plan: Annotated[FieldPlan, Field(description="The field plan from draft_index_mapping (backend lucene).")],
) -> dict[str, Any]:
    """Lucene: add the plan's fields to the index doc (keywords as TEXT with the KEYWORD analyzer)."""
    if plan.backend != 'lucene':
        raise ToolError("create_index_doc (plan=...) is for Lucene; Elasticsearch fields come from the index template the user commits "
                        "(propose_index_template)")
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Index', index_uuid)
    ref = {'type': 'Index', 'uuid': index_uuid, 'name': doc.get('name')}
    await guard_from(ctx).check_managed(ref)
    existing = await stroom.post('/dataSource/v1/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 500}})
    have = {f['fldName'] for f in existing.get('values') or []}
    added = []
    for field in plan.lucene_fields():
        if field['fldName'] not in have:
            await stroom.post('/index/v2/addField', {'indexDocRef': ref, 'indexField': field})
            added.append(field['fldName'])
    return {'index': doc.get('name'), 'added': added, 'already_present': sorted(have)}


async def find_elastic_clusters(
        ctx: Context, test: Annotated[bool, Field(description="Also run Stroom's connection test on each.")] = False,
) -> dict[str, Any]:
    """
    Elasticsearch: the Elastic Cluster docs in Stroom with their connection URLs (never credentials), the
    Elastic Index docs that use each, and those docs' settings. Pick the cluster that sibling sources use and
    confirm it with the user; this server never creates or changes cluster docs.
    """
    stroom = gateway_from(ctx)
    clusters = [v['docRef'] for v in (await stroom.find_documents('*', ['ElasticCluster'], 50)).get('values') or []
                if v['docRef'].get('type') == 'ElasticCluster']
    indexes = [v for v in (await stroom.find_documents('*', ['ElasticIndex'], 500)).get('values') or []
               if v['docRef'].get('type') == 'ElasticIndex']
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for value in indexes:
        doc = await stroom.get_doc('ElasticIndex', value['docRef']['uuid'])
        cluster = (doc.get('clusterRef') or {}).get('uuid')
        by_cluster.setdefault(cluster, []).append({'name': doc.get('name'), 'uuid': doc.get('uuid'),
                                                   'index_name': doc.get('indexName'), 'time_field': doc.get('timeField'),
                                                   'path': (value.get('path') or '').replace(' / ', '/')})
    out = []
    for ref in clusters:
        doc = _redact(await stroom.get_doc('ElasticCluster', ref['uuid']))
        entry = {'name': doc.get('name'), 'uuid': doc.get('uuid'),
                 'urls': (doc.get('connection') or {}).get('connectionUrls'),
                 'index_docs': by_cluster.get(ref['uuid'], [])}
        if test:
            entry['test'] = await stroom.post('/elasticCluster/v1/testCluster', await stroom.get_doc('ElasticCluster', ref['uuid']))
        out.append(entry)
    return {'clusters': out}


async def _the_cluster_in_use(stroom) -> str | None:
    """The one Elastic Cluster doc the environment's Elastic Index docs use, or None when they use several, or
    there are none."""
    used = set()
    try:
        for value in ((await stroom.find_documents('*', ['ElasticIndex'], 50)).get('values') or []):
            try:
                doc = await stroom.get_doc('ElasticIndex', value['docRef']['uuid'])
            except ToolError:
                continue    # listed, but gone
            if (doc.get('clusterRef') or {}).get('uuid'):
                used.add(doc['clusterRef']['uuid'])
    except ToolError:
        return None
    return used.pop() if len(used) == 1 else None


async def create_index_doc(
        ctx: Context,
        build: Build,
        backend: Backend,
        name: Annotated[str, Field(description="Index doc name, following the environment's convention.")],
        time_field: Annotated[str, Field(description="The plan's time field.")],
        index_name: Annotated[str | None, Field(description="Elasticsearch: the index or data stream name.")] = None,
        cluster_uuid: Annotated[str | None, Field(description="Elasticsearch: an existing Elastic Cluster doc.")] = None,
        volume_group: Annotated[str, Field(description="Lucene: the index volume group.")] = 'Default Volume Group',
        plan: Annotated[FieldPlan | None, Field(description="Lucene: the field plan from draft_index_mapping; its fields "
                                                           "are added to the index doc (keywords as TEXT with the KEYWORD "
                                                           "analyzer). Elasticsearch fields come from the index template.")] = None,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Create the build's index doc: a Lucene Index in a volume group with the plan's fields, or an Elastic
    Index doc on an existing Elastic Cluster doc pointing at the index or data stream, tested against the
    cluster. The user confirms the backend, name and target.
    """
    stroom = gateway_from(ctx)
    if backend == 'elasticsearch':
        # Seen in VS Code: refused for want of both, though the plan names the index and every Elastic Index doc in
        # the environment used the same cluster. Both are shown in the user's form.
        index_name = index_name or (plan.index_name if plan is not None else None)
        why = ''
        if not cluster_uuid:
            cluster_uuid = await _the_cluster_in_use(stroom)
            why = ' (the cluster the existing Elastic Index docs use)' if cluster_uuid else ''
        if not (index_name and cluster_uuid):
            raise ToolError("Elasticsearch needs index_name (or plan=, which names it) and cluster_uuid: the existing "
                            "Elastic Index docs use more than one cluster, or none (find_elastic_clusters lists them)")
        try:
            cluster = await stroom.get_doc('ElasticCluster', cluster_uuid)
        except ToolError as e:
            # Seen: an Elastic Index doc's uuid (the index the user had been offered to follow) given as the cluster,
            # and Stroom's 500 "Document not found" passed on as it was.
            try:
                index_doc = await stroom.get_doc('ElasticIndex', cluster_uuid)
            except ToolError:
                index_doc = None
            if index_doc:
                its = index_doc.get('clusterRef') or {}
                raise ToolError(f"{cluster_uuid} is the Elastic Index doc '{index_doc.get('name')}', not an Elastic "
                                f"Cluster: its cluster is '{its.get('name')}' (cluster_uuid={its.get('uuid')}). Give "
                                f"that, or another from find_elastic_clusters.") from e
            raise ToolError(f"No Elastic Cluster doc {cluster_uuid}: find_elastic_clusters lists them.") from e
        target = {'cluster': f"{cluster.get('name')}{why}", 'index name': index_name}
    else:
        target = {'volume group': volume_group}
    details = {'build': build, 'backend': backend, 'index doc': name, 'time field': time_field, **target}
    names = {'name': ('Index doc name', name)}
    if backend == 'elasticsearch' and index_name:
        names['index_name'] = ('Elasticsearch index name', index_name)
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'create_index_doc', f"Create {backend} index doc '{name}'",
                                           details, confirmation_id, editable=names)
    if gate:
        return gate
    # The user may have corrected the names in the form.
    name = edited(ctx, 'name', name)
    index_name = edited(ctx, 'index_name', index_name) if index_name else index_name
    doc_type = INDEX_TYPE[backend]
    ref = await guard_from(ctx).create(doc_type, name, build)
    doc = await stroom.get_doc(doc_type, ref['uuid'])
    if backend == 'lucene':
        doc.update(volumeGroupName=volume_group, timeField=time_field, partitionBy='MONTH', partitionSize=1,
                   shardsPerPartition=1)
    else:
        doc.update(clusterRef={'type': 'ElasticCluster', 'uuid': cluster_uuid, 'name': cluster.get('name')},
                   indexName=index_name, timeField=time_field)
    doc = await stroom.put_doc(doc)
    extra: dict[str, Any] = {}
    if backend == 'lucene' and plan is not None:
        extra['fields'] = await set_index_fields(ctx, doc['uuid'], plan)
    elif backend == 'lucene':
        extra['fields'] = ("none yet: the index indexes nothing until it has the plan's fields. Give plan= here, or "
                           "create_indexing_pipeline adds them from the plan kept with the indexing XSLT (save_xslt "
                           "index_plan=...).")
    elif backend == 'elasticsearch':
        try:
            extra['test'] = await test_elastic_index(ctx, doc['uuid'])
        except ToolError as e:
            extra['test'] = {'error': str(e)}
    from tools.plan import with_next
    return await with_next(ctx, build, {'type': doc_type, 'uuid': doc['uuid'], 'name': doc['name'], **target, **extra})


async def require_events(stroom, stream_ids: list[int]) -> None:
    """Each stream is an Events stream: never a source's own data, whatever its content looks like (seen: a raw
    <records> stream taken as the Events to plan an index from, and a plan drafted from nothing)."""
    for stream_id in stream_ids:
        meta = await _meta(stroom, stream_id)
        if meta.get('typeName') != 'Events':
            raise ToolError(f"Stream {stream_id} is {meta.get('typeName')!r}, not Events: the source's own data, "
                            f"whatever its XML or records look like. An index holds the Events an events pipeline "
                            f"writes: process the sample through it (create_processor_filter, wait_for_processing) "
                            f"and give those Events streams (stage 1 first).")


async def _require_input(ctx: Context, pipeline_uuid: str, stream_ids: list[int], plan: FieldPlan | None = None) -> None:
    """The streams an indexing pipeline is checked over: Events, unless it is a discovery pipeline, which indexes a
    source's own data as it is (seen in e2e: a discovery index's template refused for its raw sample)."""
    try:
        await require_events(gateway_from(ctx), stream_ids)
    except ToolError:
        if await _is_discovery(ctx, pipeline_uuid, plan):
            return
        raise


async def _is_discovery(ctx: Context, pipeline_uuid: str, plan: FieldPlan | None) -> bool:
    """Whether the pipeline indexes raw data as it is: its plan says so (the one given, else the one kept with its
    XSLT), or its parser is a raw one. An XML discovery pipeline's XMLParser is also an indexing pipeline's."""
    if plan is not None:
        return bool(plan.discovery)
    stroom = gateway_from(ctx)
    if (await _shape(stroom, pipeline_uuid)).get('stage') == 'discovery':
        return True
    from tools.pipelines import merge_layers
    for prop in merge_layers(await stroom.pipeline_layers(pipeline_uuid))['properties']:
        value = prop.get('value')
        if prop['name'] == 'xslt' and isinstance(value, dict) and value.get('uuid'):
            kept = read_mapping((await stroom.get_doc('XSLT', value['uuid'])).get('description'))
            if kept and kept[0] == 'index' and (kept[1] or {}).get('discovery'):
                return True
    return False


async def _events_available(ctx: Context, build: str, events_stream_ids: list[int]) -> None:
    """An indexing pipeline reads Events, which raw data only has once an events pipeline has translated it.
    The build's own events pipeline counts; otherwise the Events streams it will index must already exist."""
    stroom = gateway_from(ctx)
    for doc in await guard_from(ctx).folder_contents(build):
        if doc['type'] == 'Pipeline' and (await _shape(stroom, doc['uuid']))['stage'] == 'translation':
            return
    if not events_stream_ids:
        raise ToolError(f"Build '{build}' has no events pipeline, and no events_stream_ids were given. An indexing "
                        f"pipeline reads Events streams, not raw data: build the events pipeline first (stage 1 of "
                        f"onboard_data_source: feed, translation XSLT, step, process), then index its Events. To "
                        f"index Events an existing pipeline already produces, pass their stream ids as "
                        f"events_stream_ids. Raw structured data indexed as it is, with no translation, is a "
                        f"discovery template (find_pipeline_templates stage=discovery).")
    await require_events(stroom, events_stream_ids)


async def _missing_index_fields(stroom, index_uuid: str, xslt_uuid: str) -> tuple[list[str], FieldPlan | None]:
    """The fields the indexing XSLT writes that the Lucene index lacks, and the field plan kept with the XSLT (None when
    it was written by hand). Stroom drops a value for a field the index lacks with only a warning ("Attempt to index
    unknown field"), so the pipeline runs and indexes nothing searchable."""
    index = await stroom.get_doc('Index', index_uuid)
    ref = {'type': 'Index', 'uuid': index_uuid, 'name': index.get('name')}
    found = await stroom.post('/dataSource/v1/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 500}})
    have = {f['fldName'] for f in found.get('values') or []}
    xslt = await stroom.get_doc('XSLT', xslt_uuid)
    kept = read_mapping(xslt.get('description'))
    plan = FieldPlan.model_validate(kept[1]) if kept and kept[0] == 'index' else None
    if plan:
        written = [f.name for f in plan.fields]
    else:   # written by hand: the records:2 data elements it writes
        written = list(dict.fromkeys(re.findall(r'<data\s+name="([^"{]+)"', xslt.get('data') or '')))
    missing = [name for name in written if name not in have]
    if missing and not plan:
        raise ToolError(f"Index '{index.get('name')}' has {'no fields' if not have else 'no field'} for "
                        f"{missing[:10]}{' ...' if len(missing) > 10 else ''}, which the indexing XSLT writes: Stroom "
                        f"would drop those values ('Attempt to index unknown field') and the searches would find "
                        f"nothing. Save the indexing XSLT from its plan (save_xslt index_plan=the plan from "
                        f"draft_index_mapping, no code): the plan is kept with it, and its fields are then added to the "
                        f"index here. Or create the index with them: create_index_doc plan=....")
    return missing, plan


async def create_indexing_pipeline(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Pipeline name, e.g. 'Acme - Indexing'.")],
        template_uuid: Annotated[str, Field(description="Indexing template (find_pipeline_templates stage=indexing).")],
        xslt_uuid: Annotated[str, Field(description="The indexing XSLT, e.g. created from draft_index_mapping's draft.")],
        index_uuid: Annotated[str | None, Field(description="Lucene: the Index doc. Elasticsearch: the Elastic Index "
                                                            "doc, which names the index and its cluster.")] = None,
        index_name: Annotated[str | None, Field(description="Elasticsearch: the index or data stream name.")] = None,
        cluster_uuid: Annotated[str | None, Field(
            description="Elasticsearch: the cluster, if the template does not already set one.")] = None,
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Events streams this pipeline will index (stage 1's output, or an existing Events feed's). "
                        "Needed when the build has no events pipeline of its own.")] = [],
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Stage 2: create an indexing pipeline as a child of an indexing template, setting its XSLT and where it
    indexes: the Lucene Index doc, or the Elasticsearch index name (and cluster if the template leaves it
    open). It reads Events streams, so it comes after the events pipeline (stage 1) has produced them, or
    takes an existing Events feed's streams as events_stream_ids.
    """
    stroom = gateway_from(ctx)
    shape = await _shape(stroom, template_uuid)
    if shape['stage'] not in ('indexing', 'discovery'):
        raise ToolError(f"That template is a {shape['stage']} template, not an indexing one")
    # An XML discovery pipeline has an indexing template's shape (XMLParser, XSLT, indexing filter): the plan kept
    # with its XSLT says it reads raw data, not Events.
    kept = read_mapping((await stroom.get_doc('XSLT', xslt_uuid)).get('description'))
    discovery = bool(kept and kept[0] == 'index' and (kept[1] or {}).get('discovery'))
    if shape['stage'] == 'indexing' and not discovery:
        await _events_available(ctx, build, events_stream_ids)
    xslt_element = next((s['element'] for s in shape['child_must_supply'] if s['type'] == 'XSLTFilter'), 'xsltFilter')
    props = [PropertyValue(element=xslt_element, name='xslt', doc_uuid=xslt_uuid, doc_type='XSLT')]
    open_props = {(s['element'], s['property']) for s in shape['child_must_supply']}
    missing, plan = [], None
    if shape['backend'] == 'lucene':
        if not index_uuid:
            raise ToolError("This template indexes into Lucene: give index_uuid")
        missing, plan = await _missing_index_fields(stroom, index_uuid, xslt_uuid)
        element = next(e for e, p in open_props if p == 'index')
        props.append(PropertyValue(element=element, name='index', doc_uuid=index_uuid, doc_type='Index'))
    else:
        if not index_name and index_uuid:
            # The Elastic Index doc names the index and its cluster (seen in VS Code: refused for want of index_name
            # though the doc was given).
            try:
                elastic = await stroom.get_doc('ElasticIndex', index_uuid)
            except ToolError as e:
                raise ToolError(f"index_uuid {index_uuid} is not an Elastic Index doc this template can index into: "
                                f"give index_name (and cluster_uuid)") from e
            index_name = elastic.get('indexName')
            cluster_uuid = cluster_uuid or (elastic.get('clusterRef') or {}).get('uuid')
        if not index_name:
            raise ToolError("This template indexes into Elasticsearch: give index_name, or index_uuid (the Elastic "
                            "Index doc, which names it)")
        element = next((e for e, p in open_props if p == 'indexName'), 'elasticIndexingFilter')
        props.append(PropertyValue(element=element, name='indexName', value=index_name))
        if (element, 'cluster') in open_props:
            if not cluster_uuid:
                raise ToolError("The template leaves the cluster open: give cluster_uuid")
            props.append(PropertyValue(element=element, name='cluster', doc_uuid=cluster_uuid, doc_type='ElasticCluster'))
    result = await create_pipeline(ctx, name, template_uuid, props, build=build, confirmation_id=confirmation_id,
                                   accept_parser_mismatch=True)   # an indexing pipeline reads Events, not the raw sample
    if result.get('uuid'):
        result['backend'] = shape['backend']
        if shape['backend'] == 'lucene' and missing:
            # The index lacks fields the XSLT's plan writes (made without plan=): the agreed plan supplies them.
            result['index_fields_added'] = (await set_index_fields(ctx, index_uuid, plan))['added']
    return result


async def test_elastic_index(ctx: Context, index_uuid: Annotated[str, Field(description="Elastic Index doc.")]) -> dict[str, Any]:
    """Elasticsearch: Stroom's own connection and index test for an Elastic Index doc."""
    stroom = gateway_from(ctx)
    return {'result': await stroom.post('/elasticIndex/v1/testIndex', await stroom.get_doc('ElasticIndex', index_uuid))}


# Fields the search API's TableSettings accepts; a dashboard's table component carries more (e.g.
# selectionHandlers, pageSize) that the search request rejects with "Unable to process JSON".
_TABLE_SETTINGS = {'aggregateFilter', 'applyValueFilters', 'conditionalFormattingRules', 'extractValues',
                   'extractionPipeline', 'fields', 'maxResults', 'maxStringFieldLength', 'modelVersion',
                   'overrideMaxStringFieldLength', 'queryId', 'showDetail', 'valueFilter', 'visSettings', 'window'}


def _column(name: str) -> dict[str, Any]:
    return {'id': str(uuidlib.uuid4()), 'name': name, 'expression': '${' + name + '}', 'visible': True,
            'width': 150, 'format': {'type': 'GENERAL'}}


_IDS = ('StreamId', 'EventId')


def dashboard_config(source: dict[str, Any], fields: list[str], time_field: str | None,
                     window_start: str | None) -> dict[str, Any]:
    """A verification dashboard: a query on the index doc (the time field, from window_start through the end of
    today, run on open); a table of the user's fields, newest first, with StreamId and EventId as hidden columns; and
    a text pane on the selected row's record, with stepping, and no extraction pipeline."""
    query_id, table_id, text_id = 'query-VERIFY', 'table-VERIFY', 'text-VERIFY'
    shown = [f for f in dict.fromkeys(fields) if f not in _IDS]
    columns = [{**_column(f), **({'sort': {'order': 0, 'direction': 'DESCENDING'}} if f == time_field else {})}
               for f in shown]
    if time_field and time_field not in shown:     # sorted on, though not shown
        columns.append({**_column(time_field), 'visible': False, 'sort': {'order': 0, 'direction': 'DESCENDING'}})
    ids = {name: {**_column(name), 'visible': False} for name in _IDS}
    columns += list(ids.values())
    expression = {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'term', 'field': time_field, 'condition': 'BETWEEN', 'value': f'{window_start},day()+1d'}]
        if time_field and window_start else []}
    table = {'type': 'table', 'queryId': query_id, 'dataSourceRef': source, 'fields': columns, 'extractValues': False,
             'maxResults': [1000], 'pageSize': 100, 'showDetail': False, 'modelVersion': '7.2.0'}
    text = {'type': 'text', 'tableId': table_id, 'showAsHtml': False, 'showStepping': True,
            'streamIdField': {'id': ids['StreamId']['id'], 'name': 'StreamId'},
            'recordNoField': {'id': ids['EventId']['id'], 'name': 'EventId'}, 'modelVersion': '7.8.0'}
    size = lambda width, height: {'width': width, 'height': height}  # noqa: E731
    # Laid out as a dashboard made in Stroom's UI is (live): every layout node sized, and the config's own size,
    # constraints, model version and time range set. Without them the search API still ran the dashboard, but
    # the UI showed it empty: no query, no widgets.
    return {'components': [
        {'type': 'query', 'id': query_id, 'name': 'Query', 'settings': {
            'type': 'query', 'dataSource': source, 'expression': expression,
            'automate': {'open': bool(expression['children']), 'refresh': False}}},
        {'type': 'table', 'id': table_id, 'name': 'Table', 'settings': table},
        {'type': 'text', 'id': text_id, 'name': 'Text', 'settings': text}],
        'layout': {'type': 'splitLayout', 'preferredSize': size(200, 200), 'dimension': 1, 'children': [
            {'type': 'tabLayout', 'preferredSize': size(200, 150), 'tabs': [{'id': query_id, 'visible': True}],
             'selected': 0},
            {'type': 'splitLayout', 'preferredSize': size(200, 700), 'dimension': 0, 'children': [
                {'type': 'tabLayout', 'preferredSize': size(860, 700), 'tabs': [{'id': table_id, 'visible': True}],
                 'selected': 0},
                {'type': 'tabLayout', 'preferredSize': size(480, 700), 'tabs': [{'id': text_id, 'visible': True}],
                 'selected': 0}]}]},
        'layoutConstraints': {'fitWidth': True, 'fitHeight': True}, 'preferredSize': size(0, 0),
        'designMode': False, 'timeRange': {'name': 'All time', 'condition': 'BETWEEN'}, 'modelVersion': '7.2.0'}


def window_start(times: list[str]) -> str | None:
    """The earliest time, rounded back to a 30-day boundary (days since the epoch), as the dashboard's start."""
    from datetime import datetime, timezone
    parsed = []
    for t in times:
        try:
            parsed.append(datetime.fromisoformat(str(t).replace('Z', '+00:00')))
        except ValueError:
            continue
    if not parsed:
        return None
    days = int(min(parsed).timestamp() // 86400)
    start = datetime.fromtimestamp((days - days % 30) * 86400, tz=timezone.utc)
    return start.strftime('%Y-%m-%dT%H:%M:%S.000Z')


async def create_verification_dashboard(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Dashboard name, e.g. the index name with a -VERIFY suffix.")],
        index_uuid: Annotated[str, Field(description="The index doc to query.")],
        backend: Backend,
        fields: Annotated[list[str] | str, ONE_OR_MORE, Field(description="The table's columns.")],
        time_field: str | None = None,
        window: str | None = None,
) -> dict[str, Any]:
    """A workspace dashboard on the index doc, as dashboard_config lays it out, for verify_index."""
    stroom = gateway_from(ctx)
    index = await stroom.get_doc(INDEX_TYPE[backend], index_uuid)
    source = {'type': INDEX_TYPE[backend], 'uuid': index_uuid, 'name': index.get('name')}
    ref = await guard_from(ctx).create('Dashboard', name, build)
    doc = await stroom.get_doc('Dashboard', ref['uuid'])
    doc['dashboardConfig'] = dashboard_config(source, list(fields), time_field, window)
    doc = await stroom.put_doc(doc)
    return {'type': 'Dashboard', 'uuid': doc['uuid'], 'name': doc['name'], 'data_source': source, 'fields': fields}


SURVEY_FIELDS = 600        # fields read per survey; more are listed as not surveyed
_SURVEY_COLUMNS = 100      # columns per dashboard search


def writes_to(properties: dict[tuple[str, str], Any], doc_type: str, uuid: str, index_name: str | None) -> bool:
    """Whether a pipeline's effective properties write to the index: a Lucene IndexingFilter's index doc, or an
    Elasticsearch filter's index name (which may build the name from values, {_suffix}, or the doc may name a
    pattern or alias, ecs-windows*)."""
    for (_, name), held in properties.items():
        value = held.get('value') if isinstance(held, dict) and 'value' in held else held
        if doc_type == 'Index' and name == 'index' and isinstance(value, dict) and value.get('uuid') == uuid:
            return True
        if doc_type == 'ElasticIndex' and name == 'indexName' and isinstance(value, str) and index_name:
            written = re.sub(r'\{[^}]*\}', '*', value)
            if written == index_name or fnmatch.fnmatchcase(index_name, written) \
                    or fnmatch.fnmatchcase(written.replace('*', ''), index_name):
                return True
    return False


async def _index_fields(stroom, doc_type: str, ref: dict[str, Any]) -> list[dict[str, Any]]:
    """The fields Stroom has for an index doc; for Lucene, whether each is stored (only stored values can be shown)."""
    if doc_type == 'Index':
        found = await stroom.post('/index/v2/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 2000}})
        return [{'name': f['fldName'], 'type': (f.get('fldType') or '').lower(),
                 **({'stored': False} if f.get('stored') is False else {})}
                for f in found.get('values') or [] if f.get('fldName')]
    found = await stroom.post('/dataSource/v1/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 2000}})
    return [{'name': f['fldName'], 'type': (f.get('fldType') or '').lower()} for f in found.get('values') or [] if f.get('fldName')]


async def feeding_pipelines(ctx: Context, doc_type: str, uuid: str, index: dict[str, Any]) -> list[dict[str, Any]]:
    """The pipelines that write to the index, each with the index plan kept with its XSLT when there is one. Found
    by content (a Lucene IndexingFilter names the doc, an Elasticsearch one the index name), then each confirmed
    from its effective properties, so a mention elsewhere (a description, foo-v10 for foo-v1) does not count."""
    from tools.builds import kept_mapping
    from tools.pipelines import merge_layers
    stroom = gateway_from(ctx)
    index_name = index.get('indexName')
    needle = uuid if doc_type == 'Index' else re.split(r'[*{]', index_name or '')[0]
    if not needle:
        return []
    hits = await stroom.post('/explorer/v2/findInContent', {
        'filter': {'matchType': 'CONTAINS', 'pattern': needle, 'caseSensitive': False},
        'pageRequest': {'offset': 0, 'length': 100}})
    fed: list[dict[str, Any]] = []
    for value in hits.get('values') or []:
        doc = (value.get('docContentMatch') or {}).get('docRef') or {}
        if doc.get('type') != 'Pipeline' or any(p['uuid'] == doc.get('uuid') for p in fed):
            continue
        try:
            merged = merge_layers(await stroom.pipeline_layers(doc['uuid']))
        except ToolError:
            continue
        properties = {(q['element'], q['name']): q['value'] for q in merged['properties']}
        if not writes_to(properties, doc_type, uuid, index_name):
            continue
        kept = await kept_mapping(ctx, doc['uuid'])
        fed.append({'uuid': doc['uuid'], 'name': doc.get('name'), 'path': (value.get('path') or '').replace(' / ', '/'),
                    'plan': kept['payload'] if kept and kept['kind'] == 'index' else None})
    return fed


async def index_profile(ctx: Context, ref: dict[str, Any], time_field: str | None, names: list[str],
                        streams: int = 20) -> dict[str, Any]:
    """The whole index at a glance, through grouped dashboard searches: how many documents, the time they span and
    how many streams they came from; and, for the streams with the most documents, the feed and stream type each
    belongs to and the pipeline that produced it (from the stream's meta)."""
    from tools.streams import _meta
    stroom = gateway_from(ctx)

    def column(name: str, expression: str, **extra: Any) -> dict[str, Any]:
        return {'id': str(uuidlib.uuid4()), 'name': name, 'expression': expression, 'visible': True,
                'format': {'type': 'GENERAL'}, **extra}

    def probe(columns: list[dict[str, Any]], rows: int) -> dict[str, Any]:
        return {'uuid': str(uuidlib.uuid4()), 'name': f"{ref['name']} profile", 'dashboardConfig': {'components': [
            {'type': 'query', 'id': 'query-PROFILE', 'name': 'Query', 'settings': {
                'type': 'query', 'dataSource': ref, 'expression': {'type': 'operator', 'op': 'AND', 'children': []}}},
            {'type': 'table', 'id': 'table-PROFILE', 'name': 'Table', 'settings': {
                'type': 'table', 'queryId': 'query-PROFILE', 'fields': columns, 'extractValues': False,
                'maxResults': [rows], 'pageSize': rows}}]}}

    everything = {'type': 'operator', 'op': 'AND', 'children': []}
    totals = [column('all', "'all'", group=0), column('documents', 'count()')]
    if time_field in names:
        totals += [column('earliest', f'min(${{{time_field}}})'),
                   column('latest', f'max(${{{time_field}}})')]
    if 'StreamId' in names:
        totals.append(column('streams', 'countUnique(${StreamId})'))
    found = await _search(ctx, probe(totals, 1), everything, length=1)
    if not found['rows']:
        return {}
    row = found['rows'][0]
    profile: dict[str, Any] = {'documents': int(row.get('documents') or 0), 'earliest': row.get('earliest'),
                               'latest': row.get('latest'), 'streams': int(row['streams']) if row.get('streams') else None}
    if 'StreamId' not in names:
        return profile
    by_stream = await _search(ctx, probe([column('StreamId', '${StreamId}', group=0),
                                          column('documents', 'count()', sort={'order': 0, 'direction': 'DESCENDING'})],
                                         streams), everything, length=streams)
    sources: dict[tuple, dict[str, Any]] = {}
    pipelines: dict[str, str | None] = {}
    for r in by_stream['rows'][:streams]:
        try:
            meta = await _meta(stroom, int(r['StreamId']))
        except (ToolError, ValueError, TypeError):
            continue
        made_by = meta.get('pipelineUuid')
        if made_by and made_by not in pipelines:
            try:
                pipelines[made_by] = (await stroom.get_doc('Pipeline', made_by)).get('name')
            except ToolError:
                pipelines[made_by] = None
        key = (meta.get('feedName'), meta.get('typeName'), pipelines.get(made_by))
        entry = sources.setdefault(key, {'feed': key[0], 'type': key[1], 'produced_by': key[2], 'streams': 0, 'documents': 0})
        entry['streams'] += 1
        entry['documents'] += int(r.get('documents') or 0)
    for entry in sources.values():
        feed = next((v['docRef'] for v in (await stroom.find_documents(entry['feed'] or '', ['Feed'], 5)).get('values') or []
                     if v['docRef'].get('name') == entry['feed']), None)
        if feed:
            try:
                entry['description'] = ((await stroom.get_doc('Feed', feed['uuid'])).get('description') or '').strip()
            except ToolError:
                pass
    profile['sources'] = sorted(sources.values(), key=lambda e: -e['documents'])
    profile['streams_examined'] = len(by_stream['rows'][:streams])
    return profile


async def survey_index(ctx: Context, doc_type: str, uuid: str, max_documents: int = 100) -> dict[str, Any]:
    """What an existing index holds, surveyed through Stroom: the fields Stroom has for the index doc, the newest
    documents read through dashboard searches that are never saved (each field: how often the sample held it, its
    values), and the pipelines that feed it. A wide index is read in groups of columns, joined on StreamId and
    EventId; past SURVEY_FIELDS fields, the rest are listed as not surveyed."""
    stroom = gateway_from(ctx)
    index = await stroom.get_doc(doc_type, uuid)
    ref = {'type': doc_type, 'uuid': uuid, 'name': index.get('name')}
    fields = await _index_fields(stroom, doc_type, ref)
    names = [f['name'] for f in fields]
    time_field = (index.get('timeField') or index.get('timeFieldName')
                  or next((f['name'] for f in fields if f['type'] in ('date', 'date_field')), None))
    ids = [n for n in ('StreamId', 'EventId') if n in names]
    base = ids + ([time_field] if time_field in names and time_field not in ids else [])
    readable = [n for n in names if n not in base][:max(0, SURVEY_FIELDS - len(base))]
    width = max(1, _SURVEY_COLUMNS - len(base))
    groups = [readable[i:i + width] for i in range(0, len(readable), width)] or [[]]
    if len(ids) < 2:
        groups = groups[:1]    # without both ids, groups of columns cannot be joined: the first is read

    def probe(columns: list[str]) -> dict[str, Any]:
        cols = [{**_column(n), **({'sort': {'order': 0, 'direction': 'DESCENDING'}} if n == time_field else {})}
                for n in columns]
        return {'uuid': str(uuidlib.uuid4()), 'name': f"{index.get('name')} survey", 'dashboardConfig': {'components': [
            {'type': 'query', 'id': 'query-SURVEY', 'name': 'Query', 'settings': {
                'type': 'query', 'dataSource': ref, 'expression': {'type': 'operator', 'op': 'AND', 'children': []}}},
            {'type': 'table', 'id': 'table-SURVEY', 'name': 'Table', 'settings': {
                'type': 'table', 'queryId': 'query-SURVEY', 'fields': cols, 'extractValues': False,
                'maxResults': [max_documents], 'pageSize': max_documents}}]}}

    expression = {'type': 'operator', 'op': 'AND', 'children': []}
    first = await _search(ctx, probe(base + groups[0]), expression, length=max_documents)
    if not first['rows'] and time_field:
        # Some backends want a term: every time there is.
        expression = {'type': 'operator', 'op': 'AND', 'children': [
            {'type': 'term', 'field': time_field, 'condition': 'BETWEEN', 'value': '1970-01-01T00:00:00.000Z,day()+1d'}]}
        first = await _search(ctx, probe(base + groups[0]), expression, length=max_documents)
    errors = list(first['errors'])
    rows = [dict(r) for r in first['rows']]
    key = lambda r: tuple(str(r.get(n)) for n in ids)
    by_key = {key(r): r for r in rows}
    for group in groups[1:] if rows else []:
        more = await _search(ctx, probe(base + group), expression, length=max_documents)
        errors += more['errors']
        for r in more['rows']:
            if key(r) in by_key:
                by_key[key(r)].update({n: r.get(n) for n in group})
    surveyed = base + [n for g in groups for n in g]
    documents = [{n: [str(r[n])] if r.get(n) not in (None, '') else [] for n in surveyed} for r in rows]
    times = sorted(d[time_field][0] for d in documents if time_field and d.get(time_field))
    total = len(documents)
    profile: dict[str, Any] = {}
    if total:
        try:
            profile = await index_profile(ctx, ref, time_field, names)
        except ToolError as e:
            profile = {'error': str(e)}
    note = None
    if not total:
        note = ("No documents came back through Stroom. Stroom returns a hit only when its StreamId is a stream in "
                "this Stroom that you may read (it checks the stream's feed for every hit), so documents written from "
                "outside Stroom, without StreamId or with another system's, are not shown: the index is described "
                "from its mapping only, its values not read." if doc_type == 'ElasticIndex' else
                "No documents came back through Stroom: the index is empty, or holds no stored values to show.")
    return {'index': {**ref, 'path': await _path_of(stroom, ref), 'link': doc_link(stroom.settings, doc_type, uuid)},
            **({'note': note} if note else {}),
            'backend': 'lucene' if doc_type == 'Index' else 'elasticsearch',
            'index_name': index.get('indexName'), 'time_field': time_field, 'fields': fields,
            'surveyed_fields': surveyed, 'documents_sampled': total, 'profile': profile,
            'earliest': times[0] if times else None, 'latest': times[-1] if times else None,
            'populated': {n: round(100 * sum(1 for d in documents if d.get(n)) / total, 1) for n in surveyed} if total else {},
            'values': {n: list(dict.fromkeys(v for d in documents for v in d.get(n, [])))[:3] for n in surveyed},
            'fed_by': await feeding_pipelines(ctx, doc_type, uuid, index), 'documents': documents, 'errors': errors}


async def _path_of(stroom, ref: dict[str, Any]) -> str | None:
    found = (await stroom.find_documents(ref['name'], [ref['type']], 20)).get('values') or []
    return next(((v.get('path') or '').replace(' / ', '/') for v in found if v['docRef'].get('uuid') == ref['uuid']), None)


async def _search(ctx: Context, dashboard: dict[str, Any], expression: dict[str, Any], length: int = 100) -> dict[str, Any]:
    stroom = gateway_from(ctx)
    components = dashboard['dashboardConfig']['components']
    query = next(c for c in components if c['type'] == 'query')
    table = next(c for c in components if c['type'] == 'table')
    settings = table['settings']
    request = {
        'searchRequestSource': {'sourceType': 'DASHBOARD_UI', 'componentId': query['id'],
                                'ownerDocRef': {'type': 'Dashboard', 'uuid': dashboard['uuid'], 'name': dashboard['name']}},
        'search': {'dataSourceRef': query['settings']['dataSource'], 'expression': expression, 'incremental': True,
                   'componentSettingsMap': {table['id']: settings}},
        'componentResultRequests': [{'type': 'table', 'componentId': table['id'], 'fetch': 'ALL',
                                     'requestedRange': {'offset': 0, 'length': length}, 'tableName': table['name'],
                                     'tableSettings': {k: v for k, v in settings.items() if k in _TABLE_SETTINGS}}],
        'dateTimeSettings': {'localZoneId': 'UTC', 'referenceTime': int(time.time() * 1000)},
        'storeHistory': False, 'timeout': 5000}
    started = time.monotonic()
    while True:
        result = await stroom.post('/dashboard/v1/search', request)
        if result.get('complete') or time.monotonic() - started > 60:
            break
        request['queryKey'] = result.get('queryKey')
        await asyncio.sleep(0.5)
    table_result = next((r for r in result.get('results') or [] if r.get('componentId') == table['id']), {})
    columns = [f['name'] for f in settings['fields']]
    rows = [dict(zip(columns, row.get('values') or [])) for row in table_result.get('rows') or []]
    return {'rows': rows, 'errors': (result.get('errors') or []) + (table_result.get('errors') or [])}


Condition = Literal['EQUALS', 'NOT_EQUALS', 'CONTAINS', 'STARTS_WITH', 'ENDS_WITH', 'MATCHES_REGEX', 'GREATER_THAN',
                    'GREATER_THAN_OR_EQUAL_TO', 'LESS_THAN', 'LESS_THAN_OR_EQUAL_TO', 'BETWEEN', 'IN', 'IS_NULL',
                    'IS_NOT_NULL']


class SearchCheck(BaseModel):
    """One search, as a person would make it on a dashboard."""
    field: str
    condition: Condition = 'EQUALS'
    value: str = Field('', description="BETWEEN: 'from,to'; IN: values separated by commas; EQUALS takes * wildcards.")
    expected: int | None = Field(None, description="Rows it must return; none: at least one; 0: none (e.g. another case).")


async def _traced(ctx: Context, pipeline_uuid: str, row: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """A hit back to its record: step the indexing pipeline at the hit's StreamId and EventId (the record number,
    from 1) and read the document it writes there. The hit is traced when that document has the same EventId and
    holds the value searched for."""
    from tools.pipelines import translation_docs
    from utils.fielddoc import index_documents
    stroom = gateway_from(ctx)
    try:
        stream, event = int(float(row['StreamId'])), int(float(row['EventId']))
    except (KeyError, TypeError, ValueError):
        return {'traced': False, 'why': 'the hit has no StreamId and EventId to go back with'}
    element = next((e['element'] for e in translation_docs(pipeline_uuid, await stroom.pipeline_layers(pipeline_uuid))
                    if e['doc'].get('type') == 'XSLT' and not e['inherited_from_template']), None)
    from tools.stepping import step_pipeline
    stepped = await step_pipeline(ctx, pipeline_uuid, stream, event - 1)
    output = ((stepped.get('elements') or {}).get(element) or {}).get('output') or ''
    documents = index_documents([output]) if output else []
    document = next((d for d in documents if str(event) in [v.split('.')[0] for v in d.get('EventId', [])]), None)
    if document is None:
        return {'traced': False, 'stream': stream, 'event': event,
                'why': f'stepping record {event} of stream {stream} gives no document with EventId {event}'}
    field, value = check.get('field'), check.get('value')
    # A pattern (wildcard, or a CIDR range on an ip field) only needs the hit's record; a plain value must be there.
    holds = (check.get('condition', 'EQUALS') != 'EQUALS' or any(c in (value or '') for c in '*?/') or field not in document
             or any((value or '').lower() in v.lower() for v in document.get(field, [])))   # text fields match words
    return {'traced': holds, 'stream': stream, 'event': event,
            **({} if holds else {'why': f"record {event}'s document has {field} = {document.get(field)}, not {value}"})}


async def run_test_searches(
        ctx: Context,
        dashboard_uuid: Annotated[str, Field(description="A verification dashboard.")],
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams that were indexed.")],
        expected_documents: Annotated[int, Field(description="Events records in those streams.")],
        exact: Annotated[list[dict[str, str]] | str, ONE_OR_MORE, Field(
            description="Exact-match checks, each {'field': ..., 'value': ...} using values from stepped documents; "
                        "each must return at least one row.")] = [],
        time_range: Annotated[dict[str, Any] | None, Field(
            description="{'field': ..., 'from': ISO, 'to': ISO, 'expected': n}")] = None,
        retries: Annotated[int, Field(ge=0, le=20, description="Retries while the index catches up.")] = 6,
        searches: list[SearchCheck] | None = None,
        pipeline_uuid: str | None = None,
        dashboard_doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Run test searches through the verification dashboard, the way people will search: all documents for the
    indexed stream ids (count must match), an exact match per key field, a time range, and any other searches
    (ranges, IN, wildcards, a value in another case that must find nothing). Each check passes or fails with the
    rows it returned. With the indexing pipeline, each check's first hit is traced back to its record. A
    failure points at the mapping or the indexing XSLT.
    """
    dashboard = dashboard_doc or await gateway_from(ctx).get_doc('Dashboard', dashboard_uuid)
    term = lambda f, c, v: {'type': 'term', 'field': f, 'condition': c, 'value': str(v)}
    by_stream = {'type': 'operator', 'op': 'OR', 'children': [term('StreamId', 'EQUALS', i) for i in stream_ids]}
    for attempt in range(retries + 1):
        found = await _search(ctx, dashboard, by_stream)
        if len(found['rows']) >= expected_documents or attempt == retries:
            break
        await asyncio.sleep(5)
    checks = [{'check': f'documents for streams {stream_ids}', 'expected': expected_documents,
               'returned': len(found['rows']), 'pass': len(found['rows']) == expected_documents,
               'errors': found['errors'], 'sample': found['rows'][:3]}]
    for item in exact:
        res = await _search(ctx, dashboard, {'type': 'operator', 'op': 'AND', 'children': [
            term(item['field'], 'EQUALS', item['value'])]})
        checks.append({'check': f"{item['field']} = {item['value']}", 'returned': len(res['rows']),
                       'pass': len(res['rows']) >= 1, 'errors': res['errors'], 'sample': res['rows'][:2]})
    if time_range:
        res = await _search(ctx, dashboard, {'type': 'operator', 'op': 'AND', 'children': [
            term(time_range['field'], 'BETWEEN', f"{time_range['from']},{time_range['to']}")]})
        checks.append({'check': f"{time_range['field']} between {time_range['from']} and {time_range['to']}",
                       'expected': time_range.get('expected'), 'returned': len(res['rows']),
                       'pass': len(res['rows']) == time_range.get('expected', len(res['rows'])), 'errors': res['errors']})
    for search in searches or []:
        res = await _search(ctx, dashboard, {'type': 'operator', 'op': 'AND', 'children': [
            term(search.field, search.condition, search.value)]})
        n = len(res['rows'])
        checks.append({'check': f"{search.field} {search.condition} {search.value}".rstrip(), 'expected': search.expected,
                       'returned': n, 'pass': (n >= 1 if search.expected is None else n == search.expected)
                       and not res['errors'], 'errors': res['errors'], 'sample': res['rows'][:2],
                       '_first': res['rows'][0] if res['rows'] else None,
                       '_spec': {'field': search.field, 'condition': search.condition, 'value': search.value}})
    if pipeline_uuid:
        for check, item in zip(checks[1:1 + len(exact)], exact):
            check['_first'] = check.get('sample', [None])[0] if check.get('sample') else None
            check['_spec'] = {'field': item['field'], 'condition': 'EQUALS', 'value': item['value']}
        first = found['rows'][0] if found['rows'] else None
        checks[0]['_first'], checks[0]['_spec'] = first, {}
        for check in checks:
            if check.get('_first'):
                check['trace'] = await _traced(ctx, pipeline_uuid, check['_first'], check['_spec'])
                check['pass'] = check['pass'] and check['trace']['traced']
    for check in checks:
        check.pop('_first', None)
        check.pop('_spec', None)
    return {'passed': all(c['pass'] for c in checks), 'checks': checks}


async def _destination(ctx: Context, pipeline_uuid: str) -> dict[str, Any]:
    destination = await elastic_destination(gateway_from(ctx), pipeline_uuid)
    if not destination or not destination.get('index name'):
        raise ToolError("That is not an Elasticsearch indexing pipeline with an indexName set. An example index "
                        "template the user gave before the indexing pipeline exists is used to draft it: "
                        "draft_index_mapping example_template= it names the fields after it; once the indexing "
                        "pipeline is made and stepped, propose_index_template with it builds the index template.")
    return destination


async def _documents(ctx: Context, pipeline_uuid: str, stream_ids: list[int], cap: int) -> list[dict[str, Any]]:
    """The documents the indexing pipeline would send to Elasticsearch, by stepping its XSLT over Events."""
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    outputs = await _outputs(stroom, pipeline, stream_ids, pipeline.default_outputs()[-1], None, cap)
    docs = [d for xml in outputs.values() for d in json_xml_documents(xml)]
    if not docs:
        raise ToolError("Stepping the indexing pipeline gave no documents; step_sample it first and fix its XSLT")
    return docs


def _component_notes(missing: list[str]) -> list[str]:
    """This server doesn't read Elasticsearch: component templates the user hasn't given can't be checked."""
    return [f"composed_of {missing} not given, so not checked: their fields aren't seen here. Ask the user for them "
            f"(GET _component_template/<name>) and check again with component_templates."] if missing else []


def _template_summary(name: str, body: dict[str, Any], components: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    """The template in lines a confirmation form shows readably: the user reviews the whole request in the chat
    first (seen in VS Code: the form showed it as one long unformatted line, and an invalid template was agreed)."""
    template = body.get('template') or {}
    mappings = template.get('mappings') or {}
    fields = [(path, spec.get('type')) for path, spec in read_mapping_fields(body).items()
              if spec.get('type') not in (None, 'object')]
    settings = template.get('settings') or {}
    settings = settings.get('index', settings) if isinstance(settings, dict) else {}

    def flat(node: Any, prefix: str = '') -> list[str]:
        if isinstance(node, dict):
            return [line for k, v in node.items() for line in flat(v, f'{prefix}{k}.')]
        return [f"{prefix[:-1]} {node}"]
    patterns = body.get('index_patterns') or []
    return {'index template': f"{name}: the Dev Tools request shown in the chat",
            'applies to': f"{', '.join(patterns if isinstance(patterns, list) else [patterns])} (priority "
                          f"{body.get('priority')})" + (', as a data stream' if 'data_stream' in body else ''),
            **({'composed of': body['composed_of']} if body.get('composed_of') else {}),
            **({'settings': ', '.join(flat(settings))} if settings else {}),
            'fields': f"{len(fields)}: " + ', '.join(f"{p} ({t})" for p, t in fields[:30])
                      + (f" (+{len(fields) - 30})" if len(fields) > 30 else ''),
            'unmapped fields': {'false': 'kept in _source, not searchable', 'strict': 'refused',
                                'runtime': 'runtime fields'}.get(str(mappings.get('dynamic')).lower(),
                                                                'added by Elasticsearch (dynamic mapping)'),
            **({'component templates': sorted(components)} if components else {}),
            **({'notes': notes} if notes else {})}


async def _agree(ctx: Context, action: str, pipeline_uuid: str, destination: dict[str, Any], name: str,
                 body: dict[str, Any], components: dict[str, Any], notes: list[str],
                 confirmation_id: str | None, reviewed: bool = False) -> Any:
    """The user reviews the index template in the chat, then confirms it in a short form; once they have, it is
    kept with the pipeline as the agreed one, which create_processor_filter asks them to confirm is committed to the
    cluster. None once agreed."""
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Pipeline', pipeline_uuid)
    await guard_from(ctx).check_managed({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': doc.get('name')})
    dev_tools = f"PUT _index_template/{name}\n{json.dumps(body, indent=2)}"
    if not reviewed:
        return {'status': 'needs_review', 'dev_tools': dev_tools,
                'hint': f"Show the user this index template before anything else: dev_tools in the chat as a json code "
                        f"block, exactly as it is (a form can't show it readably), with the notes on what came from "
                        f"where, and say it is not to go on the cluster until they've confirmed it. Then, in the same "
                        f"turn, without waiting for a reply, call {action} again with the same arguments (example_template "
                        f"and component_templates included) plus reviewed=true: the form that opens is where they "
                        f"confirm it or say what to change. Seen: an agent ended its turn after showing it, and the user "
                        f"committed the template before it was agreed."}
    gate = await consent_from(ctx).require(
        ctx, 'confirmation', action, f"Use Elasticsearch index template '{name}' for index "
        f"'{destination['index name']}' (cluster {destination['cluster']}), as shown in the chat",
        _template_summary(name, body, components, notes), confirmation_id)
    if gate:
        return gate
    doc['description'] = with_agreed_template(doc.get('description'), {
        'name': name, 'index': destination['index name'], 'cluster': destination['cluster'],
        'component_templates': sorted(components), 'xslt': await indexing_xslt_digest(stroom, pipeline_uuid),
        'agreed': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'dev_tools': dev_tools})
    await stroom.put_doc(doc)
    return None


def _asking(gate: Any, result: dict[str, Any]) -> Any:
    """The confirmation to return, with what the agent shows alongside it (a form result goes back unchanged)."""
    if not isinstance(gate, dict):
        return gate
    return {**gate, **{k: result[k] for k in ('compatible', 'blocking', 'pipeline_changes', 'template_name', 'index',
                                             'cluster', 'self_check', 'from_example', 'notes', 'component_templates')
                       if k in result},
            'hint': gate['hint'] + " If the user corrects the template instead, check_index_template with their "
                                   "version (and the component templates): it is confirmed there."}


async def propose_index_template(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The candidate Elasticsearch indexing pipeline.")],
        plan: Annotated[FieldPlan, Field(description="The field plan from draft_index_mapping (backend elasticsearch).")],
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams to check the template against.")],
        example_template: Annotated[str | None, Field(
            description="The user's example: the Elasticsearch index template a sibling source's index uses (GET "
                        "_index_template/<name>, or a Dev Tools request), or an existing index's mapping (GET "
                        "<index>/_mapping). Ask the user for it: the final template follows it.")] = None,
        component_templates: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Only if the example lists any in composed_of: those component templates, as the user gave them (GET "
                        "_component_template/<name>, or PUT _component_template/<name> {...}).")] = [],
        template_name: Annotated[str | None, Field(description="Template name; defaults to the index name.")] = None,
        priority: Annotated[int, Field(ge=0)] = 200,
        without_example: Annotated[bool, Field(
            description="Only when the user has said they have no example: the template is built from the field plan "
                        "alone, and they confirm it as shown.")] = False,
        reviewed: Annotated[bool, Field(description="True once the user has been shown the template (dev_tools) in the "
                                                    "chat, after a needs_review reply: they then confirm it in a form.")] = False,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Build the final Elasticsearch index template (not a Stroom pipeline template) for the indexing pipeline's
    destination index, for the cluster admin to apply: from the user's example index template or mapping and
    any component templates it is composed of, following their conventions: their settings and composed_of,
    the new index's own pattern, fields they map left as they map them, new fields mapped in their style for
    the type (keyword ignore_above, text sub-fields, date formats), and names unlike theirs reported to rename
    in the field plan. Without an example, nothing is built until the user says they have none
    (without_example), and then from the field plan alone. Checked against the documents the
    pipeline writes; when it fits, the user confirms it as shown (or corrects it: check_index_template), and the
    agreed template is kept with the pipeline. The user then has the cluster admin commit it, and
    create_processor_filter starts indexing once they confirm that.
    """
    if plan.backend != 'elasticsearch':
        raise ToolError("Index templates are for Elasticsearch; Lucene fields are set with create_index_doc (plan=...)")
    destination = await _destination(ctx, pipeline_uuid)
    index = destination['index name']
    name = template_name or index
    body = plan.model_copy(update={'index_name': index}).elastic_template(name, priority)['body']
    notes, components = [], {}
    kept_note = None
    if not without_example and (not example_template or not _is_template(example_template)):
        kept = await _kept_example(ctx, pipeline_uuid, [index, plan.index_name])
        if kept:
            kept_note = ("the example index template the user pasted, kept in the build when the plan was drafted"
                         + ("; what was given as example_template is not an index template" if example_template else ""))
            example_template, component_templates = kept
    if example_template:
        try:
            _, example = parse_template(example_template)
            components = parse_component_templates([component_templates] if isinstance(component_templates, str)
                                                   else list(component_templates))
        except ValueError as e:
            raise ToolError(str(e)) from e
        body, notes = from_example(body, example, components, discovery=plan.discovery is not None)
        if kept_note:
            notes.insert(0, kept_note)
    stroom = gateway_from(ctx)
    if not example_template and not without_example:
        # No template to commit yet: one handed out now would be applied to the cluster without the user's
        # conventions, and without being agreed here, so indexing would still be refused.
        return {'agreed': False, 'needs': 'example_template', 'index': index, 'cluster': destination['cluster'],
                'pipeline_link': doc_link(stroom.settings, 'Pipeline', pipeline_uuid),
                'hint': "No example was given, so no template is built: call again with example_template= the index "
                        "template (or an index's mapping) a sibling " + ("discovery index" if plan.discovery else
                        "source's index") + " uses, exactly as the user pasted it, and its component templates. If "
                        "it is not in this conversation, ask the user to paste it and end your turn. Only if they say "
                        "they have none: without_example=true, and they confirm the template built from the plan."}
    composed, _ = compose(body, components)
    await _require_input(ctx, pipeline_uuid, events_stream_ids, plan)
    check = compare(composed, await _documents(ctx, pipeline_uuid, events_stream_ids, 50), index)
    text = json.dumps(body, indent=2)
    if not example_template:
        notes = ["built from the field plan alone: the user has no example index template"]
    result = {'template_name': name, 'index': index, 'cluster': destination['cluster'], 'template': body,
              'dev_tools': f"PUT _index_template/{name}\n{text}", 'self_check': check,
              **({'from_example': notes} if example_template else {}),
              'pipeline_link': doc_link(stroom.settings, 'Pipeline', pipeline_uuid)}
    if not check['compatible']:
        result['hint'] = ("It does not fit the documents the pipeline writes (self_check): show the user and ask "
                          "whether to change the indexing XSLT or the template. Not yet shown for confirmation.")
        return result
    gate = await _agree(ctx, 'propose_index_template', pipeline_uuid, destination, name, body, components, notes,
                        confirmation_id, reviewed)
    if gate:
        return _asking(gate, result)
    result.update({'agreed': True, 'hint': (
        "The user agreed this index template; it is kept with the pipeline. Give them dev_tools for the cluster admin "
        "to commit to the cluster, and end your turn. Once they say it is committed, create_processor_filter: its "
        "approval asks them to confirm that, and starts indexing.")})
    return result


async def check_index_template(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The candidate Elasticsearch indexing pipeline.")],
        template: Annotated[str, Field(description="The Elasticsearch index template as the user gave it: a Dev Tools "
                                                   "request (PUT _index_template/name {...}), the JSON body, or GET "
                                                   "_index_template output; or an existing index's mapping (GET "
                                                   "<index>/_mapping) as the example.")],
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams to step the pipeline over.")],
        component_templates: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Only if the index template lists any in composed_of: those component templates, as the user gave them: each a Dev "
                        "Tools request (PUT _component_template/name {...}) or GET _component_template output.")] = [],
        max_records: Annotated[int, Field(ge=1, le=500)] = 50,
        reviewed: Annotated[bool, Field(description="True once the user has been shown the template (dev_tools) in the "
                                                    "chat, after a needs_review reply: they then confirm it in a form.")] = False,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Check a user's Elasticsearch index template (not a Stroom pipeline template), with the component templates
    it is composed of, against the candidate indexing pipeline: does it apply to the pipeline's index, and can
    it take every field the pipeline writes (types, date formats, dynamic setting, object clashes, renamed or
    dropped fields)? Components are merged as Elasticsearch composes them. An existing index's mapping may be
    given instead. Returns whether it is compatible and each change needed, mostly to the indexing XSLT, to
    flag to the user before anything is changed. A compatible index template (with index_patterns) is shown to
    the user to confirm, and kept with the pipeline as the agreed one.
    """
    try:
        name, body = parse_template(template)
        components = parse_component_templates([component_templates] if isinstance(component_templates, str)
                                               else list(component_templates))
    except ValueError as e:
        raise ToolError(str(e)) from e
    body, missing = compose(body, components)
    destination = await _destination(ctx, pipeline_uuid)
    await _require_input(ctx, pipeline_uuid, events_stream_ids)
    result = compare(body, await _documents(ctx, pipeline_uuid, events_stream_ids, max_records),
                     destination['index name'])
    result['notes'] = _component_notes(missing) + result['notes']
    if components:
        result['component_templates'] = sorted(components)
    result.update({'template_name': name, 'index': destination['index name'], 'cluster': destination['cluster'],
                   'pipeline_link': doc_link(gateway_from(ctx).settings, 'Pipeline', pipeline_uuid)})
    if not result['compatible']:
        result['hint'] = ("Show the user pipeline_changes and ask whether to make them (update the indexing XSLT, step "
                          "again) or to change the template instead. Nothing has been changed.")
        return result
    _, own = parse_template(template)
    if 'index_patterns' not in own:
        result['hint'] = ("Compatible, but this is a mapping, not an index template: propose_index_template with it as "
                          "example_template builds the index template to agree.")
        return result
    gate = await _agree(ctx, 'check_index_template', pipeline_uuid, destination, name or destination['index name'],
                        own, components, result['notes'], confirmation_id, reviewed)
    if gate:
        return _asking(gate, result)
    result.update({'agreed': True, 'hint': (
        "The user agreed this index template; it is kept with the pipeline. Once they say the cluster admin has "
        "committed it to the cluster, create_processor_filter: its approval asks them to confirm that, and starts "
        "indexing.")})
    return result


def _cidr(value: str) -> str:
    """The CIDR range a wildcard address means: 192.0.2.* is 192.0.2.0/24."""
    known = [p for p in value.split('*')[0].split('.') if p.isdigit()][:3]
    return f"{'.'.join(known + ['0'] * (4 - len(known)))}/{8 * len(known)}" if known else '10.0.0.0/8'


def _searchable(backend: str, searches: list[SearchCheck], ip_fields: set[str] = frozenset(),
                exact: list[dict[str, str]] = ()) -> None:
    """Searches whose answer would mislead: on Elasticsearch, Stroom (7.13) finds nothing for STARTS_WITH and
    CONTAINS, nor for a wildcard on an ip field, and matches every document for IS_NULL and IS_NOT_NULL, without an
    error; IN takes values separated by commas."""
    problems = []
    for e in exact:
        if backend == 'elasticsearch' and e.get('field') in ip_fields and '*' in str(e.get('value')):
            problems.append(f"{e['field']} = '{e['value']}': {e['field']} is an ip field, where a wildcard finds "
                            f"nothing; use the CIDR range '{_cidr(str(e['value']))}'")
    for s in searches:
        if backend == 'elasticsearch' and s.field in ip_fields and s.condition == 'EQUALS' and '*' in s.value:
            # Seen in VS Code: IpAddress EQUALS 192.0.2.* found 0 of the 10 documents it should have.
            problems.append(f"{s.field} EQUALS '{s.value}': {s.field} is an ip field, where a wildcard finds nothing; "
                            f"use EQUALS '{_cidr(s.value)}' (a CIDR range)")
        if s.condition == 'IN' and ',' not in s.value and ' ' in s.value.strip():
            problems.append(f"{s.field} IN '{s.value}': separate the values with commas")
        if backend == 'elasticsearch' and s.condition in ('STARTS_WITH', 'CONTAINS'):
            pattern = f"{s.value}*" if s.condition == 'STARTS_WITH' else f"*{s.value}*"
            problems.append(f"{s.field} {s.condition} '{s.value}': Stroom finds nothing with {s.condition} on "
                            f"Elasticsearch; use EQUALS '{pattern}' (a wildcard) or MATCHES_REGEX")
        if backend == 'elasticsearch' and s.condition == 'IS_NOT_NULL':
            problems.append(f"{s.field} IS_NOT_NULL: Stroom matches every document with IS_NOT_NULL on Elasticsearch; "
                            f"use EQUALS '*' (any value) instead")
        if backend == 'elasticsearch' and s.condition == 'IS_NULL':
            problems.append(f"{s.field} IS_NULL: Stroom matches every document with IS_NULL on Elasticsearch; check "
                            f"EQUALS '*' (any value) with the expected count instead")
    if problems:
        raise ToolError('These searches would give a misleading answer: ' + '; '.join(problems))


async def verify_index(
        ctx: Context,
        build: Build,
        index_uuid: Annotated[str, Field(description="The index doc that was indexed into.")],
        backend: Backend,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams that were indexed.")],
        expected_documents: Annotated[int, Field(description="Events records in those streams.")],
        fields: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="The dashboard table's columns, which the user chose: suggest the time field and the plan's key "
                        "fields (user, host, address, event type, outcome) and confirm them with the user. StreamId and "
                        "EventId are kept as hidden columns, for the text pane and tracing hits.")],
        exact: Annotated[list[dict[str, str]] | str, ONE_OR_MORE, Field(
            description="Exact-match checks, each {'field': ..., 'value': ...} using values from stepped documents; "
                        "each must return at least one row.")] = [],
        time_range: Annotated[dict[str, Any] | None, Field(
            description="{'field': ..., 'from': ISO, 'to': ISO, 'expected': n}")] = None,
        dashboard_name: Annotated[str | None, Field(description="Defaults to the index name with a -VERIFY suffix.")] = None,
        retries: Annotated[int, Field(ge=0, le=20, description="Retries while the index catches up.")] = 6,
        searches: Annotated[list[SearchCheck] | str, ONE_OR_MORE, Field(
            description="More searches, as people will make them: numeric ranges (GREATER_THAN, BETWEEN), IN, "
                        "wildcards (EQUALS 'adm*'), CONTAINS on text, and a value in another case that must find "
                        "nothing (expected 0). Use values from stepped documents.")] = [],
        pipeline_uuid: Annotated[str | None, Field(
            description="The indexing pipeline: each check's first hit is traced back to its record by stepping it "
                        "at the hit's StreamId and EventId.")] = None,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Verify indexed events through Stroom, not by querying the backend: a workspace dashboard on the index doc,
    then the test searches: the sample stream ids, an exact match on each key field, a time range and any further
    searches; with the indexing pipeline, each hit traced back to the record it came from. Passes when every check
    returns what it should. The dashboard (created once per build, for an index the build made, after the user
    confirms its columns) has a query on the time field from the sample's earliest event (rounded back to a 30-day
    boundary) through the end of today, a table of the user's fields newest first, and a text pane on the selected row's record
    with stepping. An index from elsewhere is searched without saving a dashboard.
    """
    searches = [SearchCheck.model_validate(s) if isinstance(s, dict) else s for s in searches]
    _searchable(backend, searches)
    stroom = gateway_from(ctx)
    index = await stroom.get_doc(INDEX_TYPE[backend], index_uuid)
    if backend == 'elasticsearch':
        # Which fields are addresses, from the doc's own field list or Stroom's: a wildcard on them finds nothing.
        typed = {f.get('fldName'): (f.get('nativeType') or f.get('fldType') or '').lower() for f in index.get('fields') or []}
        if not typed:
            try:
                ref = {'type': 'ElasticIndex', 'uuid': index_uuid, 'name': index.get('name')}
                typed = {f['name']: f['type'] for f in await _index_fields(stroom, 'ElasticIndex', ref)}
            except Exception:    # a check on the searches, not a reason for verification to fail
                typed = {}
        _searchable(backend, searches, {n for n, t in typed.items() if t in ('ip', 'ipv4_address')},
                    [e for e in exact if isinstance(e, dict)])
    name = dashboard_name or f"{index.get('name')}-VERIFY"
    fields = [f for f in fields if f not in _IDS]
    time_field = index.get('timeField') or index.get('timeFieldName')
    source = {'type': INDEX_TYPE[backend], 'uuid': index_uuid, 'name': index.get('name')}
    contents = await guard_from(ctx).folder_contents(build)
    ours = any(d['uuid'] == index_uuid for d in contents)
    existing = next((d for d in contents if d['type'] == 'Dashboard' and d['name'] == name), None)
    # The window starts at the sample's earliest event: found by searching its streams first, saving nothing.
    probe = {'uuid': str(uuidlib.uuid4()), 'name': name, 'dashboardConfig': dashboard_config(source, fields, time_field, None)}
    sample = await _search(ctx, probe, {'type': 'operator', 'op': 'OR', 'children': [
        {'type': 'term', 'field': 'StreamId', 'condition': 'EQUALS', 'value': str(i)} for i in stream_ids]})
    window = window_start([r.get(time_field) for r in sample['rows']]) if time_field else None
    design = {'columns (newest first)': fields, 'hidden': list(_IDS) + ([time_field] if time_field and time_field not in fields else []),
              'initial query': f"{time_field} from {window} through today" if window else 'none',
              'text pane': "the selected row's record, with stepping; no extraction pipeline"}
    dashboard: dict[str, Any] | None = None
    if not ours:
        # Another build's (or production's) index: searched through an unsaved dashboard, so nothing lands here.
        dashboard = {'uuid': probe['uuid'], 'name': name, 'saved': False}
        doc = {**probe, 'dashboardConfig': dashboard_config(source, fields, time_field, window)}
    elif existing:
        doc = await stroom.get_doc('Dashboard', existing['uuid'])
        table = next(c for c in doc['dashboardConfig']['components'] if c['type'] == 'table')
        shown = [c['name'] for c in table['settings']['fields'] if c.get('visible', True)]
        if 'layoutConstraints' not in doc['dashboardConfig']:
            # Made before dashboards were laid out as Stroom's UI needs (it showed them empty): laid out again,
            # its columns kept, so it shows what it searches.
            doc['dashboardConfig'] = dashboard_config(source, shown, time_field, window)
            doc = await stroom.put_doc(doc)
        if shown != fields:
            gate = await consent_from(ctx).require(ctx, 'confirmation', 'verify_index',
                                                   f"Change the columns of dashboard '{name}'", design, confirmation_id)
            if gate:
                return gate
            doc['dashboardConfig'] = dashboard_config(source, fields, time_field, window)
            doc = await stroom.put_doc(doc)
        dashboard = {'uuid': doc['uuid'], 'name': name, 'saved': True}
    else:
        gate = await consent_from(ctx).require(
            ctx, 'confirmation', 'verify_index', f"Create the verification dashboard '{name}' on index "
            f"'{index.get('name')}'", design, confirmation_id)
        if gate:
            return gate
        made = await create_verification_dashboard(ctx, build, name, index_uuid, backend, fields, time_field, window)
        doc = await stroom.get_doc('Dashboard', made['uuid'])
        dashboard = {'uuid': made['uuid'], 'name': name, 'saved': True}
    searched = await run_test_searches(ctx, dashboard['uuid'], stream_ids, expected_documents, exact, time_range, retries,
                                       searches=list(searches), pipeline_uuid=pipeline_uuid, dashboard_doc=doc)
    # The link to give the user (seen in VS Code: the user had to ask for it); an unsaved dashboard has none.
    link = {'link': doc_link(stroom.settings, 'Dashboard', dashboard['uuid'])} if dashboard.get('saved') else {}
    result = {'dashboard': {**dashboard, **link, **design}, **searched}
    if searched['passed']:
        # The plan's 'indexed' step is done for the build's pipelines that write to this index.
        recorded = []
        for d in contents:
            if d['type'] != 'Pipeline':
                continue
            merged = merge_layers(await stroom.pipeline_layers(d['uuid']))
            properties = {(q['element'], q['name']): q['value'] for q in merged['properties']}
            if writes_to(properties, INDEX_TYPE[backend], index_uuid, index.get('indexName')) \
                    and await remember_verified(ctx, {'type': 'Pipeline', 'uuid': d['uuid'], 'name': d['name']}):
                recorded.append(d['name'])
        if recorded:
            result['verified'] = recorded
            from tools.plan import with_next
            return await with_next(ctx, build, result)
    return result


ALL_TOOLS = [get_field_conventions, draft_index_mapping, propose_index_template, check_index_template,
             find_elastic_clusters, create_index_doc, create_indexing_pipeline, verify_index]
