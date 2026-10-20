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
from pydantic import BaseModel, Field, PrivateAttr

from security.guard import guard_from
from tools.explorer import _redact
from tools.pipeline_writes import PropertyValue, create_pipeline
from tools.processing_writes import elastic_destination, indexing_xslt_digest
from tools.pipelines import merge_layers
from tools.stepping import _outputs, _Pipeline, remember_verified
from tools.streams import _meta, summarise_events
from tools.templates import _shape
from utils.consent import consent_from, edited, review_id, review_note, was_reviewed
from utils.fielddoc import index_field_mapping_markdown
from utils.fieldplan import Backend, Discovery, FieldPlan, PlannedField, any_action, population_of, source_matches
from utils.xsltgen import SharedTemplate
from utils.mappingstore import read_mapping, with_agreed_template
from utils.params import ONE_OR_MORE
from utils.progress import progress_from
from utils.stroom import doc_link, gateway_from, refused_reason, set_body_text
from utils.sharedxslt import json_values
from utils.templatecheck import dev_tools as render_dev_tools
from utils.templatecheck import (compare, compose, composition, from_example, json_xml_documents, names_from_example,
                                 parse_component_templates, read_mapping_fields,
                                 parse_template, with_components)

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
PASTED = 'as the user pasted it'
FIELDS_ONLY = 'its fields and their Elasticsearch types'
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


async def _keep_example(ctx: Context, build: str, index_name: str, example: str, components: list[str],
                        how: str = PASTED) -> None:
    """Keep the example index template the user pasted (and its components) in the build, verbatim: a summarised
    conversation lost it, and the agent proposed a template of its own as the user's example. how: where it came
    from, when not pasted (an existing index the user chose to follow, read through Stroom)."""
    stroom, guard = gateway_from(ctx), guard_from(ctx)
    text = (f"# Example index template for {index_name}\n\n{how[0].upper()}{how[1:]}, kept so the index template "
            f"follows it even if the conversation no longer holds it.\n\n```\n{example.strip()}\n```\n"
            + ''.join(f"\n```\n{c.strip()}\n```\n" for c in components)
            + f"\n<!-- stroom-mcp example\n{json.dumps({'example': example, 'components': components, 'how': how})}"
              f"\n-->\n")
    name = f"{index_name}{EXAMPLE_SUFFIX}"

    async def write(ref: dict[str, Any]) -> dict[str, Any]:
        doc = await stroom.get_doc('Documentation', ref['uuid'])
        set_body_text(doc, text)
        return await stroom.put_doc(doc)
    there = next((d for d in await guard.folder_contents(build) if d['type'] == 'Documentation' and d['name'] == name), None)
    if there:
        await write(there)
    else:
        await guard.create_filled('Documentation', name, build, write, role='Indexing')


async def _kept_example(ctx: Context, pipeline_uuid: str,
                        index_names: list[str]) -> tuple[str, list[str], str] | None:
    """The example kept in the indexing pipeline's build when the plan was drafted: (template, components, where it
    came from)."""
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
        return (kept['example'], list(kept.get('components') or []), kept.get('how') or PASTED) if kept else None
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


_COMPONENTS = 'component_templates_given'


def _given_components(ctx: Context, given: Any, names: list[str], example: Any = None) -> dict[str, dict[str, Any]]:
    """The component templates given now, by name, with those of names given earlier in the session: kept so the agent
    needn't send them again. Seen in testing: re-sent call after call, the bodies came back corrupted (a GET response
    nested in another, an escaped string), and the user's template could never be agreed. The example (or template)
    pasted from Stroom's elastic_index_templates tool holds its component templates too: one paste. Raises
    ValueError for one that can't be read."""
    from tools.plan import _user
    from utils.templatecheck import stroom_tool_templates
    texts = [given] if isinstance(given, (str, dict)) else list(given or [])
    parsed = parse_component_templates(texts + ([example] if stroom_tool_templates(example) else []))
    try:
        kept = ctx.lifespan_context.setdefault(_COMPONENTS, {}).setdefault(_user(ctx), {})
    except Exception:   # no session store: only what was given
        return parsed
    now = time.time()
    for name in [n for n, v in kept.items() if now - v['at'] > _NAMING_TTL]:
        del kept[name]
    kept.update({n: {'body': b, 'at': now} for n, b in parsed.items()})
    return {**{n: kept[n]['body'] for n in names if n in kept}, **parsed}


async def _index_doc(ctx: Context, index: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(ref, doc): an Elastic Index doc by uuid, else by its exact name."""
    stroom = gateway_from(ctx)
    try:
        doc = await stroom.get_doc('ElasticIndex', index)
        return {'type': 'ElasticIndex', 'uuid': index, 'name': doc.get('name')}, doc
    except ToolError:
        found = [v['docRef'] for v in (await stroom.find_documents(index, ['ElasticIndex'], 20)).get('values') or []
                 if v['docRef'].get('name') == index]
        if len(found) != 1:
            raise ToolError(f"No single Elastic Index doc named '{index}': give its uuid (get_field_conventions "
                            f"backend=elasticsearch lists them)")
        return found[0], await stroom.get_doc('ElasticIndex', found[0]['uuid'])


TEMPLATES_READ = '/elasticIndex/v1/templates'


async def _template_from_index(ctx: Context, index: str) -> dict[str, Any] | None:
    """The index template an Elastic Index doc's indices take, and its component templates, read by Stroom through
    the doc's cluster (dev/stroom-requests/elastic-index-templates.md): asked for by the user, as getting them from
    Kibana to paste was cumbersome. {'doc', 'index', 'name', 'template' (a Dev Tools request), 'components' (one each),
    'composed_of', 'missing' (named, but not on the cluster), 'unread' {name: why}} when the template was read;
    {'doc', 'why'} when it wasn't (most often the cluster's credentials lack the Elasticsearch privileges, as the user
    warned); None on a Stroom that can't read templates, or where the doc can't be found (_example_from_index then says
    so)."""
    try:
        ref, doc = await _index_doc(ctx, index)
    except Exception:
        return None
    try:
        found = await gateway_from(ctx).post(TEMPLATES_READ, {'indexDocRef': ref})
    except ToolError as e:
        if re.search(r'\((404|405)\)', str(e)):
            return None         # a Stroom without the endpoint
        return {'doc': ref['name'], 'why': f"Stroom couldn't read its index template: {refused_reason(e)[:160]}"}
    except Exception:           # the endpoint is a convenience: the doc's fields still serve
        return None
    template = found.get('indexTemplate') or {}
    if template.get('status') != 'OK' or not template.get('body'):
        why = {'NOT_FOUND': 'no index template on the cluster matches its index',
               'FORBIDDEN': "the cluster's credentials in Stroom may not read index templates"}.get(template.get('status'), '')
        message = (template.get('message') or '')[:160]
        return {'doc': ref['name'], 'why': f"Stroom couldn't read its index template ({why or template.get('status')}"
                                           + (f": {message}" if message else '') + ')'}
    from utils import ecs
    components, missing, unread = [], [], {}
    for c in found.get('componentTemplates') or []:
        if c.get('name') == ecs.COMPONENT:
            continue        # built in (Elasticsearch 8.13 and later), and what it maps is known here
        if c.get('status') == 'OK' and c.get('body') is not None:
            components.append(f"PUT _component_template/{c['name']}\n{json.dumps(c['body'])}")
        elif c.get('status') == 'NOT_FOUND':
            missing.append(c['name'])
        else:
            unread[c['name']] = (c.get('message') or c.get('status') or '')[:160]
    body = template['body']
    return {'doc': ref['name'], 'index': doc.get('indexName'), 'name': template.get('name'),
            'template': f"PUT _index_template/{template.get('name')}\n{json.dumps(body)}",
            'components': components, 'composed_of': list(body.get('composed_of') or []), 'missing': missing,
            'unread': unread}


async def _example_from_index(ctx: Context, index: str) -> tuple[str, str, dict[str, Any]]:
    """An existing Elastic Index doc's fields as an example index template, a note for the draft, and what was read
    (for the user's confirmation): where Stroom can't read the index template itself (_template_from_index). The
    doc's own field list holds Elasticsearch's types (nativeType: keyword, ip, date...); Stroom's findFields, the
    fallback, only its own (KEYWORD, LONG...). Neither gives the template's settings, component templates, keyword
    sub-fields or ignore_above."""
    stroom = gateway_from(ctx)
    ref, doc = await _index_doc(ctx, index)
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


async def _choose(ctx: Context, question: str, options: list[str], action: str = 'get_field_conventions') -> Any:
    """The user's pick in a form, None where the client has no form (the agent asks instead), or on a modern
    connection the form itself, for the tool to return."""
    store = (getattr(ctx, 'lifespan_context', None) or {}).get('consent')
    return await store.choose(ctx, action, question, options) if store else None


_NAMING = 'field_naming_chosen'
_NAMING_TTL = 4 * 3600


def _remember_naming(ctx: Context, **choice: Any) -> None:
    """How the user chose, in get_field_conventions' form, to name the index's fields: so draft_index_mapping doesn't
    ask them again. Seen in VS Code: the user picked ECS in the form, the agent drafted with convention=ecs but no
    without_example, and the user was asked the same question again (and with it, would have confirmed it again)."""
    from tools.plan import _user
    try:
        ctx.lifespan_context.setdefault(_NAMING, {})[_user(ctx)] = {**choice, 'at': time.time()}
    except Exception:
        pass


def _naming_chosen(ctx: Context) -> dict[str, Any]:
    """The user's own choice in the form, this session (only a form's answer: one the agent relays isn't theirs)."""
    from tools.plan import _user
    try:
        chosen = ctx.lifespan_context.get(_NAMING, {}).get(_user(ctx)) or {}
    except Exception:
        return {}
    return chosen if chosen and time.time() - chosen['at'] < _NAMING_TTL else {}


async def get_field_conventions(
        ctx: Context,
        name: Annotated[str | None, Field(description="Convention profile to use; omit to list them.")] = None,
        backend: Annotated[Backend | None, Field(description="The index's backend: for Elasticsearch, the user's example "
                                                             "comes first (an index template, or an existing index in "
                                                             "Stroom), a convention profile only without one.")] = None,
        ecs_fields: Annotated[str | None, Field(description=(
            "With name=ecs: the ECS fields under a field set or prefix (e.g. 'process', 'user.target'), each with its "
            "type and what it holds, to name a field ECS's way."))] = None,
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
            backends = set()
        if not backend and {'elasticsearch', 'lucene'} <= backends:
            # Asked here, in a form, and the naming after it: seen in Ask Stroom AI, the agent, given the profiles
            # before the backend, asked the user both in the chat, and draft_index_mapping asked the naming again
            # in its own form (the agent's word isn't the user's).
            labels = {'Elasticsearch': 'elasticsearch', 'Lucene (kept inside Stroom, no cluster)': 'lucene'}
            picked = await _choose(ctx, "Which backend should the new index use?", list(labels))
            if picked is not None and not isinstance(picked, str):
                return picked       # the form, on a modern connection: the answer comes with the repeated call
            if picked:
                backend = labels[picked]
            else:
                return {'status': 'needs_guidance', 'question': 'Which backend should the new index use?',
                        'options': list(labels),
                        'hint': "Ask the user only which backend, Elasticsearch or Lucene, and call again with "
                                "backend= their answer. Don't ask how fields are named yet: the choices for that follow "
                                "from the backend (for Elasticsearch, the user's own index template first), and the "
                                "server asks them."}
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
                    "uuid>, with nothing to paste. Stroom reads the index template its indices take and its component "
                    "templates where the cluster's credentials allow, else its field names and Elasticsearch types "
                    "only (then the template's settings and components are pasted, the first choice, if wanted). The "
                    "user confirms what was read in a form.",
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
                    _remember_naming(ctx, like_index=pick['uuid'])
                    result.update(like_index=pick['uuid'], hint=f"The user chose to follow '{pick['name']}': "
                                  f"draft_index_mapping like_index={pick['uuid']} (with the events streams); not "
                                  f"asked again.")
            elif option['option'] == 'example index template':
                result['hint'] = ("Ask the user to paste the index template (and any component templates) into the "
                                  "chat, and end your turn: a form can't carry it. Then draft_index_mapping "
                                  "example_template= it, exactly as given.")
            else:
                profile_name = option['option'].removeprefix('convention profile ')
                _remember_naming(ctx, convention=profile_name)
                result['hint'] = (f"draft_index_mapping convention={profile_name} (with the events streams): the user "
                                  f"chose it in the form, so it isn't asked again.")
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
            ref = value.get('docRef') or {}
            if ref.get('name') == doc_name and ref.get('type') in ('Index', 'ElasticIndex'):
                try:
                    fields = await stroom.post('/dataSource/v1/findFields', {
                        'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 500}})
                except ToolError as e:   # a reference index Stroom can't list: said, the profile still given
                    reference[f"{ref['type']} {doc_name}"] = {'(unreadable)': refused_reason(e)}
                    continue
                reference[f"{ref['type']} {doc_name}"] = {f.get('fldName'): f.get('fldType')
                                                          for f in fields.get('values') or [] if f.get('fldName')}
    out = {'name': name, 'profile': profile, 'reference_fields': reference}
    if name == 'ecs':
        # The full schema, as Elastic publishes it: plans following ECS are checked against it.
        from utils import ecs
        out['ecs'] = {'version': ecs.version(), 'field_sets': ecs.schema()['field_sets'],
                      'note': "Names in these field sets must be ECS fields (draft_index_mapping and save_xslt check "
                              "them); a field of the user's own goes outside them. ecs_fields='<set>' lists a set."}
        if ecs_fields:
            out['ecs_fields'] = ecs.fields_in(ecs_fields)
    return out


_TIME = 'discovery_time_chosen'


def _time_chosen(ctx: Context, index_name: str) -> str | None:
    """The source field the user picked in the form as a discovery index's @timestamp, this session."""
    from tools.plan import _user
    try:
        chosen = ctx.lifespan_context.get(_TIME, {}).get((_user(ctx), index_name))
    except Exception:
        return None
    return chosen['field'] if chosen and time.time() - chosen['at'] < _NAMING_TTL else None


def _records_rows(text: str) -> str | None:
    """records:2 records as JSON lines, one object of data name -> value per record; None for any other text."""
    if 'records:2' not in text[:2000]:
        return None
    from lxml import etree
    rows = []
    for chunk in re.split(r'(?=<\?xml)', text):
        if not chunk.strip():
            continue
        try:
            root = etree.fromstring(chunk.strip().encode('utf-8'), etree.XMLParser(recover=True))
        except etree.XMLSyntaxError:
            continue
        if root is None:
            continue
        for record in root.iter('{records:2}record'):
            rows.append({d.get('name'): d.get('value') for d in record.iter('{records:2}data') if d.get('name')})
    return '\n'.join(json.dumps(r) for r in rows if r) + '\n' if rows else None


async def _discovery_time(ctx: Context, index_name: str, discovery: Discovery,
                          stream_ids: list[int]) -> Discovery | dict[str, Any]:
    """The discovery index's time field, confirmed by the user when the records hold more than one field of times
    (asked for by the user: it becomes @timestamp, and with it whether the index is a data stream), or when the one
    proposed holds no times. The answer is kept for the session, so the user isn't asked again."""
    if not stream_ids:
        return discovery
    from tools.sampling import read_head
    from utils.profile import profile
    stroom = gateway_from(ctx)
    text = ''
    for stream_id in stream_ids[:3]:
        try:
            head, _, _ = await read_head(stroom, int(stream_id), 0, 100_000)
        except (ToolError, ValueError):
            continue
        text += head.rstrip('\n') + '\n'
    rows = _records_rows(text)
    if rows is not None:
        # records:2 (a Records stream): each data element is a field by its name, as the delimited input reads them
        # (seen: a Records stream profiled as XML offered only 'data@value', and the agent indexed name/value lists).
        if discovery.input != 'delimited':
            raise ToolError("These streams hold records:2 records (a Data Splitter's output, a Records stream): draft "
                            "with discovery={'input': 'delimited', 'timestamp_field': <a data name>}, which indexes "
                            "each data element as a field by its name, on an indexing template with an XMLParser.")
        text = rows
    remembered = _time_chosen(ctx, index_name)
    if remembered:
        return discovery.model_copy(update={'timestamp_field': remembered})
    if discovery.timestamp_chosen_by_user:
        return discovery
    try:
        fields = profile(text).get('fields') or [] if text.strip() else []
    except Exception:
        return discovery
    candidates = [f.get('field') for f in fields if str(f.get('type') or '').startswith('timestamp') and f.get('field')]
    proposed = discovery.timestamp_field
    if not candidates or (candidates == [proposed]):
        return discovery
    ordered = ([proposed] if proposed in candidates else []) + [c for c in candidates if c != proposed]
    labels = {(f"{c} (proposed)" if c == proposed else c): c for c in ordered}
    chosen = await _choose(ctx, "Which field holds each record's time? It is indexed as @timestamp.", list(labels),
                           action='draft_index_mapping')
    if chosen is not None and not isinstance(chosen, str):
        return chosen       # the form, on a modern connection: the answer comes with the repeated call
    if chosen in labels:
        field = labels[chosen]
        from tools.plan import _user
        try:
            ctx.lifespan_context.setdefault(_TIME, {})[(_user(ctx), index_name)] = {'field': field, 'at': time.time()}
        except Exception:
            pass
        return discovery.model_copy(update={'timestamp_field': field})
    return {'status': 'needs_guidance', 'timestamp_candidates': ordered,
            'hint': (f"The records hold times in {len(ordered)} fields ({', '.join(ordered)})"
                     + ("" if proposed in candidates else f", and '{proposed}' isn't one of them")
                     + ": ask the user which one is each record's time (it becomes @timestamp), in that order, "
                       "recommending none. Then draft_index_mapping again with discovery.timestamp_field= their "
                       "choice and discovery.timestamp_chosen_by_user=true.")}


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
                                                                  "event-logging paths are actually populated. For a "
                                                                  "discovery index: its raw streams, to find the fields "
                                                                  "holding times.")] = [],
        extra_fields: Annotated[list[PlannedField] | str, ONE_OR_MORE, Field(
            description="Fields the user asked for beyond the convention's map (ECS: one of the source's own under its "
                        "custom namespace). To add one to a plan already drafted: changes=, not a new draft.")] = [],
        drop_when: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="XPath tests on an Event for events the user wants kept out of the index, e.g. "
                        "\"EventDetail/TypeId = 'Heartbeat'\"; any that holds drops the event.")] = [],
        like_index: Annotated[str | None, Field(
            description="Elasticsearch: an existing Elastic Index doc (uuid or exact name) to follow, when the user "
                        "has no index template to paste. Stroom reads the index template its indices take and its "
                        "component templates (else its field names and types), and they serve as the example.")] = None,
        example_template: Annotated[str | None, Field(
            description="Elasticsearch: the user's example index template or index mapping (as for "
                        "propose_index_template); field names then follow it.")] = None,
        component_templates: Annotated[list[str | dict[str, Any]] | str | dict[str, Any], ONE_OR_MORE, Field(
            description="Only if the example lists any in composed_of: those component templates, as the user gave "
                        "them. Kept for the session once given: leave out those "
                        "given before, never send them again.")] = [],
        without_components: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Only when the user can't give some of the example's component templates: their names. The "
                        "template is then built without their fields, which it may repeat or contradict.")] = [],
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
        replaces: Annotated[str | None, Field(description=(
            "When this index replaces an existing one (e.g. its fields renamed to ECS): the old indexing XSLT's uuid, "
            "or its pipeline's. The reply lists the fields the old index wrote that this draft doesn't (replaced: "
            "with their sources and how often the sample populates them), to ask the user about; no need to read "
            "the old XSLT."))] = None,
        custom_namespaces: Annotated[list[str] | str, ONE_OR_MORE, Field(description=(
            "ECS: the namespace(s) for the source's own fields, those ECS has no field for, named after the source or "
            "organisation, e.g. 'secretserver'. Fields of an event's action element (Authenticate/User/Id) are drafted "
            "under the first; defaults to one named after the index. Other top-level names that aren't ECS's are "
            "reported (ecs_check)."))] = [],
        changes: Annotated[dict[str, Any] | str | None, Field(description=(
            "Instead of drafting again, to amend the plan already drafted for index_name in this session (or, with "
            "pipeline_uuid, the one kept with that pipeline's indexing XSLT): only what changes. fields: each replaces "
            "the field of that name whole, or is added; {\"name\": ..., \"remove\": true} removes one; any other key "
            "(drop_when, custom_namespaces...) replaces it. events_stream_ids, if given, re-checks unmapped paths."
            ))] = None,
        pipeline_uuid: Annotated[str | None, Field(description=(
            "With changes: the indexing pipeline whose kept plan to amend, when it wasn't drafted in this "
            "session."))] = None,
) -> dict[str, Any]:
    """
    Draft the index for the build: a field plan (name, type and source path per field) from the chosen
    convention, limited to paths the sample events actually populate, plus StreamId and EventId. With the
    user's example Elasticsearch index template, fields take the example's names for the same data (User.Id,
    TypeId...), others are named in its style, and sample paths the example maps are added. Returns the plan
    rendered for the backend (Lucene field list or Elasticsearch index template) and a draft indexing XSLT in
    the output form that backend's indexing filter reads, leaving out events drop_when names. With discovery,
    a discovery index instead: nothing is read; raw JSON records are copied as they are and Elasticsearch maps
    their fields dynamically. Nothing is saved. The plan is kept for the session: amend it with changes= (only
    what changes), and save_xslt, create_index_doc and propose_index_template take it from there when not given.
    """
    if changes is not None:
        return await _amend_plan(ctx, index_name, changes, events_stream_ids, pipeline_uuid)
    if discovery:
        timed = await _discovery_time(ctx, index_name, discovery, events_stream_ids)
        if not isinstance(timed, Discovery):
            return timed
        return _draft_discovery(backend, index_name, timed)
    if events_stream_ids:
        # First, whatever else is asked: the streams must be Events (a Records stream, or a source's own data, is
        # refused with how to index it).
        await require_events(gateway_from(ctx), events_stream_ids)
    chosen = _naming_chosen(ctx)
    if backend == 'elasticsearch' and not (example_template or like_index) and convention \
            and chosen.get('convention') == convention:
        pass        # the user chose this convention in get_field_conventions' form: asked once, not again
    elif backend == 'elasticsearch' and not (example_template or like_index):
        # Names, types and structure come from what the environment already indexes: the agent asks the user,
        # and indexing from a convention alone is the user's call, made in a form, not the agent's.
        picked: dict[str, Any] = {}
        if not without_example:
            options = await get_field_conventions(ctx, backend='elasticsearch')
            # Answered in the form just now: drafted as the user chose (seen in VS Code: the user picked ECS here
            # too, and was told to be asked again, a third time, in the chat).
            picked = _naming_chosen(ctx) if isinstance(options, dict) and options.get('status') == 'chosen' else {}
            chosen = picked or chosen
            if picked.get('like_index'):
                like_index = picked['like_index']
            elif picked.get('convention'):
                convention = picked['convention']
            elif not isinstance(options, dict):
                return options      # the form, on a modern connection: the answer comes with the repeated call
            else:
                return {**options, 'drafted': False,
                        'hint': "Not drafted: ask the user for their example first, offering these options. Then "
                                "draft_index_mapping with example_template=... (pasted) or like_index=<uuid>; only if "
                                "they have neither, with convention=... and without_example=true, which they confirm."}
        gate = None if picked else await consent_from(ctx).require(
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
    followed: dict[str, Any] | None = None
    if like_index and not example_template:
        # Following another source's index is the user's choice, as the example is. Its index template and component
        # templates, read by Stroom where it can (asked for by the user: pasting them was cumbersome); else its fields,
        # read through Stroom. Either way the user confirms what they'd get first.
        fetched = await _template_from_index(ctx, like_index)
        if fetched and fetched.get('template'):
            followed = fetched
            example_template = fetched['template']
            given = [component_templates] if isinstance(component_templates, str) else list(component_templates)
            component_templates = [*fetched['components'], *given]
            without_components = [*([without_components] if isinstance(without_components, str)
                                    else list(without_components)), *fetched['missing']]
            parts = [n for n in fetched['composed_of'] if n not in fetched['missing']]
            details = {'follow': f"Elastic Index doc '{fetched['doc']}'"
                                 + (f" (index {fetched['index']})" if fetched['index'] else ''),
                       'read through Stroom': f"its index template {fetched['name']}"
                                              + (f", composed of {', '.join(parts)}" if parts else '')}
            if fetched['unread']:
                details['not readable through Stroom'] = (f"component templates {', '.join(sorted(fetched['unread']))}: "
                                                          f"you'll be asked to paste them")
            like_note = (f"followed Elastic Index doc '{fetched['doc']}': its index template {fetched['name']}"
                         + (f" and component templates {sorted(n for n in parts if n not in fetched['unread'])}" if parts
                            else '') + ", read through Stroom")
            read = {'doc': fetched['doc']}
        else:
            example_template, like_note, read = await _example_from_index(ctx, like_index)
            details = {'follow': f"Elastic Index doc '{read['doc']}'" + (f" (index {read['index']})" if read['index'] else ''),
                       'read through Stroom': f"{read['fields']} fields and their types, e.g. {', '.join(read['examples'][:3])}",
                       'not readable through Stroom': "its settings, component templates, sub-fields and ignore_above: "
                                                      "Elasticsearch defaults, unless you paste its index template"}
            if fetched and fetched.get('why'):
                details['its index template'] = fetched['why']
                like_note += f". {fetched['why']}"
        # Picked in get_field_conventions' form: the user isn't asked again (what can't be read through Stroom is
        # said in the result, for the agent to tell them).
        gate = None if chosen.get('like_index') == like_index else await consent_from(ctx).require(
            ctx, 'confirmation', 'draft_index_mapping',
            f"Name the fields of index '{index_name}' after an existing index in Stroom", details, confirmation_id)
        if gate:
            return gate
        if followed:
            # Agreed: not asked again when the draft is called again with a component Stroom couldn't read.
            _remember_naming(ctx, like_index=like_index)
            chosen = _naming_chosen(ctx)
        if chosen.get('like_index') == like_index and not followed:
            like_note = ((like_note + ' ') if like_note else '') + (
                "Not readable through Stroom, so tell the user: its index template itself (settings, component "
                "templates, keyword sub-fields, ignore_above), Elasticsearch defaults for those unless they paste it.")
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
    unused, mapped = [], []
    for path, spec in (profile.get('field_map') or {}).items():
        if example_template and path.startswith('EventDetail/*/'):
            # Any action's Action or Outcome as one field (ECS's event.action): ECS's way, not the example's, which
            # names each action's own (Authenticate.Action).
            continue
        # A name is planned once, from the first of its paths the sample populates (the client's address, else the
        # source address of whichever Network action the event records).
        if population_of(path, populated) and not any(f.name == spec['name'] for f in fields):
            fields.append(PlannedField(name=spec['name'], type=spec['type'], source=path,
                                       transform=spec.get('transform')))
            mapped.append(path)
        elif population_of(path, populated):
            mapped.append(path)
        else:
            unused.append(path)
    # What happened, from the event's action element (Alert's Type and Severity, Authenticate's Action and Outcome,
    # Process, Update), planned by default: a user asked for these each time. Network's are the profile's.
    from utils.templatecheck import _derive, field_group, nested_name
    nested = profile.get('structure') == 'nested'
    namespaces = [custom_namespaces] if isinstance(custom_namespaces, str) else list(custom_namespaces)
    own = None
    if convention == 'ecs' and not example_template:
        from utils import ecs
        clash = [n for n in namespaces if n in ecs.top_level()]
        if clash:
            raise ToolError(f"custom_namespaces {clash}: ECS's own top-level names, so not a namespace of the source's: "
                            f"name it after the source or organisation (e.g. {ecs.source_namespace(index_name)})")
        # ECS has no place for an action's own elements (Authenticate/User/Id): they go under the source's namespace,
        # as ECS asks, not a top-level group per action (seen: authenticate.*, authorise.*, copy.* in an ECS plan).
        namespaces = namespaces or [ecs.source_namespace(index_name)]
        own = namespaces[0]
    added = 0
    for path in sorted(populated):
        if (added >= 20 or not populated[path] or not path.startswith('EventDetail/') or 'Data' in path.split('/')
                or path.startswith('EventDetail/Network/') or not field_group(path)
                or any(source_matches(f.source, path) for f in fields)
                or any(source_matches(m, path) for m in mapped)):
            continue
        taken = {f.name for f in fields}
        name = nested_name(path, 'ecs') if nested else _derive(path, 'pascal', False, taken)
        name = f'{own}.{name}' if name and own else name
        if not name or name in taken:
            continue
        type_ = 'boolean' if path.endswith('/Success') else 'long' if path.endswith('/Port') else 'keyword'
        fields.append(PlannedField(name=name, source=path, type=type_))
        added += 1
    # ECS: a Data element whose name is an ECS field's, written another way (source_ip, SourceIp as source.ip), under
    # that name and ECS's type. A draft for the user to review: abbreviations (src_ip) are not guessed.
    ecs_data = []
    if convention == 'ecs':
        from utils.ecs import for_data_name, plan_type
        for path, names in (events.get('data_names') or {}).items():
            for data_name in names:
                ecs_name = for_data_name(data_name)
                if ecs_name and "'" not in data_name and not any(f.name == ecs_name for f in fields):
                    fields.append(PlannedField(name=ecs_name, type=plan_type(ecs_name),
                                               source=f"{path}[@Name='{data_name}']/@Value"))
                    ecs_data.append(f"Data '{data_name}' ({path}) as ECS's {ecs_name}")
    example_notes, subobjects = [], True
    if example_template:
        try:
            _, example = parse_template(example_template)
            components = _given_components(ctx, component_templates, example.get('composed_of') or [],
                                           example_template)
        except ValueError as e:
            raise ToolError(str(e)) from e
        wanted = components_wanted(example, components, without_components)
        if wanted:
            if followed and followed['unread']:
                wanted['hint'] = (f"Stroom read the index template of '{followed['doc']}' but not the component templates "
                                  f"{sorted(followed['unread'])} (Elasticsearch: {next(iter(followed['unread'].values()))}). "
                                  + wanted['hint'])
            return wanted
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
        try:
            values = json_values((await stroom.get_doc('XSLT', found[0]['uuid'])).get('data') or '', use.template) \
                if found else {}
        except ToolError:   # listed, but gone or unreadable: as if not found
            found, values = [], {}
        values = {k: v for k, v in values.items() if k == use.at or k.startswith(use.at + '.')} or {use.at: 'string'}
        for name, element in values.items():
            if not any(f.name == name for f in fields):
                fields.append(PlannedField(name=name, type={'number': 'long', 'boolean': 'boolean'}.get(element, 'keyword'),
                                           source=f'shared:{use.template}'))
        example_notes.append(f"{sorted(values)}: written by the shared template {use.template} ({use.href})"
                             + ('' if found else '; not found as an XSLT document, so typed as a keyword'))
    if convention == 'ecs' and example_template:
        # The user's example names the fields: its top-level names are theirs, declared, not reported as un-ECS.
        from utils import ecs
        namespaces += sorted({f.name.split('.')[0] for f in fields} - ecs.top_level() - set(namespaces) - {'StreamId', 'EventId'})
    plan = FieldPlan(backend=backend, index_name=index_name, time_field=time_field, fields=fields, drop_when=drop_when,
                     subobjects=subobjects, shared=list(shared), convention=convention,
                     custom_namespaces=namespaces if convention == 'ecs' else [])
    if (pasted or like_index) and backend == 'elasticsearch':
        # Followed from an existing index too: seen in testing, the user chose to follow one (nothing to paste), and
        # propose_index_template, given no example, asked them to paste one.
        build = await _build_of_stream(ctx, events_stream_ids[0])
        if build:
            try:
                await _keep_example(ctx, build, index_name, pasted or example_template,
                                    [] if not (pasted or followed) else (
                    [component_templates] if isinstance(component_templates, str) else list(component_templates)),
                    PASTED if pasted else
                    f"read through Stroom from Elastic Index doc '{read['doc']}', which the user chose to follow: its "
                    f"index template and component templates" if followed else
                    f"read through Stroom from Elastic Index doc '{read['doc']}', which the user chose to follow: "
                    f"{FIELDS_ONLY}")
                example_notes.append(f"the example is kept in build {build} ('{index_name}{EXAMPLE_SUFFIX}'): "
                                     f"propose_index_template follows it even if it isn't given again")
            except Exception as e:   # keeping it is a convenience; the plan stands without it
                logger.warning("Couldn't keep the example index template in build %s: %s", build, e)
    plan = await with_cardinality(ctx, plan)
    replaced = await _replaced(ctx, replaces, plan, populated) if replaces else None
    reply = _plan_reply(ctx, plan, populated)
    # No XSLT here: save_xslt index_plan= generates it, and half the reply was code the agent never reads.
    return {**({'replaced': replaced} if replaced else {}),
            **{k: reply.pop(k) for k in ('plan', 'problems', 'rendered')}, 'convention_paths_not_in_sample': unused,
            **reply,
            **({'from_example': example_notes} if example_template or shared else {}),
            **({'ecs_data_elements': ecs_data} if ecs_data else {})}


async def with_cardinality(ctx: Context, plan: FieldPlan, build: str | None = None) -> FieldPlan:
    """The plan with each field whose source can hold several values in one event (a repeatable element on its path,
    by the event-logging schema: Data, say) marked multiple, so the indexing XSLT writes them all. Seen (asked about by
    the user): written with value-of, two ports were indexed as the one number "80 443", two tags as "a b". Unmarked
    when the schema can't be read: the fields are then written as single values, as before."""
    if plan.discovery is not None:
        return plan
    try:
        from tools.generation import chosen_or_latest, event_schema
        schema = await event_schema(ctx, await chosen_or_latest(ctx, build))
    except Exception as e:      # a Stroom without the schema: nothing marked
        logger.info("Fields not checked for several values: %s", e)
        return plan
    fields = [f.model_copy(update={'multiple': True}) if not f.multiple and not f.source.startswith(('@', 'shared:'))
              and schema.can_repeat(f.source) else f for f in plan.fields]
    return plan.model_copy(update={'fields': fields})


_PLANS = 'index_plans_drafted'
PLAN_HINT = ("Review unmapped paths with the user; add any they want with changes={\"fields\": [...]} (only what "
             "changes: the plan is kept for the session). Save the XSLT with save_xslt and no code: it is generated "
             "from the plan kept for the session (or index_plan=), so write_documentation generates the Field mapping "
             "section.")


def _plan_reply(ctx: Context, plan: FieldPlan, populated: dict[str, float] | None) -> dict[str, Any]:
    """What a drafted or amended plan comes back as; the plan kept for the session, for changes= and the tools that
    take a plan. Seen in Ask Stroom AI: after the client's step cap the agent had lost its earlier extra_fields and
    spent eight calls recovering them, and pasted the 50-field plan inline five times over."""
    from tools.plan import _user
    try:
        ctx.lifespan_context.setdefault(_PLANS, {})[(_user(ctx), plan.index_name)] = plan.model_dump()
    except Exception:   # kept for convenience; the plan is in the reply all the same
        pass
    reply: dict[str, Any] = {'plan': plan.model_dump(), 'problems': plan.required(),
                             'rendered': plan.lucene_fields() if plan.backend == 'lucene' else
                             plan.elastic_template(plan.index_name)}
    if populated is not None:
        # Network paths once, whichever action: one field for a source address, not one per Permit and Deny.
        unmapped = sorted(dict.fromkeys(any_action(p) for p in populated
                                        if populated[p] and not any(source_matches(f.source, p) for f in plan.fields)))
        reply.update({'populated_paths_not_mapped': unmapped[:40],
                      'field_mapping': index_field_mapping_markdown(plan, populated)})
    if plan.convention_problems():
        reply['ecs_check'] = plan.convention_problems()
    return {**reply, 'hint': PLAN_HINT}


def drafted_plans(ctx: Context) -> dict[str, FieldPlan]:
    """The plans draft_index_mapping drafted or amended in this session, by index name."""
    from tools.plan import _user
    try:
        user = _user(ctx)
        return {index: FieldPlan.model_validate(plan) for (who, index), plan in
                (ctx.lifespan_context.get(_PLANS) or {}).items() if who == user}
    except Exception:
        return {}


async def plan_given(ctx: Context, plan: FieldPlan | None, index_names: list[str | None] = (),
                     pipeline_uuid: str | None = None) -> FieldPlan | None:
    """The plan a tool was given, else the one kept with the pipeline's indexing XSLT (what it writes), else the one
    drafted in this session for one of the index names (or the only one drafted, with no name to go by)."""
    if plan is not None:
        return plan
    if pipeline_uuid:
        from tools.builds import kept_mapping
        try:
            kept = await kept_mapping(ctx, pipeline_uuid)
        except ToolError:
            kept = None
        if kept and kept['kind'] == 'index':
            return FieldPlan.model_validate(kept['payload'])
    drafted = drafted_plans(ctx)
    named = [drafted[n] for n in index_names if n in drafted]
    if named:
        return named[0]
    return next(iter(drafted.values())) if len(drafted) == 1 and not any(index_names) else None


async def _amend_plan(ctx: Context, index_name: str, changes: dict[str, Any] | str, events_stream_ids: Any,
                      pipeline_uuid: str | None) -> dict[str, Any]:
    """draft_index_mapping changes=: the kept plan with only what changes merged in."""
    from utils.mappingchanges import apply_plan_changes
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except json.JSONDecodeError as e:
            raise ToolError(f"changes is text that is not JSON ({e.msg} at character {e.pos}): pass it as an object")
    base = drafted_plans(ctx).get(index_name)
    if base is None and pipeline_uuid:
        base = await plan_given(ctx, None, pipeline_uuid=pipeline_uuid)
    if base is None:
        raise ToolError(f"No plan for '{index_name}' to amend: none was drafted for it in this session"
                        + ("" if pipeline_uuid else " (give pipeline_uuid= the indexing pipeline whose saved XSLT keeps "
                                                    "one)")
                        + f". Drafted in this session: {sorted(drafted_plans(ctx)) or 'none'}. Else draft it whole.")
    merged, done = apply_plan_changes(base.model_dump(), changes)
    try:
        plan = FieldPlan.model_validate(merged)
    except ValueError as e:
        raise ToolError(f"The amended plan isn't valid: {e}") from e
    populated = None
    if events_stream_ids:
        await require_events(gateway_from(ctx), events_stream_ids)
        populated = (await summarise_events(ctx, events_stream_ids, 200))['path_population']
    return {'changed': done, **_plan_reply(ctx, await with_cardinality(ctx, plan), populated)}


async def _replaced(ctx: Context, replaces: str, plan: FieldPlan, populated: dict[str, float]) -> dict[str, Any]:
    """What the index being replaced wrote that the draft doesn't. Seen in VS Code: asked to make an indexing pipeline
    write ECS, the agent compared its draft with the old XSLT by reading describe_document's output from a file its
    client spilled it to, and its model fell into a loop."""
    from lxml import etree
    from tools.pipelines import translation_docs
    from tools.validation import index_fields
    stroom = gateway_from(ctx)
    try:
        doc = await stroom.get_doc('XSLT', replaces)
    except Exception:       # a pipeline's uuid: its indexing XSLT
        doc = None
    if not doc or not doc.get('data'):
        try:
            entries = [e for e in translation_docs(replaces, await stroom.pipeline_layers(replaces))
                       if e['doc']['type'] == 'XSLT']
        except Exception as e:
            raise ToolError(f"replaces={replaces}: neither an XSLT nor a pipeline this server can read ({e})") from e
        if not entries:
            raise ToolError(f"replaces={replaces}: the pipeline runs no XSLT")
        doc = await stroom.get_doc('XSLT', entries[-1]['doc']['uuid'])
    kept = read_mapping(doc.get('description'))
    if kept and kept[0] == 'index':
        old = [{'field': f['name'], 'source': f['source']} for f in kept[1].get('fields') or []]
    else:
        try:
            root = etree.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', doc.get('data') or '').encode('utf-8'))
            old = index_fields(root) or []
        except etree.XMLSyntaxError:
            old = []
    if not old:
        return {'xslt': doc.get('name'), 'note': "No fields read from it: not an indexing XSLT this server can read."}

    def plain(source: str) -> str:
        return source.split(' ! ')[0].strip()

    def covered(source: str) -> bool:
        return any(f.source == source or source_matches(f.source, source) or source_matches(source, f.source)
                   for f in plan.fields)
    missing = []
    for f in old:
        source = plain(f.get('source') or '')
        if not source or covered(source):
            continue
        if re.fullmatch(r"[\w@*./:\[\]='\"-]+", source):
            share = population_of(source, populated)
            said = f"{share:.0f}%" if share else 'not in the sample'
        else:
            said = 'unknown (an expression)'
        missing.append({'field': f['field'], 'source': f['source'], 'populated': said})
    return {'xslt': doc.get('name'), 'fields': len(old), 'not_in_draft': missing,
            **({'hint': "Ask the user about each field the old index wrote that this draft doesn't: add it (under "
                        "which name: an ECS field, get_field_conventions name=ecs ecs_fields=<set>) with "
                        "extra_fields and draft again, or leave it out. Those not in the sample hold nothing to "
                        "index from it."} if missing else {})}


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
    unread = []
    for value in indexes:
        # Every Elastic Index doc on the instance: one Stroom can't give (deleted since the search listed it, not this
        # user's to read) is said, the others still listed.
        try:
            doc = await stroom.get_doc('ElasticIndex', value['docRef']['uuid'])
        except ToolError as e:
            unread.append(f"Elastic Index '{value['docRef'].get('name')}': {refused_reason(e)}")
            continue
        cluster = (doc.get('clusterRef') or {}).get('uuid')
        by_cluster.setdefault(cluster, []).append({'name': doc.get('name'), 'uuid': doc.get('uuid'),
                                                   'index_name': doc.get('indexName'), 'time_field': doc.get('timeField'),
                                                   'path': (value.get('path') or '').replace(' / ', '/')})
    out = []
    for ref in clusters:
        try:
            raw = await stroom.get_doc('ElasticCluster', ref['uuid'])
        except ToolError as e:
            unread.append(f"Elastic Cluster '{ref.get('name')}': {refused_reason(e)}")
            continue
        doc = _redact(raw)
        entry = {'name': doc.get('name'), 'uuid': doc.get('uuid'),
                 'urls': (doc.get('connection') or {}).get('connectionUrls'),
                 'index_docs': by_cluster.get(ref['uuid'], [])}
        if test:
            # Each cluster's own result: one that can't be reached is that cluster's answer, not the whole call's.
            try:
                entry['test'] = await stroom.post('/elasticCluster/v1/testCluster', raw)
            except ToolError as e:
                entry['test'] = {'ok': False, 'message': refused_reason(e)}
        out.append(entry)
    return {'clusters': out, **({'unreadable': unread} if unread else {})}


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
                                                           "analyzer). Elasticsearch fields come from the index template. "
                                                           "Omitted: the one drafted for this index in the session.")] = None,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Create the build's index doc: a Lucene Index in a volume group with the plan's fields, or an Elastic
    Index doc on an existing Elastic Cluster doc pointing at the index or data stream, tested against the
    cluster. The user confirms the backend, name and target.
    """
    stroom = gateway_from(ctx)
    if plan is None:
        # The plan drafted for this index in the session: not pasted again (seen: a 50-field plan, five times).
        plan = await plan_given(ctx, None, [index_name, name])
        plan = plan if plan is not None and plan.backend == backend else None
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
    if backend == 'elasticsearch':
        # The reply says what was made: seen in Ask Stroom AI, it kept the index name from before the user's edit,
        # and the agent offered that back to them as what the doc held.
        target['index name'] = index_name
    doc_type = INDEX_TYPE[backend]
    ref = await guard_from(ctx).create(doc_type, name, build, role='Indexing')
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
        if meta.get('typeName') == 'Records':
            raise ToolError(f"Stream {stream_id} is a Records stream: records:2 records, not Events, so an event-"
                            f"logging field plan doesn't fit it. Index records as they are: draft_index_mapping "
                            f"discovery={{'input': 'delimited', 'timestamp_field': ...}} (it reads records:2 data "
                            f"elements by name), on an indexing template with an XMLParser.")
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
                        f"pipeline reads Events streams, not raw data: build the events pipeline first (stage 1: feed, "
                        f"translation XSLT, step, process), then index its Events. To "
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
                                                            "doc, which names the index and its cluster (set on the "
                                                            "pipeline, over the template's own cluster).")] = None,
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
        if index_uuid:
            # The Elastic Index doc names the index and its cluster (seen in VS Code: refused for want of index_name
            # though the doc was given).
            try:
                elastic = await stroom.get_doc('ElasticIndex', index_uuid)
            except ToolError as e:
                raise ToolError(f"index_uuid {index_uuid} is not an Elastic Index doc this template can index into: "
                                f"give index_name (and cluster_uuid)") from e
            index_name = index_name or elastic.get('indexName')
            cluster_uuid = cluster_uuid or (elastic.get('clusterRef') or {}).get('uuid')
        if not index_name:
            raise ToolError("This template indexes into Elasticsearch: give index_name, or index_uuid (the Elastic "
                            "Index doc, which names it)")
        element = next((e for e, p in open_props if p == 'indexName'), 'elasticIndexingFilter')
        props.append(PropertyValue(element=element, name='indexName', value=index_name))
        inherited = next(((s.get('value') or {}) for s in shape.get('shared') or []
                          if s.get('element') == element and s.get('property') == 'cluster'), {})
        if (element, 'cluster') in open_props:
            if not cluster_uuid:
                raise ToolError("The template leaves the cluster open: give cluster_uuid")
            props.append(PropertyValue(element=element, name='cluster', doc_uuid=cluster_uuid, doc_type='ElasticCluster'))
        elif cluster_uuid and inherited.get('uuid') and inherited['uuid'] != cluster_uuid:
            # Seen in Ask Stroom AI: the index doc on ES_DEV, the user's choice, and the pipeline left on the template's
            # ES_PROD, so its index template was drafted for production.
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
    ref = await guard_from(ctx).create('Dashboard', name, build, role='Indexing')
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
                 **({'stored': False} if f.get('stored') is False else {}),
                 **({'searchable': False} if f.get('indexed') is False else {})}
                for f in found.get('values') or [] if f.get('fldName')]
    found = await stroom.post('/dataSource/v1/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 2000}})
    return [{'name': f['fldName'], 'type': (f.get('fldType') or '').lower(),
             **({'searchable': False} if f.get('queryable') is False else {})}
            for f in found.get('values') or [] if f.get('fldName')]


async def _unsearchable(ctx: Context, doc_type: str, ref: dict[str, Any], names: list[str],
                        pipeline_uuid: str | None) -> dict[str, str]:
    """Of these fields, those no search finds, with why: a Lucene field not indexed, one Stroom lists as not
    queryable, or one the template agreed with the pipeline doesn't index (index: false, or under an object with
    enabled: false). Asked about by the user: a check on such a field finds nothing, as if documents were missing."""
    stroom, out = gateway_from(ctx), {}
    try:
        for f in await _index_fields(stroom, doc_type, ref):
            if f['name'] in names and f.get('searchable') is False:
                out[f['name']] = ("the index doc's field isn't indexed" if doc_type == 'Index'
                                  else "Stroom lists it as not searchable")
    except Exception:    # a check on the searches, not a reason for verification to fail
        pass
    if pipeline_uuid and doc_type == 'ElasticIndex':
        from utils.mappingstore import read_agreed_template
        from utils.templatecheck import unsearchable
        try:
            agreed = read_agreed_template((await stroom.get_doc('Pipeline', pipeline_uuid)).get('description')) or {}
            mappings = ((parse_template(agreed['dev_tools'])[1].get('template') or {}).get('mappings') or {}
                        if agreed.get('dev_tools') else {})
        except Exception:
            mappings = {}
        for name in unsearchable(mappings, [n for n in names if n not in out]):
            out[name] = "the index template agreed for it doesn't index it (index: false, or an object with enabled: false)"
    return out


_FIELD_WAITS = (5, 5)   # seconds between looks at the index's fields, for a field Stroom hasn't listed yet


async def fields_not_in_index(stroom, doc_type: str, ref: dict[str, Any], expected: dict[str, list[str]]) -> dict[str, list[str]]:
    """{pipeline: fields it writes that Stroom doesn't list for the index}, of those expected ({pipeline: fields}),
    after looking again a few times: a field just indexed can take Stroom a moment to list. Seen in production: the
    documentation of an index came out without a field the pipeline writes, though its data was indexed and
    refreshed, and nothing said so; a minute later Stroom listed it (asked for by the user: an error, not silence)."""
    missing: dict[str, list[str]] = {}
    for attempt in range(len(_FIELD_WAITS) + 1):
        listed = {f['name'] for f in await _index_fields(stroom, doc_type, ref)}
        missing = {p: [f for f in fields if f not in listed] for p, fields in expected.items()}
        missing = {p: fields for p, fields in missing.items() if fields}
        if not missing or attempt == len(_FIELD_WAITS):
            return missing
        await asyncio.sleep(_FIELD_WAITS[attempt])
    return missing


def missing_fields_said(missing: dict[str, list[str]], index_name: str | None) -> str:
    """What the agent tells the user about fields a pipeline writes that the index doesn't have."""
    said = '; '.join(f"{', '.join(fields)} (written by '{p}')" for p, fields in missing.items())
    return (f"The index{f' {index_name}' if index_name else ''} has no field {said}: Stroom doesn't list them for it, "
            f"though the pipeline writes them. Tell the user, and ask them to either reindex (the data was indexed "
            f"before the pipeline wrote these fields, or without them: for Elasticsearch, the index template must map "
            f"them before the data is indexed again), or check again in a minute or two (Stroom can be slow to list a "
            f"field that was only just indexed). Then call again.")


async def _fields_written_not_indexed(ctx: Context, pipeline_uuid: str, ref: dict[str, Any],
                                      stream_ids: list[int]) -> dict[str, list[str]]:
    """The fields an indexing pipeline's plan writes for these Events (their source populated in them: a field no
    event fills is never mapped where the mapping is dynamic) that the index doesn't have."""
    from tools.builds import kept_mapping
    from tools.streams import summarise_events
    from utils.fieldplan import population_of
    stroom = gateway_from(ctx)
    try:
        kept = await kept_mapping(ctx, pipeline_uuid)
        if not kept or kept['kind'] != 'index':
            return {}
        plan = FieldPlan.model_validate(kept['payload'])
        if plan.discovery:
            return {}
        population = (await summarise_events(ctx, stream_ids, 200))['path_population']
        name = (await stroom.get_doc('Pipeline', pipeline_uuid)).get('name') or pipeline_uuid
    except ToolError:
        return {}       # the searches decide; this check needs the plan and the Events
    written = [f.name for f in plan.fields if f.source.startswith('@') or (population_of(f.source, population) or 0) > 0]
    return await fields_not_in_index(stroom, ref['type'], ref, {name: written})


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
        try:
            kept = await kept_mapping(ctx, doc['uuid'])
        except ToolError:   # its plan unreadable: still a pipeline that feeds the index, without one
            kept = None
        fed.append({'uuid': doc['uuid'], 'name': doc.get('name'), 'path': (value.get('path') or '').replace(' / ', '/'),
                    'plan': kept['payload'] if kept and kept['kind'] == 'index' else None})
    return fed


async def index_profile(ctx: Context, ref: dict[str, Any], time_field: str | None, names: list[str],
                        streams: int = 20, label: str | None = None) -> dict[str, Any]:
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
    label = label or ref['name']
    found = await _search(ctx, probe(totals, 1), everything, length=1,
                          doing=f"Counting every document in {label} (this reads the whole index)")
    if not found['rows']:
        return {}
    row = found['rows'][0]
    profile: dict[str, Any] = {'documents': int(row.get('documents') or 0), 'earliest': row.get('earliest'),
                               'latest': row.get('latest'), 'streams': int(row['streams']) if row.get('streams') else None}
    if not found['complete']:
        # Counting reads every document through Stroom: on a large index it can outlast the wait, and what was counted
        # by then is a floor, not the index.
        profile['partial'] = (f"Stroom had not finished counting after {SEARCH_WAIT} s: these are the documents it "
                              f"had read by then, not the whole index")
    if 'StreamId' not in names:
        return profile
    by_stream = await _search(ctx, probe([column('StreamId', '${StreamId}', group=0),
                                          column('documents', 'count()', sort={'order': 0, 'direction': 'DESCENDING'})],
                                         streams), everything, length=streams,
                        doing=f"Grouping the documents in {label} by stream (this reads the whole index)")
    say = await progress_from(ctx)
    await say(f"Looking up where the {min(streams, len(by_stream['rows']))} streams with the most documents came from")
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
    if not by_stream['complete'] and 'partial' not in profile:
        profile['partial'] = (f"Stroom had not finished grouping the documents by stream after {SEARCH_WAIT} s: the "
                              f"sources are from the documents it had read by then")
    return profile


RECENT_DAYS = 30   # the sample is read from documents this recent when the index has any
SAMPLE_WAIT = 10   # seconds the sample's searches wait in all, before the sample is what Stroom has found by then


async def survey_index(ctx: Context, doc_type: str, uuid: str, max_documents: int = 100,
                       profile: bool = True) -> dict[str, Any]:
    """What an existing index holds, surveyed through Stroom: the fields Stroom has for the index doc, a sample of
    its documents read through dashboard searches that are never saved (each field: how often the sample held it, its
    values), and the pipelines that feed it; with profile, the whole index's totals and sources too (index_profile).
    A wide index is read in groups of columns, joined on StreamId and EventId; past SURVEY_FIELDS fields, the rest
    are listed as not surveyed.

    The sample is not sorted: Stroom sorts a table itself, so a sorted search reads every document in the index,
    while an unsorted one stops at max_documents (seen: the newest 100 documents of a large Elasticsearch index,
    sorted on its time field, took searches of a minute each, and describe_document three minutes). It is read from
    the last RECENT_DAYS days when there are documents that recent, so it shows what the index is written with now.

    An unsorted search stops early only once it has its rows, and Stroom drops every hit whose StreamId isn't a
    stream the user may read: an index holding fewer readable documents than asked for is read to its end (seen: 38 s
    for any 100 documents of an Elasticsearch index written mostly from outside Stroom). The sample's searches wait
    SAMPLE_WAIT seconds in all, and the sample is then what they had found, said to be partial."""
    stroom = gateway_from(ctx)
    say = await progress_from(ctx)
    index = await stroom.get_doc(doc_type, uuid)
    ref = {'type': doc_type, 'uuid': uuid, 'name': index.get('name')}
    label = index.get('indexName') or index.get('name')     # what the user knows the index by: its name in Elasticsearch
    await say(f"Reading the fields of {index.get('name')}"
              + (f" ({index['indexName']})" if index.get('indexName') else ''))
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
        cols = [_column(n) for n in columns]
        return {'uuid': str(uuidlib.uuid4()), 'name': f"{index.get('name')} survey", 'dashboardConfig': {'components': [
            {'type': 'query', 'id': 'query-SURVEY', 'name': 'Query', 'settings': {
                'type': 'query', 'dataSource': ref, 'expression': {'type': 'operator', 'op': 'AND', 'children': []}}},
            {'type': 'table', 'id': 'table-SURVEY', 'name': 'Table', 'settings': {
                'type': 'table', 'queryId': 'query-SURVEY', 'fields': cols, 'extractValues': False,
                'maxResults': [max_documents], 'pageSize': max_documents}}]}}

    def between(span: str) -> dict[str, Any]:
        return {'type': 'operator', 'op': 'AND', 'children': [
            {'type': 'term', 'field': time_field, 'condition': 'BETWEEN', 'value': span}]}
    # The last RECENT_DAYS days; else any documents; else every time there is (some backends want a term). A recent
    # window too sparse to fill the sample gives way to any documents when those are more: five documents say little
    # of how often each field is held.
    tries = ([('recent', between(f'day()-{RECENT_DAYS}d,day()+1d'), f'from the last {RECENT_DAYS} days')]
             if time_field in names else []) \
        + [('any', {'type': 'operator', 'op': 'AND', 'children': []}, 'of any time')] \
        + ([('any', between('1970-01-01T00:00:00.000Z,day()+1d'), 'of any time')] if time_field else [])
    first: dict[str, Any] = {'rows': [], 'errors': []}
    sampled_from = None
    deadline = time.monotonic() + SAMPLE_WAIT
    for tried, expression, span in tries:
        left = deadline - time.monotonic()
        if left <= 0 and first['rows']:
            break
        found = await _search(ctx, probe(base + groups[0]), expression, length=max_documents, wait=max(left, 2),
                              doing=f"Searching {label} for up to {max_documents} documents {span}")
        if len(found['rows']) > len(first['rows']) or not first['rows']:
            first, sampled_from = found, tried
        if len(first['rows']) >= max_documents or (tried == 'any' and (first['rows'] or not found.get('complete', True))):
            # Any documents found, or Stroom still looking for some when the wait ran out: a term would find none it
            # hasn't (seen: a dev Stroom whose streams were deleted in a migration, its indices kept).
            break
    errors = list(first['errors'])
    cut_short = not first.get('complete', True)
    rows = [dict(r) for r in first['rows']]
    key = lambda r: tuple(str(r.get(n)) for n in ids)
    by_key = {key(r): r for r in rows}
    if rows and groups[1:]:
        # The other groups read exactly the documents the first did, by their ids: an unsorted search returns
        # whichever documents it reaches first, which need not be the same ones twice. At the same time: each is a
        # short search for up to max_documents documents.
        same = {'type': 'operator', 'op': 'OR', 'children': [
            {'type': 'operator', 'op': 'AND', 'children': [
                {'type': 'term', 'field': n, 'condition': 'EQUALS', 'value': str(r.get(n))} for n in ids]}
            for r in rows]}
        await say(f"Reading {len(groups) - 1} more group{'s' if len(groups) > 2 else ''} of columns for the "
                  f"{len(rows)} documents")
        for group, more in zip(groups[1:], await asyncio.gather(
                *[_search(ctx, probe(base + group), same, length=max_documents) for group in groups[1:]])):
            errors += more['errors']
            for r in more['rows']:
                if key(r) in by_key:
                    by_key[key(r)].update({n: r.get(n) for n in group})
    surveyed = base + [n for g in groups for n in g]
    documents = [{n: [str(r[n])] if r.get(n) not in (None, '') else [] for n in surveyed} for r in rows]
    times = sorted(d[time_field][0] for d in documents if time_field and d.get(time_field))
    total = len(documents)
    totals: dict[str, Any] = {}
    if total and profile:
        try:
            totals = await index_profile(ctx, ref, time_field, names, label=label)
        except ToolError as e:
            totals = {'error': str(e)}
    note = None
    if not total:
        # Stroom checks every hit's stream, Lucene's as Elasticsearch's (ExtractionDecorator, shared by both): a
        # document whose stream is gone (seen: a dev Stroom whose streams were deleted in a migration) or unreadable
        # never comes back.
        note = ("No documents came back through Stroom. Stroom returns a hit only when its StreamId is a stream in "
                "this Stroom that you may read (it checks the stream's feed for every hit), so documents written from "
                "outside Stroom, without StreamId or with another system's, or whose streams have been deleted, are "
                "not shown: the index is described from its mapping only, its values not read."
                if doc_type == 'ElasticIndex' else
                "No documents came back through Stroom: the index is empty, holds no stored values to show, or holds "
                "only documents whose streams you may not read or that have been deleted (Stroom returns a hit only "
                "when its StreamId is a stream in this Stroom that you may read).")
        if cut_short:
            note = (f"Stroom found no document it could show within {SAMPLE_WAIT} s, and was still looking. " + note)
    elif cut_short and total < max_documents:
        note = (f"The sample is {total} documents, what Stroom had found after {SAMPLE_WAIT} s: it was still looking. "
                f"Stroom returns a hit only when its StreamId is a stream you may read, so an index written mostly "
                f"from outside Stroom holds few it can show, and it reads the whole index looking for them.")
    await say(f"Looking for the pipelines that write to {label}")
    return {'index': {**ref, 'path': await _path_of(stroom, ref), 'link': doc_link(stroom.settings, doc_type, uuid)},
            **({'note': note} if note else {}),
            'backend': 'lucene' if doc_type == 'Index' else 'elasticsearch',
            'index_name': index.get('indexName'), 'time_field': time_field, 'fields': fields,
            'surveyed_fields': surveyed, 'documents_sampled': total,
            **({'sampled_from': f'the last {RECENT_DAYS} days' if sampled_from == 'recent' else 'any time'}
               if total else {}),
            **({'profile': totals} if profile else {}),
            'earliest': times[0] if times else None, 'latest': times[-1] if times else None,
            'populated': {n: round(100 * sum(1 for d in documents if d.get(n)) / total, 1) for n in surveyed} if total else {},
            'values': {n: list(dict.fromkeys(v for d in documents for v in d.get(n, [])))[:3] for n in surveyed},
            'fed_by': await feeding_pipelines(ctx, doc_type, uuid, index), 'documents': documents, 'errors': errors}


async def _path_of(stroom, ref: dict[str, Any]) -> str | None:
    found = (await stroom.find_documents(ref['name'], [ref['type']], 20)).get('values') or []
    return next(((v.get('path') or '').replace(' / ', '/') for v in found if v['docRef'].get('uuid') == ref['uuid']), None)


async def _search(ctx: Context, dashboard: dict[str, Any], expression: dict[str, Any], length: int = 100,
                  wait: float | None = None, doing: str | None = None) -> dict[str, Any]:
    """The table's rows, polling for up to wait seconds (SEARCH_WAIT by default); past that, the rows Stroom had found
    by then, with complete false. With doing (e.g. 'Searching ecs-web-v1 for ...'), the client is told so, and told
    again at every poll Stroom hasn't finished."""
    wait = SEARCH_WAIT if wait is None else wait
    left = gateway_from(ctx).token_seconds_left()
    if left is not None:
        wait = max(1.0, min(wait, left))    # what it has found by then, partial, rather than a request refused
    say = await progress_from(ctx) if doing else None
    if say:
        await say(doing)
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
        'storeHistory': False}
    started = time.monotonic()
    result: dict[str, Any] = {}
    try:
        while True:
            # Stroom holds each poll until the search completes or this many ms pass: never past the wait.
            request['timeout'] = max(500, min(5000, int((wait - (time.monotonic() - started)) * 1000)))
            result = await stroom.post('/dashboard/v1/search', request)
            if result.get('complete') or time.monotonic() - started >= wait:
                break
            if say:
                await say(f"{doing}: still searching, {int(time.monotonic() - started)} s of {int(wait)}")
            request['queryKey'] = result.get('queryKey')
            await asyncio.sleep(0.5)
    finally:
        await _destroy_search(stroom, result)
    table_result = next((r for r in result.get('results') or [] if r.get('componentId') == table['id']), {})
    columns = [f['name'] for f in settings['fields']]
    rows = [dict(zip(columns, row.get('values') or [])) for row in table_result.get('rows') or []]
    return {'rows': rows, 'errors': (result.get('errors') or []) + (table_result.get('errors') or []),
            'complete': bool(result.get('complete'))}


SEARCH_WAIT = 60    # seconds a search is polled; past that, what it has found so far is the answer, said to be partial


async def _destroy_search(stroom, result: dict[str, Any]) -> None:
    """End a search and free what Stroom holds for it. A search left behind runs to its end even when nobody asks for
    its results (seen: three whole-index scans of a large Elasticsearch index carried on after the survey gave up on
    them), and Stroom keeps its result store for 24 hours by default."""
    if not (result.get('queryKey') and result.get('node')):
        return
    try:
        await stroom.post(f"/result-store/v1/destroy/{result['node']}",
                          {'queryKey': result['queryKey'], 'destroyReason': 'NO_LONGER_NEEDED'})
    except ToolError as e:      # a Stroom without the result store API: it ends the search itself, later
        logger.info("Search %s not destroyed: %s", result['queryKey'], e)


async def _count(ctx: Context, dashboard: dict[str, Any], expression: dict[str, Any],
                 doing: str | None = None) -> tuple[int | None, list]:
    """How many documents the expression finds, however many: the dashboard's table asked for count() grouped on a
    constant, so one row holds it. Counting the rows of a search stopped at its first page (100) and at the table's
    maxResults (seen: 32,000 documents counted as 100)."""
    n, errors, _ = await _counted(ctx, dashboard, expression, doing)
    return n, errors


async def _counted(ctx: Context, dashboard: dict[str, Any], expression: dict[str, Any],
                   doing: str | None = None) -> tuple[int | None, list, bool]:
    """_count, and whether Stroom finished counting: a count cut off at SEARCH_WAIT is a floor, not the number (seen:
    compared with the documents expected, it failed verification as if they were missing)."""
    components = dashboard['dashboardConfig']['components']
    table = next(c for c in components if c['type'] == 'table')
    counting = {**table['settings'], 'fields': [
        {'id': 'mcp-all', 'name': 'All', 'expression': '"all"', 'group': 0, 'visible': True},
        {'id': 'mcp-count', 'name': 'Count', 'expression': 'count()', 'format': {'type': 'NUMBER'}, 'visible': True}]}
    found = await _search(ctx, {**dashboard, 'dashboardConfig': {**dashboard['dashboardConfig'], 'components': [
        c if c is not table else {**table, 'settings': counting} for c in components]}}, expression, length=1,
        doing=doing)
    complete = found.get('complete', True)
    if not found['rows']:
        return (0 if not found['errors'] else None), found['errors'], complete
    try:
        return int(float(found['rows'][0].get('Count') or 0)), found['errors'], complete
    except ValueError:
        return None, found['errors'], complete


Condition = Literal['EQUALS', 'NOT_EQUALS', 'CONTAINS', 'STARTS_WITH', 'ENDS_WITH', 'MATCHES_REGEX', 'GREATER_THAN',
                    'GREATER_THAN_OR_EQUAL_TO', 'LESS_THAN', 'LESS_THAN_OR_EQUAL_TO', 'BETWEEN', 'IN', 'IS_NULL',
                    'IS_NOT_NULL']


class SearchCheck(BaseModel):
    """One search, as a person would make it on a dashboard."""
    field: str
    condition: Condition = 'EQUALS'
    value: str = Field('', description="BETWEEN: 'from,to'; IN: values separated by commas; EQUALS takes * wildcards.")
    expected: int | None = Field(None, description="Rows it must return; none: at least one; 0: none (e.g. another case).")
    _searched_as: str | None = PrivateAttr(None)


def _as_searched(backend: str, searches: list[SearchCheck]) -> list[SearchCheck]:
    """IS_NOT_NULL and IS_NULL on Elasticsearch searched as EQUALS '*' and NOT_EQUALS '*', which is what they mean
    there: Stroom matches every document with either on Elasticsearch. Seen: refused with that advice, the agent sent
    IS_NOT_NULL again and again."""
    out = []
    for s in searches:
        if backend == 'elasticsearch' and s.condition in _NULL_AS:
            condition, said = _NULL_AS[s.condition]
            asked = s.condition
            s = s.model_copy(update={'condition': condition, 'value': '*'})
            s._searched_as = (f"{s.field} {condition} '*' ({said}), for {asked}: Stroom matches every document with "
                              f"{asked} on Elasticsearch")
        out.append(s)
    return out


_NULL_AS = {'IS_NOT_NULL': ('EQUALS', 'any value'), 'IS_NULL': ('NOT_EQUALS', 'no value')}


def _same_value(shown: str, written: str) -> bool | None:
    """Whether a value a dashboard shows is one a document was written with: as times, as numbers, else as text
    (either holding the other: a text field shows its words). None when they can't be compared (one a time, the
    other not): a dashboard formats some values its own way."""
    from datetime import datetime

    def when(v: str) -> datetime | None:
        try:
            t = datetime.fromisoformat(v.strip().replace('Z', '+00:00'))
            return t if t.tzinfo else None
        except ValueError:
            return None

    def number(v: str) -> float | None:
        try:
            return float(v)
        except ValueError:
            return None
    def whole(shown: str) -> bool | None:
        times, numbers = (when(shown), when(written)), (number(shown), number(written))
        if all(times):
            return times[0] == times[1]
        if all(n is not None for n in numbers):
            return numbers[0] == numbers[1]
        if any(times) or any(n is not None for n in numbers):
            return None
        return shown.lower() in written.lower() or written.lower() in shown.lower()
    same = whole(shown)
    # A field of several values, which a dashboard shows separated by commas ("80, 443"): the written value is the
    # hit's when it is one of them (asked about by the user; as a whole, numbers and times couldn't be compared).
    parts = [p.strip(' []') for p in shown.split(',') if p.strip(' []')]
    if same is not True and len(parts) > 1:
        each = [whole(p) for p in parts]
        if True in each:
            return True
        if same is None and all(e is False for e in each):
            return False
    return same


def _own_xslt_element(pipeline_uuid: str, layers: list[dict[str, Any]]) -> str | None:
    """The element running the pipeline's own XSLT: where its documents are written."""
    from tools.pipelines import translation_docs
    return next((e['element'] for e in translation_docs(pipeline_uuid, layers)
                 if e['doc'].get('type') == 'XSLT' and not e['inherited_from_template']), None)


def _hit_ids(row: dict[str, Any] | None) -> tuple[int, int] | None:
    try:
        return int(float(row['StreamId'])), int(float(row['EventId']))
    except (KeyError, TypeError, ValueError):
        return None


async def _traced(ctx: Context, pipeline_uuid: str, row: dict[str, Any], check: dict[str, Any],
                  step: Any = None, element: str | None = None) -> dict[str, Any]:
    """A hit back to its record: step the indexing pipeline at the hit's StreamId and EventId (the record's number
    in its stream, from 1: record index EventId - 1) and read the document it writes there. The hit is traced when
    that document holds the value searched for, and is the hit's: its EventId the hit's, or, when stepping numbers
    it otherwise, the only document the record gives, holding what the hit's other columns show.
    Stepping one record may not number it as processing did: Stroom's IdEnrichmentFilter counts the Events it reads,
    and seen on the live Stroom (verify_index), every record stepped alone was numbered 1 (EventId 1 traced, every
    other hit failed, and the agent told the user the tracer was off by one).
    step(stream, record index): the record already stepped (run_test_searches steps every hit of a stream in one
    session); by default, step_pipeline."""
    from utils.fielddoc import index_documents
    ids = _hit_ids(row)
    if ids is None:
        return {'traced': False, 'why': 'the hit has no StreamId and EventId to go back with'}
    stream, event = ids
    if element is None:
        element = _own_xslt_element(pipeline_uuid, await gateway_from(ctx).pipeline_layers(pipeline_uuid))
    if step is None:
        from tools.stepping import step_pipeline
        stepped = await step_pipeline(ctx, pipeline_uuid, stream, event - 1)
    else:
        stepped = await step(stream, event - 1)
    at = f"record index {event - 1} of stream {stream} (the hit's EventId {event}, counted from 1)"
    out = {'stream': stream, 'event': event}
    if stepped is None:
        return {'traced': False, **out, 'why': f"Stroom found no record at {at}"}
    if stepped.get('record') not in (None, event - 1):
        return {'traced': False, **out, 'why': f"asked to step {at}, Stroom stepped record index {stepped['record']}"}
    output = ((stepped.get('elements') or {}).get(element) or {}).get('output') or ''
    documents = index_documents([output]) if output else []
    ids = lambda d: [v.split('.')[0] for v in d.get('EventId', [])]
    document = next((d for d in documents if str(event) in ids(d)), None)
    if document is None and len(documents) == 1:
        document = documents[0]
        out['stepped_event_id'] = (ids(document) or [None])[0]
        # Matched on what the hit shows instead: each column the document also has, compared as the values are.
        agreed = []
        for column, shown in row.items():
            if column in ('StreamId', 'EventId') or shown in (None, '') or not document.get(column):
                continue
            same = [_same_value(str(shown), v) for v in document[column]]
            if all(s is False for s in same):
                return {'traced': False, **out, 'why': (
                    f"stepping {at} gives one document, numbered EventId {out['stepped_event_id']} by stepping, and "
                    f"it isn't the hit's: its {column} is {document[column]}, where the hit has {shown}")}
            agreed += [column] if True in same else []
        if not agreed:
            return {'traced': False, **out, 'why': (
                f"stepping {at} gives one document, numbered EventId {out['stepped_event_id']} by stepping, and none "
                f"of the hit's columns ({sorted(set(row) - {'StreamId', 'EventId'})}) has a value to match it on")}
        out['matched_on'] = agreed
    if document is None:
        return {'traced': False, **out, 'why': (
            f"stepping {at} with element {element} gives " + (
                f"{len(documents)} documents, EventIds {[i for d in documents for i in ids(d)]}, none {event}"
                if documents else "no document"))}
    field, value = check.get('field'), check.get('value')
    # A pattern (wildcard, or a CIDR range on an ip field) only needs the hit's record; a plain value must be there.
    holds = (check.get('condition', 'EQUALS') != 'EQUALS' or any(c in (value or '') for c in '*?/') or field not in document
             or any((value or '').lower() in v.lower() for v in document.get(field, [])))   # text fields match words
    return {'traced': holds, **out, **({} if holds else {
        'why': f"the document stepping {at} gives has {field} = {document.get(field)}, not {value}"})}


async def _trace_checks(ctx: Context, pipeline_uuid: str, checks: list[dict[str, Any]], say: Any,
                        out_of_time: Any) -> list[str]:
    """Each check's hit traced back to its record (_traced), every hit of a stream stepped in one stepping session;
    the checks left untraced when time ran out, by key. Stroom's stepping captures the whole stream in a session, and
    each hit stepped in a session of its own captured it again (seen: verifying a long stream took over five minutes,
    'Tracing the first hit' said check after check, and outlasted the user's token). The hit with the lowest EventId
    is the one traced: the record nearest the start of its stream."""
    from tools.stepping import _step
    stroom = gateway_from(ctx)
    chosen = []
    for check in checks:
        rows = [r for r in check.get('_rows') or [] if _hit_ids(r)]
        if rows:
            chosen.append((check, min(rows, key=lambda r: _hit_ids(r)[1])))
    if not chosen:
        return []
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    pipeline.refuse_mistyped()
    element = _own_xslt_element(pipeline_uuid, pipeline.layers)
    wanted: dict[int, set[int]] = {}
    for _, row in chosen:
        stream, event = _hit_ids(row)
        wanted.setdefault(stream, set()).add(event - 1)
    stepped: dict[tuple[int, int], Any] = {}
    total, n = sum(len(r) for r in wanted.values()), 0
    for stream, records in wanted.items():
        async with stroom.stepping() as session:
            for record in sorted(records):
                if out_of_time():
                    break
                n += 1
                await say(f"Tracing hits back to their records: record {record} of stream {stream} ({n} of {total})")
                try:
                    result = await _step(session, pipeline, stream, 'REFRESH',
                                         {'metaId': stream, 'partIndex': 0, 'recordIndex': record}, None)
                except ToolError as e:
                    stepped[(stream, record)] = e
                    continue
                elements = (result.get('stepData') or {}).get('elementMap') or {}
                stepped[(stream, record)] = {
                    'record': (result.get('foundLocation') or {}).get('recordIndex'),
                    'elements': {e: {'output': (d or {}).get('output') or ''} for e, d in elements.items()},
                } if result.get('foundRecord') else None

    async def step(stream: int, record: int) -> Any:
        return stepped[(stream, record)]
    untraced = []
    for check, row in chosen:
        stream, event = _hit_ids(row)
        done = stepped.get((stream, event - 1), ...)
        if done is ...:
            check['trace'] = {'traced': None, 'stream': stream, 'event': event, 'why': 'not traced: no time left'}
            check['pass'] = False
            untraced.append(check.get('_key') or check.get('check'))
        elif isinstance(done, ToolError):
            check['trace'] = {'traced': False, 'stream': stream, 'event': event,
                              'why': f"stepping record index {event - 1} of stream {stream} failed: {done}"}
            check['pass'] = False
        else:
            check['trace'] = await _traced(ctx, pipeline_uuid, row, check.get('_spec') or {}, step=step, element=element)
            check['pass'] = check['pass'] and check['trace']['traced']
    return untraced


_PASSED = 'verify_checks_passed'
_PASSED_TTL = 3600


async def _passed_checks(ctx: Context, index_uuid: str | None, stream_ids: list[int], expected: int,
                         pipeline_uuid: str | None) -> dict[str, dict[str, Any]]:
    """The checks that passed earlier in the session, by key, for these streams in this index as the indexing
    pipeline's current code wrote them: a store the run adds its own passes to. Empty, and kept nowhere, when there is
    no session store or no code to go by."""
    from tools.plan import _user
    try:
        code = await indexing_xslt_digest(gateway_from(ctx), pipeline_uuid) if pipeline_uuid else ''
        store = ctx.lifespan_context.setdefault(_PASSED, {})
        user = _user(ctx)
    except Exception:   # kept for convenience: without it, every check runs
        return {}
    now = time.time()
    for key in [k for k, v in store.items() if now - v['at'] > _PASSED_TTL]:
        del store[key]
    entry = store.setdefault((user, index_uuid, tuple(sorted(stream_ids)), expected, code), {'at': now, 'checks': {}})
    return entry['checks']


FULL_COUNT = 10_000     # a stream's documents counted one by one; past this, its first and last ENDS events are
ENDS = 1_000            # (and none past its last): every document of a stream would be read through Stroom


def _unsorted(dashboard: dict[str, Any]) -> dict[str, Any]:
    """The dashboard with its table unsorted, for checks: Stroom sorts a table itself, reading every match to do it,
    while an unsorted search stops once its page is full."""
    def plain(c: dict[str, Any]) -> dict[str, Any]:
        if c.get('type') != 'table':
            return c
        settings = c['settings']
        return {**c, 'settings': {**settings, 'fields': [{k: v for k, v in f.items() if k != 'sort'}
                                                         for f in settings.get('fields') or []]}}
    config = dashboard['dashboardConfig']
    return {**dashboard, 'dashboardConfig': {**config, 'components': [plain(c) for c in config['components']]}}


def _cut_off(n: int | None) -> str:
    return (f"Stroom had not finished counting after {SEARCH_WAIT} s: at least {n or 0} so far, not missing "
            f"documents. Call again to check once more.")


async def _stream_events(ctx: Context, stream_ids: list[int]) -> dict[int, int | None]:
    """Events in each stream, for a segmented one (an Events stream: a record each), as Stroom's data API says; None
    for a raw stream, whose count is of parts, or one it can't read."""
    stroom = gateway_from(ctx)
    sizes: dict[int, int | None] = {}
    for sid in stream_ids:
        try:
            first = await stroom.fetch_data(sid, 0, 1)
        except ToolError:
            sizes[sid] = None
            continue
        sizes[sid] = (first.get('totalItemCount') or {}).get('count') if first.get('dataType') == 'SEGMENTED' else None
    return sizes


async def _completeness(ctx: Context, dashboard: dict[str, Any], stream_ids: list[int],
                        sizes: dict[int, int | None], expected: int, label: str) -> dict[str, Any]:
    """Whether every document of the streams is in the index. A stream of more than FULL_COUNT events is checked by
    its first and last ENDS (EventId is the event's number in its stream, from 1) and by none past its last: a few
    thousand documents read, however large the stream (asked for by the user: a stream of 100,000 records was
    counted through Stroom, seven times over while the index caught up). The others are counted whole."""
    term = lambda f, c, v: {'type': 'term', 'field': f, 'condition': c, 'value': str(v)}
    of = lambda sids, *terms: {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'operator', 'op': 'OR', 'children': [term('StreamId', 'EQUALS', i) for i in sids]}, *terms]}
    large = {sid: n for sid, n in sizes.items() if n and n > FULL_COUNT}
    rest = [sid for sid in stream_ids if sid not in large]
    parts, errors = [], []
    if rest:
        want = expected - sum(large.values())
        n, found_errors, counted_all = await _counted(
            ctx, dashboard, of(rest), doing=f"Counting the documents of streams {rest} in {label}")
        errors += found_errors
        parts.append({'streams': rest, 'expected': want, 'returned': n, 'pass': counted_all and n == want,
                      **({} if counted_all else {'unproven': _cut_off(n)})})
    for sid, n in large.items():
        for what, terms, want in (
                (f'events 1 to {ENDS}', [term('EventId', 'GREATER_THAN_OR_EQUAL_TO', 1),
                                          term('EventId', 'LESS_THAN_OR_EQUAL_TO', ENDS)], ENDS),
                (f'events {n - ENDS + 1} to {n}', [term('EventId', 'GREATER_THAN_OR_EQUAL_TO', n - ENDS + 1),
                                                    term('EventId', 'LESS_THAN_OR_EQUAL_TO', n)], ENDS),
                (f'none past event {n}', [term('EventId', 'GREATER_THAN', n)], 0)):
            got, found_errors, counted_all = await _counted(
                ctx, dashboard, of([sid], *terms), doing=f"Counting stream {sid}'s {what} in {label}")
            errors += found_errors
            parts.append({'streams': [sid], 'of': what, 'expected': want, 'returned': got,
                          'pass': counted_all and got == want, **({} if counted_all else {'unproven': _cut_off(got)})})
    passed = all(p['pass'] for p in parts)
    if not large:
        only = parts[0]
        return {'check': f'documents for streams {stream_ids}', 'expected': expected, 'returned': only['returned'],
                'pass': passed, 'errors': errors, **({'unproven': only['unproven']} if 'unproven' in only else {}),
                'progress': f"{only['returned'] or 0} of {expected} documents indexed so far"}
    held = sum(p['returned'] or 0 for p in parts)
    return {'check': f'documents for streams {stream_ids}', 'expected': expected, 'pass': passed, 'errors': errors,
            'how': f"streams of more than {FULL_COUNT} events by their first and last {ENDS} and none past their last; "
                   f"the others counted whole",
            'parts': parts, 'progress': f"{sum(1 for p in parts if p['pass'])} of {len(parts)} counts as expected, "
                                        f"{held} documents found"}


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
    stroom = gateway_from(ctx)
    dashboard = dashboard_doc or await stroom.get_doc('Dashboard', dashboard_uuid)
    term = lambda f, c, v: {'type': 'term', 'field': f, 'condition': c, 'value': str(v)}
    by_stream = {'type': 'operator', 'op': 'OR', 'children': [term('StreamId', 'EQUALS', i) for i in stream_ids]}
    # What each search is, told to the client as it runs (asked for by the user), by the index the dashboard queries.
    say = await progress_from(ctx)
    query = next((c for c in dashboard['dashboardConfig']['components'] if c.get('type') == 'query'), {})
    source = (query.get('settings') or {}).get('dataSource') or {}
    label = source.get('name') or dashboard.get('name')
    # Every check is of the sample's streams, and unsorted. The index may hold millions of other documents: a check
    # of the whole index answered for them (a time range counted everyone's documents in it, an exact match passed on
    # any stream's), and the dashboard's table, sorted newest first, made Stroom read every match to sort it. The
    # user's dashboard stays sorted.
    dashboard = _unsorted(dashboard)
    scoped = lambda *terms: {'type': 'operator', 'op': 'AND', 'children': [by_stream, *terms]}
    # Checks that passed earlier in the session, of these streams in this index as the same code wrote them, aren't
    # run again: seen on a long stream, a call outlasted the user's token, and the next one started from the start.
    passed = await _passed_checks(ctx, source.get('uuid'), stream_ids, expected_documents, pipeline_uuid)
    unfinished: list[str] = []

    def out_of_time() -> bool:
        left = stroom.token_seconds_left()
        return left is not None and left <= 0

    async def run(key: str, check: Any) -> dict[str, Any] | None:
        """The check's result: from earlier in the session if it passed then, None when there is no time left."""
        if key in passed:
            return {**passed[key], 'checked': 'earlier in this session'}
        if out_of_time():
            unfinished.append(key)
            return None
        return {**await check(), '_key': key}

    async def documents() -> dict[str, Any]:
        sizes = await _stream_events(ctx, stream_ids)
        last = None
        for attempt in range(retries + 1):
            complete = await _completeness(ctx, dashboard, stream_ids, sizes, expected_documents, label)
            # Waited for only while the index catches up: not when Stroom couldn't finish counting (waiting doesn't
            # make it count faster), nor once two counts in a row found the same.
            unproven = 'unproven' in complete or any('unproven' in p for p in complete.get('parts') or [])
            if complete['pass'] or attempt == retries or unproven or complete['progress'] == last or out_of_time():
                break
            last = complete['progress']
            await say(f"Waiting for {label} to catch up: {complete['progress']} (try {attempt + 1} of {retries + 1})")
            await asyncio.sleep(5)
        found = await _search(ctx, dashboard, by_stream, doing=f"Reading the documents of streams {stream_ids} in {label}")
        complete.pop('progress')
        return {**complete, 'errors': complete['errors'] + found['errors'], 'sample': found['rows'][:3],
                '_rows': found['rows'], '_spec': {}}

    async def exact_match(item: dict[str, str]) -> dict[str, Any]:
        res = await _search(ctx, dashboard, scoped(term(item['field'], 'EQUALS', item['value'])),
                            doing=f"Searching {label}: {item['field']} = {item['value']}")
        # A value as a dashboard shows a field of several values ("retry, timeout"): the index holds each value on
        # its own, so EQUALS finds nothing (asked about by the user).
        several = not res['rows'] and len([p for p in str(item['value']).split(',') if p.strip()]) > 1
        return {'check': f"{item['field']} = {item['value']}", 'returned': len(res['rows']),
                'pass': len(res['rows']) >= 1, 'errors': res['errors'], 'sample': res['rows'][:2],
                **({'why': f"if {item['field']} holds several values (a dashboard shows them separated by commas), "
                           f"the index holds each on its own: search one of them, e.g. "
                           f"'{str(item['value']).split(',')[0].strip(' []')}'"} if several else {}),
                '_rows': res['rows'], '_spec': {'field': item['field'], 'condition': 'EQUALS', 'value': item['value']}}

    async def in_time_range() -> dict[str, Any]:
        between = scoped(term(time_range['field'], 'BETWEEN', f"{time_range['from']},{time_range['to']}"))
        doing = f"Searching {label}: {time_range['field']} between {time_range['from']} and {time_range['to']}"
        n, errors, counted_all = await _counted(ctx, dashboard, between, doing=doing)
        n = n if n is not None else len((await _search(ctx, dashboard, between, doing=doing))['rows'])
        return {'check': f"{time_range['field']} between {time_range['from']} and {time_range['to']}",
                'expected': time_range.get('expected'), 'returned': n,
                'pass': counted_all and n == time_range.get('expected', n), 'errors': errors,
                **({} if counted_all else {'unproven': _cut_off(n)})}

    async def other(search: SearchCheck) -> dict[str, Any]:
        expression = scoped(term(search.field, search.condition, search.value))
        doing = f"Searching {label}: {search.field} {search.condition} {search.value}".rstrip()
        res = await _search(ctx, dashboard, expression, doing=doing)
        n, counted_all = len(res['rows']), True
        if search.expected is not None and n >= 100:
            # A page is full: the number is counted, not the rows of the first page.
            counted, _, counted_all = await _counted(ctx, dashboard, expression, doing=doing)
            n = n if counted is None else counted
        return {'check': f"{search.field} {search.condition} {search.value}".rstrip(), 'expected': search.expected,
                'returned': n, 'pass': (n >= 1 if search.expected is None else n == search.expected)
                and counted_all and not res['errors'], 'errors': res['errors'], 'sample': res['rows'][:2],
                **({} if counted_all else {'unproven': _cut_off(n)}),
                **({'searched_as': search._searched_as} if search._searched_as else {}),
                '_rows': res['rows'], '_spec': {'field': search.field, 'condition': search.condition,
                                                'value': search.value}}

    planned: list[tuple[str, Any]] = [('documents', documents)]
    planned += [(f"exact {item['field']}={item['value']}", lambda item=item: exact_match(item)) for item in exact]
    if time_range:
        planned.append((f"time {json.dumps(time_range, sort_keys=True)}", in_time_range))
    planned += [(f"search {s.field} {s.condition} {s.value} {s.expected}", lambda s=s: other(s))
                for s in searches or []]
    checks = [c for key, check in planned if (c := await run(key, check)) is not None]
    if pipeline_uuid:
        unfinished += await _trace_checks(ctx, pipeline_uuid, [c for c in checks if '_key' in c], say, out_of_time)
    for check in checks:
        check.pop('_rows', None)
        check.pop('_spec', None)
        key = check.pop('_key', None)
        if key and check['pass']:
            passed[key] = dict(check)
    result = {'passed': all(c['pass'] for c in checks) and not unfinished, 'checks': checks}
    if unfinished:
        result['unfinished'] = unfinished
        result['next'] = (f"Stopped before your sign-in (access token) ran out, with {len(unfinished)} check(s) still "
                          f"to run: call verify_index again with the same arguments. The checks that passed are kept "
                          f"for this session, so only the rest run; your client sends a fresh token with every call, "
                          f"so there is nothing for the user to do.")
    return result


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


async def _component_choices(ctx: Context, pipeline_uuid: str,
                             nominated: list[str] | str = ()) -> tuple[list[str], list[str], list[str]]:
    """(include, exclude, notes): the components the user nominated, with those their standing instructions (an AGENTS
    doc's Component templates) include, and those the instructions exclude. The user's own nomination wins over an
    instruction to exclude it: their request takes precedence."""
    from tools.instructions import components_ruled
    nominated = [nominated] if isinstance(nominated, str) else list(nominated or [])
    rules = await components_ruled(ctx, pipeline_uuid)
    include = list(dict.fromkeys(nominated + [r['name'] for r in rules['include']]))
    exclude = [r['name'] for r in rules['exclude'] if r['name'] not in nominated]
    notes = [f"standing instructions (the AGENTS doc in {r['from'] or '(root)'}) {kind} component template "
             f"'{r['name']}'" for kind in ('include', 'exclude') for r in rules[kind]
             if not (kind == 'exclude' and r['name'] in nominated)]
    return [n for n in include if n not in exclude], exclude, notes


def components_wanted(example: dict[str, Any], components: dict[str, Any],
                      without: list[str] | str = ()) -> dict[str, Any] | None:
    """A needs_components reply while the example is composed of component templates not given (asked for by the
    user): the fields an index template maps are those of it and every component together, and this server doesn't
    read Elasticsearch, so it can't see them otherwise. Elastic's ecs@mappings is known here; those the user can't
    give (without) are let go."""
    from utils import ecs
    without = [without] if isinstance(without, str) else list(without or [])
    composed_of = list(example.get('composed_of') or [])
    # Seen in testing: a proposed template composed ecs@settings, which Elasticsearch doesn't have (ecs@mappings is its
    # only ECS component template), let go as one the user couldn't give. Only its content, given, shows it exists.
    unknown = [n for n in composed_of if n.lower().startswith('ecs@') and n != ecs.COMPONENT and n not in components]
    if unknown:
        raise ToolError(f"{unknown}: not a component template Elasticsearch has: {ecs.COMPONENT} is its only ECS one, "
                        f"built in. Don't compose it (leave it out of include_components). Only if the user's cluster "
                        f"has one by that name: its content in component_templates (GET _component_template/{unknown[0]}).")
    missing = [n for n in composed_of if n not in components and n != ecs.COMPONENT and n not in without]
    if not missing:
        return None
    # One request per name: GET _component_template takes a single name or wildcard, not a list (seen in testing: a
    # comma-separated list found nothing).
    requests = '\n'.join(f"GET _component_template/{n}" for n in missing)
    return {'status': 'needs_components', 'ok': False, 'composed_of': composed_of, 'given': sorted(components),
            'missing': missing, 'request': requests,
            'hint': ("The example is composed of component templates whose fields can't be seen here without them: the "
                     "fields it maps are those of the index template and all of its components together. Ask the user "
                     f"to run {'the request' if len(missing) == 1 else 'these requests (select them all and run: Dev Tools answers each)'} "
                     "in Kibana Dev Tools and paste the whole response, exactly as shown, then call again with "
                     "component_templates= that response alone: those given before are kept for the session. If a request says "
                     "resource_not_found, the cluster has no such component: it can't be composed, so leave it out. "
                     "If the user can't get one that exists, call again with without_components= its name: the "
                     "template is then built without its fields, which it may repeat or contradict.")}


def _component_notes(missing: list[str]) -> list[str]:
    """This server doesn't read Elasticsearch: component templates the user hasn't given can't be checked."""
    return [f"composed_of {missing} not given, so not checked: their fields aren't seen here. Ask the user for them "
            f"(GET _component_template/<name>) and check again with component_templates."] if missing else []


def _template_summary(name: str, body: dict[str, Any], shown: str) -> dict[str, Any]:
    """The template in a few short lines, for the confirmation form: the user reviews the whole request, with its
    notes, in the chat first (seen in VS Code: the form showed it as one long unformatted line, and an invalid
    template was agreed). Seen in testing: with every field, note and component template in it, the form ran to 2,000
    characters and the client declined it as too long. Bound to exactly what was shown by its review id."""
    mappings = (body.get('template') or {}).get('mappings') or {}
    fields = [p for p, spec in read_mapping_fields(body).items() if spec.get('type') not in (None, 'object')]
    patterns = body.get('index_patterns') or []
    return {'index template': f"{name}, exactly as shown in the chat (review id {review_id(shown)})",
            'applies to': ', '.join(patterns if isinstance(patterns, list) else [patterns])
                          + (f" (priority {body['priority']})" if body.get('priority') is not None else '')
                          + (', as a data stream' if 'data_stream' in body else ''),
            **({'composed of': ', '.join(body['composed_of'])} if body.get('composed_of') else {}),
            'fields mapped here': len(fields),
            'unmapped fields': {'false': 'kept in _source, not searchable', 'strict': 'refused',
                                'runtime': 'runtime fields'}.get(str(mappings.get('dynamic')).lower(),
                                                                'mapped by Elasticsearch as documents bring them')}


_SHOWN = 'templates_shown'


def _remember_shown(ctx: Context, dev_tools: str) -> None:
    """The template a needs_review reply shows, by its review id, so the call after the review needn't send it again
    (seen in testing: retyped by the agent, the template and its components came back corrupted)."""
    from tools.plan import _user
    try:
        shown = ctx.lifespan_context.setdefault(_SHOWN, {})
        now = time.time()
        for key in [k for k, v in shown.items() if now - v['at'] > _NAMING_TTL]:
            del shown[key]
        shown[(_user(ctx), review_id(dev_tools))] = {'text': dev_tools, 'at': now}
    except Exception:   # kept for convenience: the template can still be given
        pass


def _shown_template(ctx: Context, reviewed: Any) -> str | None:
    from tools.plan import _user
    try:
        return ctx.lifespan_context.get(_SHOWN, {}).get((_user(ctx), str(reviewed).strip().lower()), {}).get('text')
    except Exception:
        return None


async def _agree(ctx: Context, action: str, pipeline_uuid: str, destination: dict[str, Any], name: str,
                 body: dict[str, Any], components: dict[str, Any], notes: list[str],
                 confirmation_id: str | None, reviewed: str | bool | None = None) -> Any:
    """The user reviews the index template in the chat, then confirms it in a short form; once they have, it is
    kept with the pipeline as the agreed one, which create_processor_filter asks them to confirm is committed to the
    cluster. None once agreed."""
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Pipeline', pipeline_uuid)
    await guard_from(ctx).check_managed({'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': doc.get('name')})
    dev_tools = render_dev_tools(name, body)
    from utils.mappingstore import read_agreed_template
    kept = read_agreed_template(doc.get('description')) or {}
    try:
        same = bool(kept.get('dev_tools')) and parse_template(kept['dev_tools']) == (name, body) and \
            kept.get('index') == destination['index name'] and kept.get('cluster') == destination['cluster'] and \
            sorted(kept.get('component_templates') or []) == sorted(components) and \
            kept.get('xslt') == await indexing_xslt_digest(stroom, pipeline_uuid)
    except ValueError:
        same = False
    if same:
        # Already agreed, as it is: seen in Ask Stroom AI, the template the user had agreed was checked again once
        # committed, and they were asked to review and confirm it a second time.
        return None
    _remember_shown(ctx, dev_tools)
    if not was_reviewed(reviewed, dev_tools):
        # Passed on only with this template's id, as promote_build's layout (seen there: reviewed=true from the start).
        return {'status': 'needs_review', 'dev_tools': dev_tools, 'review_id': review_id(dev_tools),
                'hint': f"Show the user this index template before anything else, in your reply (not in your "
                        f"thinking, which they may not see): dev_tools as a json code block, exactly as it is (a form "
                        f"can't show it readably), with the notes on what came from where, and say it is not to go on "
                        f"the cluster until they've confirmed it. Then, in the same turn, without waiting for a reply, "
                        f"call {action} again with the same arguments plus reviewed='{review_id(dev_tools)}' (this "
                        f"template's review_id); "
                        + ("the template itself may be left out, and component templates given before are kept: don't "
                           "retype them" if action == 'check_index_template' else
                           "component templates given before are kept: don't send them again")
                        + ". The form that opens is where they confirm it or say what to change. Seen: an agent ended "
                          "its turn after showing it, and the user committed the template before it was agreed."
                        + review_note(reviewed)}
    gate = await consent_from(ctx).require(
        ctx, 'confirmation', action, f"Use Elasticsearch index template '{name}' for index "
        f"'{destination['index name']}' (cluster {destination['cluster']}), as shown in the chat",
        _template_summary(name, body, dev_tools), confirmation_id)
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
    # What to do first, the template after: seen in testing, the review reply ran to 10,000 characters, the client
    # showed it cut short, and the template was never put to the user. The fields written are in the template.
    shown = {k: result[k] for k in ('compatible', 'blocking', 'pipeline_changes', 'template_name', 'index', 'cluster',
                                    'self_check', 'from_example', 'notes', 'component_templates') if k in result}
    if isinstance(shown.get('self_check'), dict):
        shown['self_check'] = {k: v for k, v in shown['self_check'].items() if k != 'fields_written'}
    return {'status': gate.get('status'),
            'hint': gate['hint'] + " If the user corrects the template instead, check_index_template with their "
                                   "version: it is confirmed there (component templates given before are kept).",
            **{k: v for k, v in gate.items() if k not in ('status', 'hint')}, **shown}



async def _with_convention(ctx: Context, plan: FieldPlan, pipeline_uuid: str) -> FieldPlan:
    """The plan with the convention it was drafted under, when the agent's copy left it out (it decides whether the
    template composes ecs@mappings): taken from the plan kept with the pipeline, or drafted this session for the
    index."""
    if plan.convention:
        return plan
    try:
        kept = await plan_given(ctx, None, [plan.index_name], pipeline_uuid=pipeline_uuid)
    except Exception:   # nothing kept to go by: the plan as given
        return plan
    if kept is not None and kept.convention and {f.name for f in kept.fields} & {f.name for f in plan.fields}:
        return plan.model_copy(update={'convention': kept.convention,
                                       'custom_namespaces': plan.custom_namespaces or kept.custom_namespaces})
    return plan


def _ecs_base(example: dict[str, Any]) -> bool:
    """Whether a template built from the user's example leaves the ECS fields to ecs@mappings, with dynamic mapping on
    so it maps them: when the example references it (asked for by the user: ecs@mappings only when the user opts for
    ECS field mapping, by the ECS convention or an example that composes it). Seen in testing: a PascalCase example
    (User.Id), drafted with the ECS profile picking its paths, got ecs@mappings and dynamic mapping for @timestamp."""
    from utils import ecs
    return ecs.COMPONENT in (example.get('composed_of') or [])

async def propose_index_template(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The candidate Elasticsearch indexing pipeline.")],
        plan: Annotated[FieldPlan | None, Field(description=(
            "The field plan from draft_index_mapping (backend elasticsearch). Omitted: the one kept with the "
            "pipeline's indexing XSLT (what it writes), else the one drafted in this session."))] = None,
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Events streams to check the template against (required).")] = [],
        example_template: Annotated[str | None, Field(
            description="The user's example: the Elasticsearch index template a sibling source's index uses (GET "
                        "_index_template/<name>, or a Dev Tools request), or an existing index's mapping (GET "
                        "<index>/_mapping). Ask the user for it: the final template follows it.")] = None,
        component_templates: Annotated[list[str | dict[str, Any]] | str | dict[str, Any], ONE_OR_MORE, Field(
            description="Only if the example lists any in composed_of: those component templates, as the user gave them (GET "
                        "_component_template/<name>, or PUT _component_template/<name> {...}). Kept for the session once given: leave out those "
                        "given before, never send them again.")] = [],
        without_components: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Only when the user can't give some of the example's component templates: their names. The "
                        "template is then built without their fields, which it may repeat or contradict.")] = [],
        include_components: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Component templates the user wants the new template to compose besides the example's (or "
                        "without one), by name: referenced whatever they map, their fields left to them. Give their "
                        "content in component_templates too (but ecs@mappings, known here).")] = [],
        template_name: Annotated[str | None, Field(description="Template name; defaults to the index name.")] = None,
        priority: Annotated[int | None, Field(ge=0, description=(
            "Only when the user asks for one. None by default: with an example, the example's; without one, "
            "none (Elasticsearch's 0)."))] = None,
        without_example: Annotated[bool, Field(
            description="Only when the user has said they have no example: the template is built from the field plan "
                        "alone, and they confirm it as shown.")] = False,
        reviewed: Annotated[str | bool | None, Field(
            description="The review_id of the needs_review reply, once the user has been shown its template "
                        "(dev_tools) in the chat: they then confirm it in a form. Not true: a template that changes "
                        "gets a new id, to show again.")] = None,
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
    if not events_stream_ids:
        raise ToolError("Give events_stream_ids: the Events streams to check the template against")
    plan = await plan_given(ctx, plan, pipeline_uuid=pipeline_uuid)
    if plan is None:
        raise ToolError("Give plan= the field plan: the pipeline's indexing XSLT keeps none, and draft_index_mapping "
                        "drafted none (or several) in this session")
    if plan.backend != 'elasticsearch':
        raise ToolError("Index templates are for Elasticsearch; Lucene fields are set with create_index_doc (plan=...)")
    plan = await _with_convention(ctx, plan, pipeline_uuid)
    destination = await _destination(ctx, pipeline_uuid)
    index = destination['index name']
    name = template_name or index
    body = plan.model_copy(update={'index_name': index}).elastic_template(name, priority)['body']
    notes, components = [], {}
    include, exclude, ruled = await _component_choices(ctx, pipeline_uuid, include_components)
    kept_note = None
    if not without_example and (not example_template or not _is_template(example_template)):
        kept = await _kept_example(ctx, pipeline_uuid, [index, plan.index_name])
        if kept:
            example_template, component_templates, how = kept
            kept_note = (f"the example index template {how}, kept in the build when the plan was drafted"
                         + ("" if not how.endswith(FIELDS_ONLY) else ": its settings, component templates, keyword sub-fields and "
                                                     "ignore_above aren't readable through Stroom, so Elasticsearch's "
                                                     "defaults; the user may paste its index template for them")
                         + ("; what was given as example_template is not an index template" if example_template else ""))
    if example_template:
        try:
            _, example = parse_template(example_template)
            components = _given_components(ctx, component_templates, composition(
                example.get('composed_of') or [], include, exclude), example_template)
        except ValueError as e:
            raise ToolError(str(e)) from e
        wanted = components_wanted({'composed_of': composition(example.get('composed_of') or [], include, exclude)},
                                   components, without_components)
        if wanted:
            return wanted
        # Every planned field: the example says what it is composed of, and so which fields are left to that.
        full = plan.model_copy(update={'index_name': index}).elastic_template(name, priority, leave_to_component=False)
        body, notes = from_example(full['body'], example, components, discovery=plan.discovery is not None,
                                   include=include, exclude=exclude, ecs_base=_ecs_base(example))
        notes = ruled + notes
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
    plan_notes = []
    if not example_template and (include or exclude):
        try:
            components = _given_components(ctx, component_templates, include)
        except ValueError as e:
            raise ToolError(str(e)) from e
        wanted = components_wanted({'composed_of': include}, components, without_components)
        if wanted:
            return wanted
        body, plan_notes = with_components(body, components, include, exclude)
        plan_notes = ruled + plan_notes
    await _require_input(ctx, pipeline_uuid, events_stream_ids, plan)
    docs = await _documents(ctx, pipeline_uuid, events_stream_ids, 50)
    time_note = None
    if plan.discovery is not None and not example_template:
        # A discovery index is a data stream when the source's time field gives every record its time.
        from utils.templatecheck import discovery_data_stream
        body, time_note = discovery_data_stream(body, docs, plan.discovery.timestamp_field)
    composed, _ = compose(body, components)
    check = compare(composed, docs, index)
    if not example_template:
        notes = ["built from the field plan alone: the user has no example index template"] + (
            [time_note] if time_note else []) + plan_notes
    result = {'template_name': name, 'index': index, 'cluster': destination['cluster'], 'template': body,
              'dev_tools': render_dev_tools(name, body), 'self_check': check,
              **({'from_example': notes} if example_template else {'notes': notes}),
              **({'data_stream': time_note} if time_note else {}),
              'pipeline_link': doc_link(stroom.settings, 'Pipeline', pipeline_uuid)}
    if not check['compatible']:
        result['hint'] = ("It does not fit the documents the pipeline writes (self_check): show the user and ask "
                          "whether to change the indexing XSLT or the template. Not yet shown for confirmation.")
        return result
    gate = await _agree(ctx, 'propose_index_template', pipeline_uuid, destination, name, body, components, notes,
                        confirmation_id, reviewed)
    if gate:
        return _asking(gate, result)
    result.update({'agreed': True, **_commit_action(result, name, destination), 'hint': (
        "The user agreed this index template; it is kept with the pipeline. Give them dev_tools for the cluster admin "
        "to commit to the cluster, and end your turn. Once they say it is committed, create_processor_filter: its "
        "approval asks them to confirm that, and starts indexing.")})
    return result


async def check_index_template(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The candidate Elasticsearch indexing pipeline.")],
        template: Annotated[str | None, Field(description="The Elasticsearch index template as the user gave it: a Dev "
                                                          "Tools request (PUT _index_template/name {...}), the JSON body, "
                                                          "or GET _index_template output; or an existing index's mapping "
                                                          "(GET <index>/_mapping) as the example. Left out with reviewed=: "
                                                          "the template that review showed, kept for the session (never "
                                                          "retype it).")] = None,
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams to step the pipeline over.")] = [],
        component_templates: Annotated[list[str | dict[str, Any]] | str | dict[str, Any], ONE_OR_MORE, Field(
            description="Only if the index template lists any in composed_of: those component templates, as the user gave them: each a Dev "
                        "Tools request (PUT _component_template/name {...}) or GET _component_template output. Kept for the session once given: leave out those "
                        "given before, never send them again.")] = [],
        without_components: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Only when the user can't give some of the example's component templates: their names. The "
                        "template is then built without their fields, which it may repeat or contradict.")] = [],
        max_records: Annotated[int, Field(ge=1, le=500)] = 50,
        reviewed: Annotated[str | bool | None, Field(
            description="The review_id of the needs_review reply, once the user has been shown its template "
                        "(dev_tools) in the chat: they then confirm it in a form. Not true: a template that changes "
                        "gets a new id, to show again.")] = None,
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
    if not events_stream_ids:
        raise ToolError("Give events_stream_ids: the Events streams to step the pipeline over")
    template = template or _shown_template(ctx, reviewed)
    if not template:
        raise ToolError("Give template: the index template as the user gave it (or reviewed= the review_id of one "
                        "shown in this session)")
    try:
        name, body = parse_template(template)
        components = _given_components(ctx, component_templates, body.get('composed_of') or [], template)
    except ValueError as e:
        raise ToolError(str(e)) from e
    wanted = components_wanted(body, components, without_components)
    if wanted:
        return wanted
    include, exclude, _ = await _component_choices(ctx, pipeline_uuid)
    ruled = [f"composed_of lacks '{n}', which standing instructions include" for n in include
             if n not in (body.get('composed_of') or [])] + \
            [f"composed_of has '{n}', which standing instructions exclude" for n in exclude
             if n in (body.get('composed_of') or [])]
    from utils.templatecheck import ecs_duplicates
    doubled = ecs_duplicates(body)
    if doubled:
        # Seen in testing: asked to use ecs@mappings, the agent composed it and still mapped every ECS field itself.
        ruled.append(f"mapped explicitly just as ecs@mappings (which this template composes) maps them, so mapped "
                     f"twice: {doubled[:30]}" + (f" (+{len(doubled) - 30})" if len(doubled) > 30 else '')
                     + ". Leave them out of properties, with dynamic mapping on: the component maps them as documents "
                       "bring them (propose_index_template builds it that way)")
    body, missing = compose(body, components)
    destination = await _destination(ctx, pipeline_uuid)
    await _require_input(ctx, pipeline_uuid, events_stream_ids)
    result = compare(body, await _documents(ctx, pipeline_uuid, events_stream_ids, max_records),
                     destination['index name'])
    result['notes'] = ruled + _component_notes(missing) + result['notes']
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
    result.update({'agreed': True, **_commit_action(result, name or destination['index name'], destination), 'hint': (
        "The user agreed this index template; it is kept with the pipeline. Once they say the cluster admin has "
        "committed it to the cluster, create_processor_filter: its approval asks them to confirm that, and starts "
        "indexing.")})
    return result


def _commit_action(result: dict[str, Any], name: str, destination: dict[str, Any]) -> dict[str, Any]:
    """The agreed template's request, as a button for the cluster admin to copy (asked for by the user)."""
    from utils import useractions
    return useractions.actions(useractions.es_request(
        'commit-template', result.get('dev_tools') or '',
        f"Commit the agreed index template '{name}' to cluster {destination.get('cluster')}"))


def _cidr(value: str) -> str:
    """The CIDR range a wildcard address means: 192.0.2.* is 192.0.2.0/24."""
    known = [p for p in value.split('*')[0].split('.') if p.isdigit()][:3]
    return f"{'.'.join(known + ['0'] * (4 - len(known)))}/{8 * len(known)}" if known else '10.0.0.0/8'


_RANGES = ('BETWEEN', 'GREATER_THAN', 'GREATER_THAN_OR_EQUAL_TO', 'LESS_THAN', 'LESS_THAN_OR_EQUAL_TO')


def _unlisted(field: str) -> str:
    """Why Stroom doesn't list a field of an Elasticsearch index."""
    from utils import ecs
    kind = ecs.component_type(field)
    if kind in ecs.STROOM_UNLISTED:
        return (f"{field} isn't among the index's fields in Stroom: {ecs.COMPONENT} maps it as {kind}, a type Stroom "
                f"leaves out, so Stroom can't search or show it. Map it as "
                f"{'text' if kind in ('match_only_text', 'search_as_you_type') else 'keyword'} in the index template "
                f"(propose_index_template does, for a new index; an existing index keeps a field's type, so it takes a "
                f"new index or a reindex), and leave it out of these searches until then")
    return (f"{field} isn't among the index's fields in Stroom: either no document has it yet (a field left to dynamic "
            f"mapping is mapped once a document brings it), or its mapping type is one Stroom leaves out "
            f"({', '.join(sorted(ecs.STROOM_UNLISTED))}), so Stroom can't search it")


def _searchable(backend: str, searches: list[SearchCheck], ip_fields: set[str] = frozenset(),
                exact: list[dict[str, str]] = (), lucene_text: dict[str, str] | None = None,
                listed: set[str] | None = None, unindexed: dict[str, str] | None = None) -> None:
    """Searches whose answer would mislead: on Elasticsearch, Stroom (7.13) finds nothing for STARTS_WITH and
    CONTAINS, nor for a wildcard on an ip field, and matches every document for IS_NULL and IS_NOT_NULL, without an
    error; on Lucene it finds nothing for STARTS_WITH and ENDS_WITH, for CONTAINS on a keyword field, nor for a range
    on a text field (lucene_text: the index's TEXT fields and their analyzers); IN takes values separated by commas."""
    problems = [f"{name}: {why}, so no search through Stroom finds it (it is kept, and a dashboard column still shows it): "
                f"leave it out of the checks" for name, why in (unindexed or {}).items()]
    if backend == 'elasticsearch' and listed:
        # Seen in production: message, mapped as match_only_text by ecs@mappings, isn't listed by Stroom; every
        # search on it found nothing, and after three tries the agent dropped the check.
        for name in dict.fromkeys([s.field for s in searches] + [e.get('field') for e in exact if e.get('field')]):
            if name not in listed:
                problems.append(_unlisted(name))
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
            problems.append(f"{s.field} IN '{s.value}': separate the values with commas (one value holding a space is "
                            f"EQUALS '{s.value}')")
        elif backend == 'lucene' and s.condition == 'IN' and any(' ' in v.strip() for v in s.value.split(',')):
            # Seen (eval case 15): IN 'Main entrance,Server room' found 0 where EQUALS 'Server room' found 2.
            problems.append(f"{s.field} IN '{s.value}': Stroom finds nothing with IN on a Lucene index when a value "
                            f"holds a space; search each such value with EQUALS")
        if backend == 'elasticsearch' and s.condition in ('STARTS_WITH', 'CONTAINS'):
            bare = s.value.strip('*')
            pattern = f"{bare}*" if s.condition == 'STARTS_WITH' else f"*{bare}*"
            problems.append(f"{s.field} {s.condition} '{s.value}': Stroom finds nothing with {s.condition} on "
                            f"Elasticsearch; use EQUALS '{pattern}' (a wildcard) or MATCHES_REGEX")
        # Seen on the local Stroom (eval case 22, Haiku): each of these found 0 where EQUALS with a wildcard found
        # them all, and the agent spent twenty turns on checks that could never pass.
        if backend == 'lucene' and s.condition in ('STARTS_WITH', 'ENDS_WITH'):
            bare = s.value.strip('*')      # seen: STARTS_WITH 'A*', given back as EQUALS 'A**'
            pattern = f"{bare}*" if s.condition == 'STARTS_WITH' else f"*{bare}"
            problems.append(f"{s.field} {s.condition} '{s.value}': Stroom finds nothing with {s.condition} on a Lucene "
                            f"index; use EQUALS '{pattern}' (a wildcard)")
        analyzer = (lucene_text or {}).get(s.field)
        if backend == 'lucene' and s.condition == 'CONTAINS' and analyzer == 'KEYWORD':
            problems.append(f"{s.field} CONTAINS '{s.value}': {s.field} is a keyword field, where Stroom finds nothing "
                            f"with CONTAINS; use EQUALS '*{s.value.strip('*')}*' (a wildcard)")
        if backend == 'lucene' and s.condition in _RANGES and analyzer:
            problems.append(f"{s.field} {s.condition} '{s.value}': {s.field} is a text field in Lucene, where Stroom "
                            f"finds nothing with a range; use EQUALS with a wildcard (an address range as '10.1.*') "
                            f"or IN with the values")
        if backend == 'elasticsearch' and s.condition == 'IS_NOT_NULL':
            problems.append(f"{s.field} IS_NOT_NULL: Stroom matches every document with IS_NOT_NULL on Elasticsearch; "
                            f"use EQUALS '*' (any value) instead")
        if backend == 'elasticsearch' and s.condition == 'IS_NULL':
            problems.append(f"{s.field} IS_NULL: Stroom matches every document with IS_NULL on Elasticsearch; check "
                            f"EQUALS '*' (any value) with the expected count instead")
    if problems:
        raise ToolError('These searches would give a misleading answer: ' + '; '.join(problems))


async def _columns_not_in_index(ctx: Context, ref: dict[str, Any], columns: list[str], pipeline_uuid: str | None) -> None:
    """Refuses table columns Stroom doesn't list for the index: a column on one is empty in every row. Seen in Ask
    Stroom AI: EventSource.User.Id, the Events path, was offered and chosen in place of user.name, the field the plan
    indexed it as; the dashboard was saved and verification passed with that column empty. A path the pipeline's plan
    indexes is answered with the field it became."""
    stroom = gateway_from(ctx)
    try:
        missing = (await fields_not_in_index(stroom, ref['type'], ref, {'columns': list(columns)})).get('columns')
        if not missing:
            return
        listed = sorted(f['name'] for f in await _index_fields(stroom, ref['type'], ref))
    except Exception:    # a check on the columns, not a reason for verification to fail
        return
    planned: list[PlannedField] = []
    if pipeline_uuid:
        from tools.builds import kept_mapping
        try:
            kept = await kept_mapping(ctx, pipeline_uuid)
            if kept and kept['kind'] == 'index':
                planned = FieldPlan.model_validate(kept['payload']).fields
        except Exception:    # without the plan, the index's fields are still listed
            planned = []
    from utils import ecs
    said = []
    for name in missing:
        path = name.replace('.', '/').strip('/')
        became = next((f.name for f in planned if source_matches(f.source.strip('/'), path)), None)
        if became and became in listed:
            said.append(f"{name} (an Events path: the plan indexes it as {became})")
        elif ref['type'] == 'ElasticIndex' and ecs.component_type(name) in ecs.STROOM_UNLISTED:
            said.append(_unlisted(name))
        else:
            said.append(name)
    raise ToolError(f"Not fields of index '{ref.get('name')}' in Stroom, so a dashboard column on them would be empty "
                    f"in every row: {'; '.join(said)}. A column is the index's field name, not the Events path it "
                    f"comes from. Its fields: {', '.join(listed[:60])}. Confirm the corrected columns with the user, "
                    f"then call again.")


async def verify_index(
        ctx: Context,
        build: Build,
        index_uuid: Annotated[str, Field(description="The index doc that was indexed into.")],
        backend: Backend,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams that were indexed.")],
        expected_documents: Annotated[int, Field(description="Events records in those streams.")],
        fields: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="The dashboard table's columns, which the user chose: the index's field names (as the plan "
                        "named them, e.g. user.name), not the Events paths they come from (EventSource/User/Id). Suggest "
                        "the time field and the plan's key fields (user, host, address, event type, outcome) and "
                        "confirm them with the user. StreamId and "
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
                        "at the hit's StreamId and EventId (record index EventId - 1); a trace that fails says what "
                        "it compared (trace.why).")] = None,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
        checks: Annotated[bool, Field(description=(
            "False only when the user asks to skip verification: the build's -VERIFY dashboard is made (or its columns "
            "changed) as the user confirms, and its link returned, with no test searches. Nothing is marked verified, "
            "so promotion still waits for a verification that passes."))] = True,
) -> dict[str, Any]:
    """
    Verify indexed events through Stroom, not by querying the backend: a workspace dashboard on the index doc,
    then the test searches: the sample stream ids, an exact match on each key field, a time range and any further
    searches, each of the sample's streams only (the rest of the index never answers for them); with the indexing
    pipeline, each hit traced back to the record it came from. Passes when every check returns what it should. A
    stream of more than 10,000 events is checked by its first and last 1,000 and none past its last, not counted
    whole; a count Stroom hadn't finished in time is unproven, not a failure: call again. The dashboard (created once per build, for an index the build made, after the user
    confirms its columns) has a query on the time field from the sample's earliest event (rounded back to a 30-day
    boundary) through the end of today, a table of the user's fields newest first, and a text pane on the selected row's record
    with stepping. An index from elsewhere is searched without saving a dashboard, unless pipeline_uuid is the build's
    own pipeline writing to it: the dashboard is then the build's, promoted with that pipeline. checks=false, when
    the user asks to skip verification, makes the dashboard and gives its link, with no searches.
    """
    searches = _as_searched(backend, [SearchCheck.model_validate(s) if isinstance(s, dict) else s for s in searches])
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
                    [e for e in exact if isinstance(e, dict)], listed=set(typed))
    elif any(s.condition == 'CONTAINS' or s.condition in _RANGES for s in searches):
        try:
            found = await stroom.post('/index/v2/findFields', {'dataSourceRef': {'type': 'Index', 'uuid': index_uuid},
                                                               'pageRequest': {'offset': 0, 'length': 2000}})
            text = {f['fldName']: f.get('analyzerType') or '' for f in found.get('values') or []
                    if f.get('fldName') and f.get('fldType') == 'TEXT'}
        except Exception:    # a check on the searches, not a reason for verification to fail
            text = {}
        _searchable(backend, searches, lucene_text=text)
    searched = [s.field for s in searches] + [e.get('field') for e in exact if isinstance(e, dict) and e.get('field')] \
        + ([time_range['field']] if time_range and time_range.get('field') else [])
    ref = {'type': INDEX_TYPE[backend], 'uuid': index_uuid, 'name': index.get('name')}
    unindexed = await _unsearchable(ctx, INDEX_TYPE[backend], ref, list(dict.fromkeys(searched)), pipeline_uuid)
    if unindexed:
        _searchable(backend, [], unindexed=unindexed)
    name = dashboard_name or f"{index.get('name')}-VERIFY"
    fields = [f for f in fields if f not in _IDS]
    time_field = index.get('timeField') or index.get('timeFieldName')
    source = {'type': INDEX_TYPE[backend], 'uuid': index_uuid, 'name': index.get('name')}
    await _columns_not_in_index(ctx, source, fields, pipeline_uuid)
    contents = await guard_from(ctx).folder_contents(build)
    # The build's index, or one from elsewhere that the build's own indexing pipeline writes to (a new version writing
    # through the existing index doc): the dashboard is the build's, and promoted with it (seen in production: a new
    # version's verification left no dashboard to promote).
    ours = any(d['uuid'] == index_uuid for d in contents) or any(
        d['type'] == 'Pipeline' and d['uuid'] == pipeline_uuid for d in contents)
    existing = next((d for d in contents if d['type'] == 'Dashboard' and d['name'] == name), None)
    # The window starts at the sample's earliest event: found by searching its streams first, saving nothing.
    probe = {'uuid': str(uuidlib.uuid4()), 'name': name, 'dashboardConfig': dashboard_config(source, fields, time_field, None)}
    sample = await _search(ctx, probe, {'type': 'operator', 'op': 'OR', 'children': [
        {'type': 'term', 'field': 'StreamId', 'condition': 'EQUALS', 'value': str(i)} for i in stream_ids]},
        doing=f"Reading a sample of streams {stream_ids} from {index.get('indexName') or index.get('name')}")
    window = window_start([r.get(time_field) for r in sample['rows']]) if time_field else None
    design = {'columns (newest first)': fields, 'hidden': list(_IDS) + ([time_field] if time_field and time_field not in fields else []),
              'initial query': f"{time_field} from {window} through today" if window else 'none',
              'text pane': "the selected row's record, with stepping; no extraction pipeline"}
    dashboard: dict[str, Any] | None = None
    if not ours:
        # Another build's (or production's) index, not written by this build: searched through an unsaved dashboard.
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
    if not checks:
        # Seen in testing: told to skip verification and give the dashboard, the agent went through making a dashboard
        # of its own (asking the user which kind), and the -VERIFY one was never finished.
        if not dashboard.get('saved'):
            raise ToolError(f"Index '{index.get('name')}' isn't the build's, nor written by its pipeline (pipeline_uuid), "
                            f"so no dashboard is saved for it: there is nothing to give without the checks")
        return {'dashboard': {**dashboard, 'link': doc_link(stroom.settings, 'Dashboard', dashboard['uuid']), **design},
                'checks_skipped': 'as the user asked',
                'hint': "Give the user the dashboard's link. Verification was skipped at their request: nothing is "
                        "marked verified, so promotion waits for verify_index with the checks to pass."}
    searched = await run_test_searches(ctx, dashboard['uuid'], stream_ids, expected_documents, exact, time_range, retries,
                                       searches=list(searches), pipeline_uuid=pipeline_uuid, dashboard_doc=doc)
    if pipeline_uuid:
        # Every field the pipeline writes for these Events must be in the index (asked for by the user: an index that
        # lacks one is an error to put to them, not something its documentation leaves out unsaid).
        lacking = await _fields_written_not_indexed(ctx, pipeline_uuid, source, stream_ids)
        if lacking:
            searched['checks'].append({'check': 'every field the pipeline writes for these Events is in the index',
                                       'missing': lacking, 'pass': False,
                                       'next': missing_fields_said(lacking, index.get('indexName') or index.get('name'))})
            searched['passed'] = False
    # The link to give the user (seen in VS Code: the user had to ask for it); an unsaved dashboard has none.
    link = {'link': doc_link(stroom.settings, 'Dashboard', dashboard['uuid'])} if dashboard.get('saved') else {}
    result = {'dashboard': {**dashboard, **link, **design}, **searched}
    if searched['passed']:
        # The plan's 'indexed' step is done for the build's pipelines that write to this index.
        recorded = []
        for d in contents:
            if d['type'] != 'Pipeline':
                continue
            try:
                merged = merge_layers(await stroom.pipeline_layers(d['uuid']))
            except ToolError:   # one Stroom can't load: not recorded as verified, the others still are
                continue
            properties = {(q['element'], q['name']): q['value'] for q in merged['properties']}
            if writes_to(properties, INDEX_TYPE[backend], index_uuid, index.get('indexName')) \
                    and await remember_verified(ctx, {'type': 'Pipeline', 'uuid': d['uuid'], 'name': d['name']}):
                recorded.append(d['name'])
        if recorded:
            result['verified'] = recorded
            from tools.plan import with_next
            return await with_next(ctx, build, result)
    return result


_FIND_COLUMNS = 8
_ALL_STREAMS = 10_000      # Events streams find_events lists with all_streams


async def _affected(stroom, index: dict[str, Any], by_stream: list[dict[str, Any]], total: int | None) -> dict[str, Any]:
    """What a translation change to these events reaches: the Events streams holding them, the raw streams they were
    made from (to reprocess, the user's to do), and for Elasticsearch the request that deletes their old documents
    before they are indexed again (or they are indexed twice)."""
    ids = []
    for row in by_stream:
        try:
            ids.append(int(float(row['stream_id'])))
        except (TypeError, ValueError):
            continue
    raw: set[int] = set()
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        terms = [{'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(i)} for i in batch]
        rows = (await stroom.find_meta(terms, len(batch), op='OR')).get('values') or []
        raw |= {r['meta']['parentMetaId'] for r in rows if r['meta'].get('parentMetaId')}
    out: dict[str, Any] = {'events': total, 'events_streams': ids, 'raw_streams': sorted(raw),
                           'reprocess': "The raw streams are the user's to reprocess, once the change is promoted: "
                                        "exactly these, not the whole feed."}
    if len(ids) >= _ALL_STREAMS:
        out['note'] = f"Stopped at {_ALL_STREAMS} Events streams: narrow the time range and search again for the rest."
    if index.get('indexName'):
        out['elasticsearch_delete'] = (
            f"POST {index['indexName']}/_delete_by_query "
            f'{{"query": {{"terms": {{"StreamId": {json.dumps(ids)}}}}}}}')
        out['delete_when'] = ("The cluster's admin runs it just before the raw streams are reprocessed (the new Events "
                              "are then indexed again): otherwise the index holds these events twice.")
    return out


class FindTerm(BaseModel):
    """One condition, as a person would set it on a dashboard."""
    field: str
    condition: Condition = 'EQUALS'
    value: str = Field('', description="BETWEEN: 'from,to'; IN: values separated by commas; EQUALS takes * wildcards.")


async def find_events(
        ctx: Context,
        index_uuid: Annotated[str, Field(description="The index to search: its Elastic Index or Lucene Index doc "
                                                     "(find_documents types=['ElasticIndex', 'Index']).")],
        where: Annotated[list[FindTerm] | str, ONE_OR_MORE, Field(
            description="Conditions the event meets, all of them, on the index's fields as the user describes it, "
                        "e.g. [{'field': 'TypeId', 'value': 'RoleChange'}, {'field': 'UserId', 'value': 'admin*'}].")] = [],
        time_from: Annotated[str | None, Field(description="From this time (ISO 8601, or Stroom's day()-7d).")] = None,
        time_to: Annotated[str | None, Field(description="Up to this time (ISO 8601, or Stroom's now()).")] = None,
        fields: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Index fields to show for each hit beside StreamId, EventId and the time (the searched ones "
                        "are shown too).")] = [],
        limit: Annotated[int, Field(ge=1, le=20, description="Hits to return, newest first.")] = 10,
        all_streams: Annotated[bool, Field(
            description="Every Events stream holding a match (not only the 20 with the most), with the raw stream "
                        "each was made from: the streams to reprocess once a translation change is promoted, and for "
                        "Elasticsearch the request that deletes their old documents first.")] = False,
) -> dict[str, Any]:
    """
    Find events in an index through Stroom, as the user would on a dashboard (with their permissions; nothing is
    saved): for an event the user describes but can't point at. Each hit has its Events stream's StreamId and its
    EventId, for locate_event, which traces it to its raw record; with how many events match in all, and in which
    streams. Read only. Searches only fields the index has.
    """
    stroom = gateway_from(ctx)
    terms = [FindTerm.model_validate(t) if isinstance(t, dict) else t for t in where]
    doc_type, index = None, None
    for candidate in ('ElasticIndex', 'Index'):
        try:
            index = await stroom.get_doc(candidate, index_uuid)
            doc_type = candidate
            break
        except ToolError:
            continue
    if not doc_type:
        from utils.stroom import not_found
        raise ToolError(not_found(stroom.settings, f"No Elastic Index or Lucene Index doc with UUID {index_uuid}"))
    ref = {'type': doc_type, 'uuid': index_uuid, 'name': index.get('name')}
    info = await _index_fields(stroom, doc_type, ref)
    listed = {f['name']: f['type'] for f in info}
    unindexed = {f['name']: "the index doc's field isn't indexed" if doc_type == 'Index' else
                 "Stroom lists it as not searchable" for f in info
                 if f.get('searchable') is False and f['name'] in {t.field for t in terms}}
    backend = 'elasticsearch' if doc_type == 'ElasticIndex' else 'lucene'
    unknown = [n for n in [t.field for t in terms] + list(fields) if n not in listed]
    if unknown:
        import difflib
        near = {n: difflib.get_close_matches(n, list(listed), 3, 0.5) for n in unknown}
        raise ToolError(f"Not fields of index '{ref['name']}': " + '; '.join(
            f"{n} (like {', '.join(m)})" if m else n for n, m in near.items())
            + f". Its fields: {', '.join(sorted(listed)[:60])}")
    if backend == 'elasticsearch':
        # Any value or none, which IS_NOT_NULL and IS_NULL mean: Stroom matches every document with them on
        # Elasticsearch.
        terms = [t.model_copy(update={'condition': _NULL_AS[t.condition][0], 'value': '*'})
                 if t.condition in _NULL_AS else t for t in terms]
    _searchable(backend, [SearchCheck(field=t.field, condition=t.condition, value=t.value) for t in terms],
                {n for n, t in listed.items() if t in ('ip', 'ipv4_address')}, unindexed=unindexed)
    time_field = index.get('timeField') or index.get('timeFieldName')
    children = [{'type': 'term', 'field': t.field, 'condition': t.condition, 'value': t.value} for t in terms]
    if time_from or time_to:
        if not time_field:
            raise ToolError(f"Index '{ref['name']}' has no time field to search a time range on")
        children.append({'type': 'term', 'field': time_field, 'condition': 'BETWEEN',
                         'value': f"{time_from or '1970-01-01T00:00:00.000Z'},{time_to or 'day()+1d'}"})
    if not children:
        raise ToolError("Give where (the conditions the event meets) or a time range: this finds an event, it doesn't "
                        "list the index")
    expression = {'type': 'operator', 'op': 'AND', 'children': children}
    shown = list(dict.fromkeys([n for n in (*_IDS, time_field) if n and n in listed]
                               + [t.field for t in terms] + list(fields)))[:_FIND_COLUMNS]

    def probe(columns: list[dict[str, Any]], rows: int) -> dict[str, Any]:
        return {'uuid': str(uuidlib.uuid4()), 'name': f"{ref['name']} search", 'dashboardConfig': {'components': [
            {'type': 'query', 'id': 'query-FIND', 'name': 'Query', 'settings': {
                'type': 'query', 'dataSource': ref, 'expression': expression}},
            {'type': 'table', 'id': 'table-FIND', 'name': 'Table', 'settings': {
                'type': 'table', 'queryId': 'query-FIND', 'fields': columns, 'extractValues': False,
                'maxResults': [rows], 'pageSize': rows}}]}}

    columns = [{**_column(n), **({'sort': {'order': 0, 'direction': 'DESCENDING'}} if n == time_field else {})}
               for n in shown]
    label = index.get('indexName') or index.get('name')
    searched = ' AND '.join(f"{c['field']} {c['condition']} {c['value']}".rstrip() for c in children)
    found = await _search(ctx, probe(columns, limit), expression, length=limit,
                          doing=f"Searching {label} for {searched}")
    total, count_errors = await _count(ctx, probe(columns, limit), expression,
                                       doing=f"Counting the events in {label} matching {searched}")
    hits = [{k: (str(v)[:120] if v is not None else None) for k, v in row.items()} for row in found['rows'][:limit]]
    by_stream: list[dict[str, Any]] = []
    affected: dict[str, Any] | None = None
    if 'StreamId' in listed:
        streams = _ALL_STREAMS if all_streams else 20
        grouped = await _search(ctx, probe([
            {**_column('StreamId'), 'group': 0},
            {'id': 'mcp-n', 'name': 'events', 'expression': 'count()', 'format': {'type': 'NUMBER'}, 'visible': True,
             'sort': {'order': 0, 'direction': 'DESCENDING'}}], streams), expression, length=streams,
            doing=f"Grouping the matching events in {label} by stream")
        by_stream = [{'stream_id': r.get('StreamId'), 'events': r.get('events')} for r in grouped['rows']]
        if all_streams:
            affected = await _affected(stroom, index, by_stream, total)
            by_stream = by_stream[:20]
    errors = list(dict.fromkeys(str(e) for e in found['errors'] + count_errors))
    result: dict[str, Any] = {
        'index': {**ref, 'backend': backend}, 'searched': [f"{c['field']} {c['condition']} {c['value']}" for c in children],
        'total': total if total is not None else len(hits), 'hits': hits, 'by_stream': by_stream,
        **({'affected': affected} if affected else {}),
        **({'errors': errors} if errors else {})}
    if not hits:
        result['next'] = ("Nothing matches. Check the conditions with the user (the values as the dashboard shows them; "
                          "EQUALS takes * wildcards), or ask them for the event's StreamId and EventId from the "
                          "dashboard row where they saw it.")
    elif 'StreamId' not in listed or 'EventId' not in listed:
        result['next'] = "The index has no StreamId and EventId: ask the user for the Events stream and event id."
    else:
        result['next'] = ("Confirm with the user which hit is the event they mean (the hits are newest first), then "
                          "locate_event stream_id=<its StreamId> event_id=<its EventId> traces it to its raw record. "
                          "by_stream says which Events streams hold every match, with how many each.")
    return result


ALL_TOOLS = [get_field_conventions, draft_index_mapping, propose_index_template, check_index_template,
             find_elastic_clusters, create_index_doc, create_indexing_pipeline, verify_index, find_events]
