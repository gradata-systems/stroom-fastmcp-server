"""Generating translation code from a mapping, so the model does not have to write XSLT by hand."""
import asyncio
import re
import time
import json
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from tools.instructions import applicable_instructions
from utils.eventschema import EventSchema
from utils.params import ONE_OR_MORE
from utils.schemas import SchemaCache, event_logging_system_id, event_logging_version
from utils.consent import consent_from, review_id, review_note, was_reviewed
from utils.stroom import gateway_from
from tools.stepping import _outputs, _Pipeline
from tools.streams import SampleStreams, read_sample_streams
from pydantic import ValidationError

from utils.draftmap import draft_mapping
from utils.dsgen import EXAMPLES, SplitterSpec, dry_run, generate_splitter, infer_spec
from utils.samples import SAMPLE_GUIDE, SampleTexts, check_sample
from utils.localcheck import check_mapping, check_schema_values, sample_records
from utils.mappingchanges import apply_changes
from utils.mappingstore import read_mapping
from utils.xpathcheck import check_extractions, check_xpaths
from utils.profile import _inventory
from utils.refgen import ReferenceMapping, generate_reference
from utils.fielddoc import field_mapping_markdown, sampled_events
from utils.xsltgen import TranslationMapping, compact_pairs, compact_rules, generate, grouped, kept_unknown


def _first_sentence(text: str, limit: int = 220) -> str:
    first = re.split(r'(?<=\.) ', text, maxsplit=1)[0]
    return first if len(first) <= limit else first[:limit - 20].rsplit(' ', 1)[0] + ' …'


async def event_schema(ctx: Context, version: str) -> EventSchema:
    schemas = ctx.lifespan_context.setdefault('event_schemas', {})
    if version not in schemas:
        cache = ctx.lifespan_context.setdefault('schemas', SchemaCache(gateway_from(ctx)))
        schemas[version] = EventSchema.parse(await cache.source(event_logging_system_id(version)))
    return schemas[version]


async def event_versions(ctx: Context) -> list[str]:
    """The event-logging versions this Stroom holds, newest first, or ToolError if it holds none."""
    versions = await ctx.lifespan_context.setdefault('schemas', SchemaCache(gateway_from(ctx))).event_versions()
    if not versions:
        raise ToolError("This Stroom holds no event-logging XML schema (System/XML Schemas): ask the user to import "
                        "Stroom's event-logging schema pack first.")
    return versions


# The version the user chose for a build's translation, this session: so build_translation_xslt doesn't ask again what
# draft_translation_mapping asked.
_VERSION_CHOSEN = 'event_logging_version_chosen'
_VERSION_HOW = 'event_logging_version_how'


def _build_key(ctx: Context, build: str | None) -> str:
    from tools.plan import _user
    try:
        return build or ctx.lifespan_context.get('current_build', {}).get(_user(ctx)) or ''
    except Exception:
        return build or ''


def _remember_version(ctx: Context, build: str | None, version: str, how: str = 'given') -> None:
    """The version for the build, and how it was settled: 'given' (schema_version), 'form' (the user picked it in the
    server's form) or 'only' (the one version this Stroom holds)."""
    from tools.plan import _user
    try:
        key = _build_key(ctx, build)
        ctx.lifespan_context.setdefault(_VERSION_CHOSEN, {}).setdefault(_user(ctx), {})[key] = version
        ctx.lifespan_context.setdefault(_VERSION_HOW, {}).setdefault(_user(ctx), {})[key] = how
    except Exception:
        pass


def version_settled(ctx: Context, build: str | None, version: str) -> str | None:
    """For the agent, where the version was settled without it: seen in Ask Stroom AI, the user picked 4.1.0 in the
    draft's form, and the agent, told only schema_version 4.1.0, asked them again with it as the one option."""
    from tools.plan import _user
    try:
        how = ctx.lifespan_context.get(_VERSION_HOW, {}).get(_user(ctx), {}).get(_build_key(ctx, build))
    except Exception:
        return None
    if how == 'form':
        return f"The user chose event-logging {version} in the server's form: don't ask them for the version again."
    if how == 'only':
        return f"{version} is the only event-logging version this Stroom holds: there is no version to ask about."
    return None


def _version_chosen(ctx: Context, build: str | None) -> str | None:
    from tools.plan import _user
    try:
        return ctx.lifespan_context.get(_VERSION_CHOSEN, {}).get(_user(ctx), {}).get(_build_key(ctx, build))
    except Exception:
        return None


async def chosen_or_latest(ctx: Context, build: str | None = None) -> str:
    """The event-logging version to read the schema of where nothing names one: the one the user chose for the
    build, else the newest this Stroom holds."""
    chosen = _version_chosen(ctx, build)
    return chosen if chosen else (await event_versions(ctx))[0]


async def translation_version(ctx: Context, payload: dict[str, Any] | None, code: str | None) -> str:
    """The event-logging version an existing translation targets: the one kept with its mapping, else the one its
    code writes, else the newest this Stroom holds."""
    return ((payload or {}).get('schema_version') or event_logging_version(code)
            or (await event_versions(ctx))[0])


_VERSION_QUESTION = "Which event-logging schema version should the translation write?"
# Seen: an agent took the nearest 3.x to a missing 3.5.2 (3.4.2) where 4.1.0 was installed, as if namespace
# event-logging:3 bound it to 3.x.
ONE_NAMESPACE = ("Every version, 4.x as much as 3.x, writes namespace event-logging:3: the namespace doesn't limit "
                 "the choice to 3.x.")


async def _ask_version(ctx: Context, versions: list[str]) -> str | dict[str, Any]:
    """The version the user picks in a form, the form itself (a modern connection), or the question for the agent
    to put to them (a client without forms)."""
    labels = {f"{v} (newest installed, recommended)" if i == 0 else v: v for i, v in enumerate(versions)}
    store = (getattr(ctx, 'lifespan_context', None) or {}).get('consent')
    picked = await store.choose(ctx, 'event_logging_version', _VERSION_QUESTION, list(labels)) if store else None
    if isinstance(picked, str):
        return labels.get(picked, picked)
    if picked is not None:
        return picked
    return {'status': 'needs_guidance', 'question': _VERSION_QUESTION, 'options': versions, 'recommended': versions[0],
            'hint': f"Ask the user which event-logging schema version the translation should write, offering these "
                    f"versions (those this Stroom holds), {versions[0]}, the newest, first and recommended. "
                    f"{ONE_NAMESPACE} Then call again with schema_version= their choice."}


async def target_version(ctx: Context, schema_version: str | None, build: str | None) -> str | dict[str, Any]:
    """The event-logging version a new translation writes: the one given, else the one the user chose for the build,
    else the one this Stroom holds if it holds only one, else the user's pick (or the question to return: a form, or
    needs_guidance). Never a version this Stroom doesn't hold."""
    versions = await event_versions(ctx)
    if schema_version:
        if schema_version not in versions:
            raise ToolError(f"This Stroom holds no event-logging schema v{schema_version}; it holds "
                            f"{', '.join(versions)}. Ask the user which to write ({versions[0]}, the newest, "
                            f"recommended), not the nearest to the one asked for, then call again with schema_version= "
                            f"their choice. {ONE_NAMESPACE}")
        _remember_version(ctx, build, schema_version)
        return schema_version
    chosen = _version_chosen(ctx, build)
    if chosen in versions:
        return chosen
    picked = versions[0] if len(versions) == 1 else await _ask_version(ctx, versions)
    if isinstance(picked, str):
        _remember_version(ctx, build, picked, 'only' if len(versions) == 1 else 'form')
    return picked


def _unknown_markdown(kept: list[Any], sampled: dict[str, dict[str, Any]], total: int) -> str:
    """The records rules keep as Unknown, for the user in the chat: per rule, how many of the sample, what they hold,
    what their values suggest instead and the reason given."""
    lines = ["**Records to translate as Unknown events**", "",
             "An Unknown event says what happened isn't known: it has no Authenticate, Create or other action.", ""]
    for r in kept:
        held = sampled[r.name]
        lines += [f"**Records no other rule matches** (rule `{r.name}`)" if not r.when else
                  f"**Records rule `{r.name}` matches**", "",
                  f"- {held['records']} of the {total} sample records",
                  "- What they hold:"]
        lines += [f"  - {part}" for part in held['sample'].split('; ') if part]
        if held.get('suggested'):
            lines.append(f"- Their values suggest: {held['suggested']}"
                         + (" (kept as Unknown though, as asked)" if held.get('against_suggestion') else ''))
        if r.allow_unknown:
            lines.append(f"- Reason given: {r.allow_unknown}")
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# How a regex or xpath travels: written once, as XPath reads it; the call's JSON doubles each backslash; the server
# writes it into the XSLT as it is. Seen: agents escaping again for XSLT and for JSON, a hundred times over.
ESCAPING = ("Regexes and xpaths are written once, as XPath reads them (a literal [ in a regex is \\[). Only the JSON "
            "of your call doubles each backslash (\\[ is \"\\\\[\" in JSON); the server puts them in the XSLT as "
            "they are, so nothing escapes them again.")


# The shape of a mapping, whole, for a reply that refuses one: seen, a mapping sent without input and a rule without
# name, after the agent guessed the structure from the description. Checked to generate by the tests.
MAPPING_EXAMPLE = {
    'input': 'xml_fragments', 'record': 'Event', 'xml_namespace': 'records:2',
    'common': [{'path': 'EventTime/TimeCreated', 'xpath': 'System/TimeCreated/@SystemTime',
                'time_format': "yyyy-MM-dd'T'HH:mm:ss.SSSX"},
               {'path': 'EventSource/System/Name', 'value': 'AppAudit'},
               {'path': 'EventSource/System/Environment', 'value': 'Prod'},
               {'path': 'EventSource/Generator', 'value': 'AppAudit'},
               {'path': 'EventSource/Device/HostName', 'field': 'Computer'}],
    'events': [{'name': 'view', 'when': [{'field': 'EventID', 'equals': '4663'}],
                'fields': [{'path': 'EventDetail/TypeId', 'field': 'EventID'},
                           {'path': 'EventDetail/View/Document/Id', 'xpath': 'EventData/Data[@Name="ObjectName"]'}]}]}


def _described(loc: tuple) -> str | None:
    """The description of the mapping key at loc (('events', 1, 'name')), for one that is missing."""
    from typing import get_args
    from pydantic import BaseModel
    model, info = TranslationMapping, None
    for part in loc:
        if isinstance(part, int):
            continue
        if model is None or part not in model.model_fields:
            return None
        info = model.model_fields[part]
        found, todo = None, [info.annotation]
        while todo and found is None:
            t = todo.pop()
            if isinstance(t, type) and issubclass(t, BaseModel):
                found = t
            todo += list(get_args(t))
        model = found
    text = info.description if info and info.description else None
    return text if not text or len(text) <= 160 else text[:150].rsplit(' ', 1)[0] + ' …'


def mapping_problems(e: ValidationError, unknown_keys: list[str]) -> list[str]:
    """A mapping's validation errors, each where it is (events[2].fields[0].path) with the value given."""
    out = []
    for x in e.errors()[:8]:
        where = ''.join(f'[{p}]' if isinstance(p, int) else (f'.{p}' if n else str(p)) for n, p in enumerate(x['loc']))
        given = x.get('input')
        shown = '' if given is None or isinstance(given, (dict, list)) else f" (given {json.dumps(given, default=str)[:80]})"
        if x['type'] == 'missing':
            # "input: Field required" said nothing of what input is
            about = _described(tuple(x['loc']))
            shown = f" ({about})" if about else ''
        out.append(f"{where or 'mapping'}: {x['msg']}{shown}")
    if len(e.errors()) > 8:
        out.append(f"and {len(e.errors()) - 8} more")
    if unknown_keys:
        out.append(f"keys that are not part of a mapping: {unknown_keys} (it has {sorted(TranslationMapping.model_fields)})")
    return out


_SENT = 'sent_mappings'


def _remember_mapping(ctx: Context, target: str, mapping: dict[str, Any] | TranslationMapping) -> None:
    """The mapping as last sent for an XSLT (its uuid, or build/name), so a fix can send only its changes (this
    replica's memory; a call that lands elsewhere falls back to the mapping saved with the XSLT)."""
    if not target:
        return
    from tools.plan import _user
    try:
        sent = ctx.lifespan_context.setdefault(_SENT, {})
        value = mapping.model_dump(exclude_none=True, exclude_defaults=True) if isinstance(mapping, TranslationMapping) else mapping
        sent[(_user(ctx), target)] = json.loads(json.dumps(value))
        while len(sent) > 500:
            sent.pop(next(iter(sent)))
    except Exception:
        pass


def _generated(mapping: TranslationMapping, schema: EventSchema, version: str) -> dict[str, Any]:
    """generate's result, compiled as Stroom compiles it: seen, an XSLT saved ok:true whose every record failed at
    stepping with "Variable kv has not been declared" (an xpath of $kv, an extract's name). One that doesn't compile
    isn't saved. Run in a worker thread, as the sample check is."""
    result = generate(mapping, schema, version)
    if result['ok']:
        from utils.saxon import compile_errors
        errors, compile_notes = compile_errors(result['xslt'])
        result['warnings'] += [f"compiling: {n}" for n in compile_notes]
        if errors:
            result['problems'] += [f"The XSLT doesn't compile, so it would fail every record: {e}. Fix the mapping "
                                   f"entry it comes from (an xpath reads only the record and the XSLT's functions: a "
                                   f"field, an extract's name included, is read with field=)" for e in errors]
            result['ok'], result['xslt'] = False, None
    return result


def _check_seconds(ctx: Context) -> int:
    try:
        seconds = gateway_from(ctx).settings.sample_check_seconds
    except Exception:
        seconds = None
    return seconds if isinstance(seconds, int) else 60


def _sample_check(mapping: TranslationMapping, sample: str | list[str], splitter: SplitterSpec | None,
                  schema: EventSchema, inferred: bool, seconds: int = 60) -> dict[str, Any]:
    """The mapping checked against the sample's records. Run in a worker thread: seen in production, a Fortigate
    sample's check (10 streams of 1 MB, 60 extract entries) held the event loop for over a minute, the health probe
    went unanswered, and the pod was restarted mid-call."""
    deadline = time.monotonic() + seconds
    records, note = sample_records(mapping, sample, splitter)
    check = check_mapping(mapping, records)
    check['problems'] += check_schema_values(mapping, records, schema)
    check['warnings'] += check_xpaths(mapping, sample, splitter)
    extraction_problems, extraction_warnings = check_extractions(mapping, sample, splitter, records, deadline)
    check['problems'] += extraction_problems
    check['warnings'] += extraction_warnings
    if inferred:
        # Read with a guess at the converter, a field the guess doesn't name may be one the build's own converter
        # does (seen: a hand-written syslog splitter's 'pwd' refused, as the inferred one has no such field).
        missing = [p for p in check['problems'] if p.startswith("field '") and ' is in none of the ' in p]
        check['problems'] = [p for p in check['problems'] if p not in missing]
        check['warnings'] += [f"{p} (the sample was read with an inferred Data Splitter: give splitter if the "
                              f"build's converter names its fields otherwise)" for p in missing]
    return {**check, **({'note': note} if note else {})}


def _fix_hint(uuid: str | None, build: str | None, name: str | None) -> str:
    return ("Fix the problems in the mapping (not the XSLT) and call again with changes= only the rules or entries that "
            "change" + (f" and uuid='{uuid}'" if uuid else " and the same build and name" if build and name else
                        ", or the whole mapping") + ".")


UNKNOWN_AGREED = 'unknown agreed'


def _unknown_key(rule: Any) -> str:
    """What the user agreed to in keeping a rule as Unknown: its conditions and the reason given."""
    return json.dumps({'when': [c.model_dump(exclude_none=True) for c in rule.when], 'reason': rule.allow_unknown,
                       'keep': rule.keep_unknown}, sort_keys=True)


async def _unknown_agreed(ctx: Context, uuid: str | None, build: str | None, name: str | None) -> dict[str, str]:
    """The rules the user agreed to keep as Unknown in this XSLT (uuid, or the build's XSLT of that name), kept with
    it: {rule name: what they agreed to}."""
    from utils.mappingstore import read_block
    try:
        if not uuid and build and name:
            from security.guard import guard_from
            uuid = next((d['uuid'] for d in await guard_from(ctx).folder_contents(build)
                         if d['type'] == 'XSLT' and d['name'] == name), None)
        if not uuid:
            return {}
        found = read_block((await gateway_from(ctx).get_doc('XSLT', uuid)).get('description'), UNKNOWN_AGREED)
    except Exception:   # nothing known to have been agreed: the user is asked
        return {}
    return found if isinstance(found, dict) else {}


async def _remember_unknown_agreed(ctx: Context, uuid: str, agreed: dict[str, str]) -> str | None:
    """Keep the rules agreed as Unknown with the saved XSLT (only those it still keeps); its new version."""
    from utils.mappingstore import with_block
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('XSLT', uuid)
    doc['description'] = with_block(doc.get('description'), UNKNOWN_AGREED, agreed)
    return (await stroom.put_doc(doc)).get('version')


_DRAFT = 'draft:'


def _sent(ctx: Context, target: str) -> dict[str, Any] | None:
    from tools.plan import _user
    try:
        return ctx.lifespan_context.get(_SENT, {}).get((_user(ctx), target)) if target else None
    except Exception:
        return None


async def _mapping_to_change(ctx: Context, target: str, uuid: str | None, build: str | None = None,
                             name: str | None = None) -> tuple[dict[str, Any], str | None]:
    """(mapping, base): the mapping changes= apply to, the one last sent for the XSLT, else the one kept with it
    (uuid, or the build's XSLT of that name), else the draft draft_translation_mapping made for the build this session;
    base names the draft when it was that. Seen in testing: a first save from a 50 KB draft had to send it whole,
    which the agent couldn't write out in one call."""
    sent = _sent(ctx, target)
    if sent:
        return sent, None
    if not uuid and build and name:
        from security.guard import guard_from
        try:
            uuid = next((d['uuid'] for d in await guard_from(ctx).folder_contents(build)
                         if d['type'] == 'XSLT' and d['name'] == name), None)
        except Exception:     # no such build yet: nothing saved in it
            uuid = None
        if not uuid:
            draft = _sent(ctx, _DRAFT + build) or _sent(ctx, _DRAFT)
            if draft:
                return draft, 'the draft draft_translation_mapping made'
    if uuid:
        doc = await gateway_from(ctx).get_doc('XSLT', uuid)
        kept = read_mapping(doc.get('description'))
        if kept and kept[0] == 'translation' and (kept[1] or {}).get('mapping'):
            return kept[1]['mapping'], None
        from tools.translation import lost_mapping
        raise ToolError(await lost_mapping(ctx, {'type': 'XSLT', 'uuid': uuid, 'name': doc.get('name')},
                                           doc.get('description'))
                        or f"XSLT {uuid} keeps no translation mapping to change: give the whole mapping")
    raise ToolError("changes needs a mapping to apply to: uuid= the saved XSLT, the same build and name as a mapping "
                    "sent or saved before, or a draft from draft_translation_mapping this session (build= the same "
                    "build). None was found for those: draft one, or give the whole mapping.")


async def _kept_mapping(ctx: Context, target: str, uuid: str | None) -> tuple[dict[str, Any], str | None]:
    """The mapping to regenerate from, and the schema version it was saved for: the one kept with the XSLT (uuid),
    else the one last sent for the build and name."""
    if uuid:
        doc = await gateway_from(ctx).get_doc('XSLT', uuid)
        kept = read_mapping(doc.get('description'))
        if kept and kept[0] == 'translation' and (kept[1] or {}).get('mapping'):
            return kept[1]['mapping'], kept[1].get('schema_version')
        from tools.translation import lost_mapping
        raise ToolError(await lost_mapping(ctx, {'type': 'XSLT', 'uuid': uuid, 'name': doc.get('name')},
                                           doc.get('description'))
                        or f"XSLT {uuid} keeps no translation mapping to regenerate from: give the whole mapping "
                           f"(draft_translation_mapping drafts one)")
    return (await _mapping_to_change(ctx, target, None))[0], None


async def _required_for(ctx: Context, stage: str, build: str | None, feeds: Any = ()) -> list[dict[str, str]]:
    """The imports standing instructions require of this stage's XSLTs, for the build and its feeds."""
    from tools.instructions import imports_required
    from security.guard import guard_from
    folders = []
    if build:
        try:
            folder = await guard_from(ctx).build_folder(build, create=False)
            folders = [folder['_path']] if folder else []
        except Exception:
            folders = []
    feeds = [feeds] if isinstance(feeds, str) else list(feeds or [])
    try:
        return await imports_required(ctx, stage, folders=folders, feeds=feeds)
    except Exception:
        return []


async def _import_notes(ctx: Context, required: list[dict[str, str]], functions: bool = False) -> list[dict[str, Any]]:
    """Each required import, with what it offers: its named templates (called with shared entries where they write)
    and template rules (which apply without a call); with functions (a translation, whose xpaths can call them), its
    xsl:functions and the functions entry that binds their prefix. A required import may define functions an agent
    told only of its templates never calls (e.g. geo:lookup-ip() in GeoIP-V1)."""
    from utils.sharedxslt import describe
    from utils.stroom import gateway_from
    stroom = gateway_from(ctx)
    out = []
    for r in required:
        note: dict[str, Any] = {'import': r['href'], 'required_by': f"the AGENTS doc in {r['from'] or '(root)'}"}
        try:
            found = (await stroom.find_documents(r['href'], ['XSLT'], 10)).get('values') or []
            ref = next((v['docRef'] for v in found if v['docRef'].get('name') == r['href']), None)
            if ref is None:
                note['problem'] = f"no XSLT named '{r['href']}' in Stroom: the import will fail until it exists"
            else:
                described = describe((await stroom.get_doc('XSLT', ref['uuid'])).get('data') or '')
                note['named_templates'] = {name: t.get('writes') for name, t in (described.get('templates') or {}).items()}
                note['template_rules'] = len(described.get('rules') or [])
                offered = described.get('functions') or {}
                if functions and offered:
                    note['functions'] = {name: {k: v for k, v in f.items() if k in ('params', 'returns')}
                                         for name, f in offered.items()}
                    note['functions_entries'] = [{'href': r['href'], 'prefix': prefix, 'namespace': namespace}
                                                 for prefix, namespace in dict.fromkeys(
                                                     (f['prefix'], f['namespace']) for f in offered.values()
                                                     if f.get('prefix') and f.get('namespace'))]
        except Exception:
            pass
        out.append(note)
    return out


def zone_not_kept(mapping: TranslationMapping) -> list[str]:
    """A time read in a named zone (timezone 'Australia/Sydney') with that zone nowhere in the event: the schema has
    a place for it (asked for by the user). From the mapping's own setting, not field names."""
    entries = list(mapping.common) + [e for rule in mapping.events for e in rule.fields]
    zones = sorted({e.timezone for e in entries if e.path.endswith('TimeCreated') and e.timezone and '/' in e.timezone})
    if not zones or any(e.path.endswith('TimeZoneName') for e in entries):
        return []
    return [f"times are read in {', '.join(zones)}, which the event doesn't keep: map EventSource/Device/Location/"
            f"TimeZoneName (value '{zones[0]}') so the event says which zone its source writes in"]


META_CALL = re.compile(r"stroom:meta\(\s*'([^']+)'\s*\)")


async def _meta_not_sent(ctx: Context, mapping: TranslationMapping, stream_ids: list[int]) -> list[str]:
    """Stream meta the mapping reads (stroom:meta('Name')) that no sample stream carries: the element would stay empty.
    Compared with the streams' own attributes, whatever the header is called."""
    from tools.streams import get_stream_attributes
    entries = list(mapping.common) + [e for rule in mapping.events for e in rule.fields]
    xpaths = [e.xpath for e in entries if e.xpath] + [x.xpath for x in mapping.extract if x.xpath]
    wanted = sorted({name for xpath in xpaths for name in META_CALL.findall(xpath)})
    if not wanted:
        return []
    carried: set[str] = set()
    for stream_id in list(stream_ids)[:3]:
        try:
            got = await get_stream_attributes(ctx, int(stream_id))
        except Exception:
            return []           # attributes unreadable: nothing to say either way
        carried |= {k.lower() for k in list((got.get('attributes') or {})) + list((got.get('stream') or {}))}
    missing = [n for n in wanted if n.lower() not in carried]
    return [f"stroom:meta('{n}') is not among the sample streams' attributes ({', '.join(sorted(carried)) or 'none'}): "
            f"the element stays empty unless the source sends it as a header" for n in missing]


async def build_translation_xslt(
        ctx: Context,
        mapping: Annotated[TranslationMapping | dict[str, Any] | list[Any] | str | None, Field(
            description="The translation mapping: {input, common: [{path, field|value|...}], events: [{name, when, fields}]}. "
                        "Start from draft_translation_mapping and edit it. A field inventory is not a mapping. To fix "
                        "one already sent, give changes instead. Leave both out, with uuid=, to regenerate a saved "
                        "XSLT from the mapping kept with it (as the generator now writes it: its current style "
                        "defaults); never send a kept mapping back whole.")] = None,
        schema_version: Annotated[str | None, Field(
            description="Event-logging version the translation writes, e.g. '4.0.2': the one the user chose (as "
                        "draft_translation_mapping returned it). Left out: a saved XSLT's own, else the user is asked "
                        "which of the versions this Stroom holds (the newest recommended).")] = None,
        feeds: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Feeds the translation is for, so the standing "
                                                      "instructions for their folders are included.")] = [],
        pipeline_uuid: Annotated[str | None, Field(
            description="Only with field_mapping=true: the pipeline to step the generated XSLT through (nothing is "
                        "saved). Without field_mapping it is not stepped.")] = None,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="The sample streams: the mapping is checked against them (when no sample text is given, so "
                        "the text need not be sent again), and with pipeline_uuid stepped for field_mapping.")] = [],
        max_records: Annotated[int, Field(ge=1, le=1000, description="Records to step for field_mapping.")] = 50,
        field_mapping: Annotated[bool, Field(
            description="Step pipeline_uuid over stream_ids for a preview of the Field mapping section (the values "
                        "the events get). Slow: a Stroom step per record. Once at most, when the mapping is settled; "
                        "never while fixing it. write_documentation makes the section itself.")] = False,
        sample: Annotated[str | list[str] | None, Field(
            description="The raw sample, or a list of sample files, to check the mapping against before stepping: "
                        "fields no record has, and time formats the values do not fit. For data_splitter input "
                        "give splitter too.")] = None,
        splitter: Annotated[SplitterSpec | None, Field(
            description="The Data Splitter spec (build_data_splitter) that parses the sample, for the check.")] = None,
        build: Annotated[str | None, Field(
            description="Save the XSLT in this build, with the mapping kept with it, and return the document instead "
                        "of the code: the XSLT never has to pass through you. With name for a new XSLT.")] = None,
        name: Annotated[str | None, Field(description="With build: the new XSLT's name, following the environment's naming.")] = None,
        uuid: Annotated[str | None, Field(
            description="Replace an XSLT this server created (the one an earlier call saved) with the code "
                        "generated now; build is then not needed.")] = None,
        include_xslt: Annotated[bool, Field(description="When saving, also return the code (to read it).")] = False,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply (rules kept "
                                                                 "as Unknown).")] = None,
        reviewed: Annotated[str | bool | None, Field(description=(
            "The review_id of a needs_review reply, once its show_user (the records to translate as Unknown) is shown "
            "to the user in the chat: the short form to agree to it then opens. Not true: what changes gets a new "
            "id."))] = None,
        change: Annotated[str | None, Field(description=(
            "What this change is and why, in a line: recorded in the XSLT's version history (and the documentation's "
            "version control when it is written), e.g. 'Rule for Secret Checkout events, seen once the whole feed was "
            "processed'."))] = None,
        changes: Annotated[dict[str, Any] | str | None, Field(description=(
            "Instead of mapping, to fix one: only what changes, merged into the mapping last sent for this XSLT (the "
            "same uuid, or build and name; else the one saved with it; else, for a build and name not saved yet, the "
            "draft draft_translation_mapping made this session). events: rules, each replacing the rule of "
            "that name whole (or added); common: entries, each replacing the one with that path (and data_name); "
            "{name or path, \"remove\": true} removes one; any other key replaces it. Much less to write than the "
            "whole mapping again."))] = None,
        discard_hand_edit: Annotated[bool, Field(description=(
            "Save over an edit made by hand in Stroom's editor since the server saved the XSLT, though the new code "
            "undoes it. Only when the user has said to drop their edit: a save that would undo one is refused, saying "
            "what it changed, so it can be carried into the mapping (rebuild_mapping) instead."))] = False,
        hand_edit_choices: Annotated[dict[str, Literal['keep', 'overwrite', 'per_field']] | None, Field(description=(
            "Only from a needs_guidance reply about a hand edit, with the user's answers: {'*': 'overwrite'} to "
            "overwrite the XSLT with the change (every hand edit dropped), or for each field it names 'keep' (their "
            "hand edit) or 'overwrite' (with the proposed field). Never chosen for them."))] = None,
) -> dict[str, Any]:
    """
    Write the event-logging translation XSLT from a field mapping instead of by hand. Give the input kind
    (data_splitter, json or xml), fields every event shares (time, System, Device...), and one rule per
    kind of event with its conditions and fields. Text fields that hold several values (a message string with
    a time, user and action) are parsed by the mapping's extract list, not a function: entries {field, regex,
    names}, whose regex groups become fields named by names, used then like any input field (by field=, never
    $name in an xpath); no substring-before/after chains. JSON held in a string is read with an xpath using
    json-to-xml(). An xpath reads from the record (.//x, not //x: processing reads a stream's records as one
    document). Paths are checked against the schema: unknown paths come back with suggestions, constants are
    checked against allowed values, and elements are written in schema order with empty inputs left out; the XSLT
    is compiled, and one that doesn't compile is not saved. Fix any problems and call
    again with changes= only the rules or entries that change, not the whole mapping. With build and name (or uuid, to replace the XSLT saved before) the XSLT is saved
    with its mapping and the document comes back instead of the code, so step_sample steps it as saved; without
    them nothing is saved and the code comes back. The standing instructions (AGENTS docs) that apply
    come back with the result: check the mapping follows them, and set mapping.style from any XSLT style
    section in them (naming, variables, xsl:maps). With pipeline_uuid and stream_ids it also returns
    field_mapping, the Field mapping section of the pipeline's documentation, to read before writing it; the
    saved doc's section is generated by write_documentation itself from the mapping kept with the XSLT,
    stepped over the sample streams.
    """
    version = None
    target = uuid or (f"{build}/{name}" if build and name else '')
    applied = based_on = None
    if changes is not None:
        if mapping is not None:
            raise ToolError("Give mapping (the whole one) or changes (only what changes), not both")
        if isinstance(changes, str):
            try:
                changes = json.loads(changes)
            except json.JSONDecodeError as e:
                raise ToolError(f"changes is text that is not JSON ({e.msg} at character {e.pos}): pass it as an "
                                f"object. " + ESCAPING)
        base, based_on = await _mapping_to_change(ctx, target, uuid, build, name)
        mapping, applied = apply_changes(base, changes)
    elif mapping is None:
        if not target:
            raise ToolError("Give mapping (draft_translation_mapping drafts one), changes to fix the one sent before, "
                            "or uuid= a saved XSLT alone to regenerate it from the mapping kept with it")
        # Regenerated from the mapping kept with the XSLT, as the generator writes it now (seen: an agent asked to
        # restyle an XSLT sent its 1,400-line kept mapping back whole, after a first attempt written from memory).
        mapping, kept_version = await _kept_mapping(ctx, target, uuid)
        version = kept_version
        change = change or 'Regenerated from its mapping'
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except json.JSONDecodeError as e:
            around = mapping[max(0, e.pos - 30):e.pos + 30]
            raise ToolError(f"mapping is text that is not JSON ({e.msg} at character {e.pos}, around `{around}`): pass "
                            f"the mapping as an object, not a string. " + ESCAPING)
    if isinstance(mapping, (dict, TranslationMapping)):
        _remember_mapping(ctx, target, mapping)
    unknown_keys = sorted(set(mapping) - set(TranslationMapping.model_fields)) if isinstance(mapping, dict) else []
    if isinstance(splitter, dict):
        splitter = SplitterSpec.model_validate(splitter)     # as build_data_splitter gave it (its spec)
    if not isinstance(mapping, TranslationMapping):
        try:
            mapping = TranslationMapping.model_validate(mapping)
        except ValidationError as e:
            problems = mapping_problems(e, unknown_keys)
            looks_like_inventory = isinstance(mapping, list) and mapping and all(isinstance(x, dict) and 'field' in x for x in mapping)
            # The draft is made only for a list of fields given as the mapping (small models do), not for every
            # mistake: drafting a large sample took long, to answer what a precise error answers at once.
            if sample is not None and looks_like_inventory:
                draft = await asyncio.to_thread(draft_mapping, sample if isinstance(sample, list) else [sample])
                return {'status': 'needs_mapping', 'ok': False,
                        'problems': ["mapping is a list of fields, not a translation mapping" if looks_like_inventory else
                                     "mapping is not a translation mapping"] + problems,
                        'draft_mapping': draft['mapping'], 'splitter': draft['splitter'], 'notes': draft['notes'],
                        'hint': "Edit draft_mapping (the notes say what to decide) and call build_translation_xslt again "
                                "with it as mapping, the same sample, and the splitter if there is one."}
            # The whole shape, where the mapping's shape is what's wrong (a key missing or misplaced), not a value.
            structural = any(x['type'] in ('missing', 'model_type', 'model_attributes_type', 'list_type', 'dict_type')
                             or 'holds more than a path' in x['msg'] for x in e.errors())
            raise ToolError("mapping is not a translation mapping: " + '; '.join(problems) + ". Fix these in the "
                            "mapping you have; to start again, draft_translation_mapping (stream_ids=the sample streams) "
                            "gives one." + (" A whole mapping looks like: " + json.dumps(MAPPING_EXAMPLE) if structural else '')) from e
    if not version and uuid and not schema_version:
        # A saved XSLT keeps the version it writes; one that names none (made empty) is asked about like a new one.
        saved = await gateway_from(ctx).get_doc('XSLT', uuid)
        kept = read_mapping(saved.get('description'))
        version = ((kept[1] or {}).get('schema_version') if kept and kept[0] == 'translation' else None) \
            or event_logging_version(saved.get('data'))
    if schema_version or not version:
        chosen = await target_version(ctx, schema_version, build)
        if not isinstance(chosen, str):
            return chosen       # the question: a form, or needs_guidance for the agent to ask
        version = chosen
    schema = await event_schema(ctx, version)
    required = await _required_for(ctx, 'translation', build, feeds)
    missing = [r['href'] for r in required if r['href'] not in
               {*mapping.imports, *(u.href for u in mapping.shared), *(f.href for f in mapping.functions)}]
    if missing:
        mapping = mapping.model_copy(update={'imports': [*mapping.imports, *missing]})
    result = await asyncio.to_thread(_generated, mapping, schema, version)
    result['schema_version'] = version
    if schema_version is None and (settled := version_settled(ctx, build, version)):
        result['schema_version_settled'] = settled
    if required:
        result['required_imports'] = await _import_notes(ctx, required, functions=True)
    if unknown_keys:
        result['warnings'].append(f"mapping keys ignored, as a mapping has no such key: {unknown_keys} (it has "
                                  f"{sorted(TranslationMapping.model_fields)})")
    if result['problems']:
        # Fast: what the schema says is wrong comes back before the sample is read and checked (seen: minutes a call
        # on a large sample, for a mapping that could not have generated anyway).
        result.update(warnings=grouped(result.get('warnings') or []), sample_check='not run: fix the problems first',
                      hint=_fix_hint(uuid, build, name) + " The sample is checked once they are fixed.")
        if applied:
            result['changes_applied'] = applied
        if based_on:
            result['changes_applied_to'] = based_on
        return result
    if sample is None and stream_ids:
        texts, read_notes = await read_sample_streams(ctx, stream_ids)
        sample = list(texts.values())
        if read_notes:
            result['warnings'] += [f"sample: {n}" for n in read_notes]
    elif sample is not None:
        for text in (sample if isinstance(sample, list) else [sample]):
            check_sample(text)
    if stream_ids:
        result['warnings'] += await _meta_not_sent(ctx, mapping, stream_ids)
    result['warnings'] += zone_not_kept(mapping)
    inferred = False
    if sample and mapping.input == 'data_splitter' and splitter is None:
        # Without the spec no record could be read, so nothing was checked: the rule for the rest went to the user
        # as "no sample records checked", logoffs and all. The spec is inferred, as build_data_splitter does.
        from utils.dsgen import infer_spec
        splitter, _ = infer_spec(sample[0] if isinstance(sample, list) else sample)
        inferred = splitter is not None
        if splitter is not None:
            result['warnings'].append("sample: read with the Data Splitter spec inferred from it (as build_data_splitter "
                                      "does); give splitter if the build's converter differs")
    if sample is not None:
        check = await asyncio.to_thread(_sample_check, mapping, sample, splitter, schema, inferred, _check_seconds(ctx))
        result['sample_check'] = check
        result['warnings'] += [f"sample: {w}" for w in check['warnings']]
        if check['problems']:
            # A time format no sample value fits fails every record at stepping: as good as a schema problem.
            result['problems'] += [f"sample: {p}" for p in check['problems']]
            result['ok'], result['xslt'] = False, None
    if build:
        # The user's documentation, where the build keeps notes from it: a rule that contradicts the catalogue is reported.
        from utils.sourcenotes import check_mapping as check_against_notes, merged, notes_in_build
        catalogue = merged(await notes_in_build(ctx, build))
        if catalogue['events']:
            contradictions = check_against_notes(mapping.model_dump(exclude_none=True), catalogue)
            result['source_notes_check'] = contradictions or 'the mapping agrees with the event catalogue'
            result['warnings'] += [f"source documentation: {c}" for c in contradictions]
    if mapping.input == 'json':
        # As create_pipeline takes them (set_properties): seen, a dict passed on as is, to a parameter of another name.
        result['set_properties'] = [{'element': 'jsonParser', 'name': 'addRootObject',
                                     'value': mapping.json_layout == 'lines'}]
        result['set_properties_note'] = (
            "For create_pipeline's set_properties (the element id as the template names its JSON parser). " + ("JSON lines need the parser's root map (addRootObject true, the default), which the XSLT "
                     "matches as /map/map." if mapping.json_layout == 'lines' else
                     "A JSON array is matched with or without the parser's root map; set addRootObject false on "
                     "create_pipeline to keep the output simple, as sibling pipelines do.") + " No text converter.")
    if result['ok'] and field_mapping and pipeline_uuid and stream_ids:
        stroom = gateway_from(ctx)
        pipeline = await _Pipeline.load(stroom, pipeline_uuid)
        element = pipeline.default_outputs()[0]
        # The documentation run steps a copy that marks each Event with its rule; the returned XSLT has no marks.
        marked = (await asyncio.to_thread(generate, mapping, schema, version, True))['xslt']
        outputs = await _outputs(stroom, pipeline, stream_ids, element, {element: marked}, max_records)
        events = sampled_events(list(outputs.values()))
        result['field_mapping'] = field_mapping_markdown(mapping, schema, events)
        result['field_mapping_sample'] = {'records': len(outputs), 'events': len(events), 'element': element}
        if not events:
            result['field_mapping_sample']['warning'] = ("The sample produced no events, so the event types table "
                                                         "has no values: check stream_ids are the pipeline's input.")
    elif result['ok']:
        # A table from the mapping alone would show how values are computed, not what they are; it was being
        # copied into documentation as it was. Only a sampled run gives one.
        result['field_mapping'] = None
        result['field_mapping_needs'] = ("Not needed while fixing the mapping: write_documentation makes the section. "
                                         "For a preview once the mapping is settled: field_mapping=true with "
                                         "pipeline_uuid and stream_ids (it steps the sample: slow).")
    saved = None
    agreed_now: dict[str, str] = {}
    if result['ok'] and (build or uuid):
        kept = kept_unknown(mapping)
        # Once per rule: what the user agreed to for this XSLT is kept with it, and only a rule not agreed yet, or whose
        # conditions or reason changed since, is put to them (seen in production: asked again on every save).
        agreed_before = await _unknown_agreed(ctx, uuid, build, name) if kept else {}
        agreed_now = {r.name: _unknown_key(r) for r in kept if agreed_before.get(r.name) == _unknown_key(r)}
        kept = [r for r in kept if r.name not in agreed_now]
        if agreed_now:
            # Not asked again, but on the record: the XSLT keeps these as Unknown on the user's earlier answer.
            from security.audit import audit
            audit('confirmation', action='build_translation_xslt', outcome='granted', via='earlier',
                  details={f"rule '{n}'": {'kept as Unknown': json.loads(k)} for n, k in agreed_now.items()})
        if kept:
            # Unknown says what happened is not known: the user agrees to that per rule, seeing what its records hold
            # (a form when the client has them, so the model cannot agree for them).
            check = result.get('sample_check') or {}
            if not check.get('records'):
                # The user is asked what they agree to: which records, with which values. Without the sample the form
                # said "no sample records checked", and logoffs went to Unknown unseen.
                raise ToolError("Keeping a rule as Unknown is put to the user with the sample records it catches: call "
                                "again with stream_ids (the sample streams) or sample, so the check can show them"
                                + (f" ({check['note']})" if check.get('note') else ''))
            sampled = {k['rule']: k for k in check.get('kept_unknown') or []}
            # A rule keeping none of the sample Unknown (a catch-all for records the sample lacks) holds nothing to
            # agree to: seen, the user asked to keep 'other' as Unknown for "none of the 1000" records.
            kept = [r for r in kept if r.name in sampled]
        if kept:
            # Shown in the chat first, the form kept short (asked for by the user: "Keep rest as Unknown (what happened
            # is not known) in the saved XSLT: rest: ..." was cryptic, and the detail too much tiny text in a form).
            shown = _unknown_markdown(kept, sampled, check['records'])
            if not was_reviewed(reviewed, shown):
                # Passed on only with this id, as promote_build's layout (seen there: reviewed=true from the start).
                return {'status': 'needs_review', 'show_user': shown, 'review_id': review_id(shown),
                        'hint': "Show the user show_user in the chat exactly as it is (markdown), then, in the same "
                                "turn, without waiting for a reply, call build_translation_xslt again with the same "
                                f"arguments plus reviewed='{review_id(shown)}' (its review_id): a short form asks them "
                                "to agree, or they say what to change (a rule's action instead of Unknown)."
                                + review_note(reviewed)}
            short = {f"rule '{r.name}'": f"{sampled[r.name]['records']} of {check['records']} sample records"
                     for r in kept}
            short['as shown in the chat'] = review_id(shown)
            gate = await consent_from(ctx).require(ctx, 'confirmation', 'build_translation_xslt',
                                                   "Translate the records shown in the chat as Unknown events",
                                                   short, confirmation_id)
            if gate:
                return gate
            agreed_now.update({r.name: _unknown_key(r) for r in kept})
        from tools.translation import create_xslt, update_xslt
        said = change or ('Mapping changes: ' + '; '.join(applied) if applied else
                          'Saved again from its mapping' if uuid else 'Created from its mapping')
        if uuid:
            saved = await update_xslt(ctx, uuid, result['xslt'], mapping=mapping, change=said,
                                      discard_hand_edit=discard_hand_edit, hand_edit_choices=hand_edit_choices)
            if not isinstance(saved, dict) or not saved.get('uuid'):
                return saved        # the user is asked about fields edited by hand: nothing saved yet
        elif name:
            saved = await create_xslt(ctx, build, name, result['xslt'], mapping=mapping, change=said)
        else:
            raise ToolError("To save the XSLT give name (a new XSLT in the build) or uuid (the one saved before)")
        if agreed_now:
            saved = {**saved, 'version': await _remember_unknown_agreed(ctx, saved['uuid'], agreed_now)}
        result['saved'] = {k: saved[k] for k in ('type', 'uuid', 'name', 'version')}
        if saved.get('next'):
            result['next'] = saved['next']      # the plan's next step, as the save gave it
        _remember_mapping(ctx, saved['uuid'], mapping)
        result.update({k: saved[k] for k in ('next', 'done') if k in saved})
        if not include_xslt:
            result.pop('xslt')
    if applied:
        result['changes_applied'] = applied
    if based_on:
        result['changes_applied_to'] = based_on
    if not result['ok']:
        result['hint'] = _fix_hint(uuid, build, name)
    elif saved:
        if uuid:
            # A change to an events pipeline reaches the pipelines that read its Events (asked for by the user).
            result['follow_on'] = ("This changes an events pipeline: once it has written Events with the change (step, or "
                                   "reprocess_streams in a build), review_coverage pipeline_uuid=<its pipeline> "
                                   "events_stream_ids=<those Events> reviews the pipelines that read its Events "
                                   "(indexing, CEF output): what each lacks, their documentation, and for Elasticsearch "
                                   "what the cluster's admin must do before streams are indexed again.")
        result['hint'] = (f"Saved with its mapping as '{saved['name']}' ({saved['uuid']}). If no pipeline uses it yet, "
                          f"create_pipeline (it takes the build's XSLT); then step_sample over every sample stream. "
                          f"To change it, call again with uuid='{saved['uuid']}' and changes= only the rules or "
                          f"entries that change.")
    else:
        result['hint'] = ("Save it by calling again with build and name (or uuid to replace the one saved before): it "
                          "is kept with this mapping, so write_documentation can regenerate the Field mapping section "
                          "from it and later changes start from the mapping, and the XSLT need not pass through you. "
                          "Then step_sample. To try code without saving it, step_sample takes draft_code.")
    instructions = await applicable_instructions(ctx, feeds=feeds)
    if instructions['instructions']:
        result['standing_instructions'] = instructions['instructions']
        result['hint'] += " Check the mapping against standing_instructions (the most specific last)."
    result['warnings'] = grouped(result.get('warnings') or [])
    return result


async def build_data_splitter(
        ctx: Context,
        sample: Annotated[str | None, Field(description="The raw sample text (every line of a file). The spec is "
                                                        "inferred from it when none is given, and run on it, so the "
                                                        "records and field names are seen before anything is created.")] = None,
        spec: Annotated[dict[str, Any] | None, Field(description=(
            "Only when the inferred spec is not right, or for free text: how a record divides into fields. "
            "{'kind': 'delimited', 'delimiter': ',', 'header': true} (or header: [names]); "
            "{'kind': 'key_value', 'delimiter': ' ', 'pair_separator': '=', 'quote': '\"'}; "
            "{'kind': 'regex', 'pattern': '^(\\S+) (.*)$', 'names': ['time', 'message']}; "
            "{'kind': 'syslog', 'rfc': 'rfc3164', 'body': {'kind': 'key_value'}}."))] = None,
        save_as: Annotated[str | None, Field(description="Also save the converter in the build under this document name "
                                                         "(the build this session is on, or `build`), when every line parsed.")] = None,
        build: Annotated[str | None, Field(description="With save_as: the build; defaults to the one this session is on.")] = None,
        stream_ids: SampleStreams = [],
) -> dict[str, Any]:
    """
    Write a Data Splitter (text converter) from the sample (its text, or stream_ids to read it from the sample
    streams; with several, the spec comes from the first and is run on each): its format is profiled (delimited with or without
    a header line, key=value pairs, syslog RFC 5424 or 3164 with the message parsed further) and the spec
    inferred, unless one is given; the spec is run on the sample locally and the records it produces, the lines
    that match nothing and the field names a mapping may use come back (give the same spec to
    build_translation_xslt as splitter). JSON and XML need no converter. Saves nothing: save_text_converter saves it.
    """
    others: dict[str, str] = {}
    if sample is None and stream_ids:
        texts, _ = await read_sample_streams(ctx, stream_ids)
        sample, others = next(iter(texts.values())), dict(list(texts.items())[1:])
    elif sample is not None:
        check_sample(sample)
    if sample is None and spec is None:
        raise ToolError("Give the sample (the file's text, or stream_ids): the spec is inferred from it")
    profiled: dict[str, Any] | None = None
    if spec is None:
        inferred, profiled = infer_spec(sample)
        if inferred is None:
            fmt = profiled['format']
            if fmt == 'xml fragments':
                # Fragments do take a converter: the wrapper the XMLFragmentParser puts round them. Seen: the plan's
                # converter step sent here, told "no Data Splitter", went on without one.
                return await _fragment_wrapper(ctx, profiled, save_as, build)
            if fmt in ('json array', 'json lines', 'xml'):
                raise ToolError(f"The sample is {fmt}: it needs no Data Splitter. {profiled.get('suggested_parser')}. "
                                f"Go on to build_translation_xslt (input {'json' if fmt.startswith('json') else fmt.replace(' ', '_')}).")
            raise ToolError(f"The sample's format could not be inferred ({fmt}): give spec, a regex with a name per group, e.g. "
                            f"{EXAMPLES['unknown text']}, written for lines like {profiled.get('examples', [''])[0]!r}")
        chosen = inferred
    else:
        try:
            chosen = SplitterSpec.model_validate(spec)
        except ValidationError as e:
            fmt = profile_format(sample) if sample else None
            example = EXAMPLES.get(fmt or '', EXAMPLES['delimited'])
            raise ToolError(f"spec is not a splitter spec ({'; '.join(x['msg'] for x in e.errors()[:3])}). For "
                            f"{fmt or 'this'} data it looks like {example}; or leave spec out and it is inferred from the sample.") from e
    result: dict[str, Any] = {'spec': chosen.model_dump(exclude_none=True, exclude_defaults=True),
                              'inferred': spec is None, 'converter': generate_splitter(chosen), 'converter_type': 'DATA_SPLITTER',
                              **({'format': profiled['format']} if profiled else {})}
    if sample is not None:
        run = dry_run(chosen, sample)
        for text in others.values():   # every sample stream must parse, not just the first
            more = dry_run(chosen, text)
            run = {**run, 'records': run['records'] + more['records'],
                   'unmatched_lines': run['unmatched_lines'] + more['unmatched_lines']}
        result.update({'records': len(run['records']), 'fields': _inventory(run['records']),
                       'first_records': run['records'][:5], 'unmatched_lines': run['unmatched_lines'][:10],
                       'unmatched_count': len(run['unmatched_lines'])})
        if run['unmatched_lines'] and not run['records']:
            result['hint'] = "No line matched the spec: check the pattern or delimiter against the lines shown."
        elif run['unmatched_lines']:
            result['hint'] = (f"{len(run['unmatched_lines'])} line(s) match nothing and would produce no record: widen the "
                              f"spec, or confirm with the user that they are noise.")
        else:
            result['hint'] = ("Every line parsed. Use these field names in the mapping, with this spec as splitter; "
                              "save_text_converter saves the converter (or call again with save_as=<name>).")
    if save_as:
        if result.get('unmatched_count'):
            result['not_saved'] = f"{result['unmatched_count']} line(s) match nothing: fix the spec (or confirm they are noise) before saving"
        else:
            from tools.plan import resolve_build
            from tools.translation import create_text_converter
            saved = await create_text_converter(ctx, resolve_build(ctx, build, 'build_data_splitter save_as'), save_as,
                                                'DATA_SPLITTER', result['converter'])
            result['saved'] = saved
    return result


async def _fragment_wrapper(ctx: Context, profiled: dict[str, Any], save_as: str | None,
                            build: str | None) -> dict[str, Any]:
    """build_data_splitter for XML fragments: the wrapper converter (the environment's own when it has one), saved
    with save_as, and the namespace the mapping then reads the fragments in."""
    from tools.translation import create_text_converter, with_fragment_setup
    setup = await with_fragment_setup(ctx, profiled)
    converter = setup['text_converter']
    result = {'format': 'xml fragments', 'converter_type': 'XML_FRAGMENT', 'converter': converter['code'],
              **{k: converter[k] for k in ('environment', 'other_wrappers') if k in converter},
              'records': profiled.get('records'), 'xslt_input': setup['xslt_input'],
              'hint': (f"The wrapper the XMLFragmentParser puts round the fragments: {converter['note']} In the mapping "
                       f"set input 'xml_fragments', record '{profiled['record_element']}' and xml_namespace "
                       f"'{setup['xslt_input']['namespace']}', the namespace the fragments are read in.")}
    if save_as:
        from tools.plan import resolve_build
        result['saved'] = await create_text_converter(ctx, resolve_build(ctx, build, 'build_data_splitter save_as'),
                                                      save_as, 'XML_FRAGMENT', converter['code'])
    return result


def profile_format(sample: str) -> str:
    from utils.profile import profile
    return profile(sample)['format']


async def build_reference_xslt(
        ctx: Context,
        mapping: Annotated[ReferenceMapping, Field(description="The maps a reference feed provides: name, key field "
                                                               "and value fields per map.")],
        schema_version: Annotated[str | None, Field(description="reference-data schema version; defaults to the "
                                                                "newest this Stroom holds.")] = None,
        build: Annotated[str | None, Field(description="The build it will be saved in: the shared XSLTs standing "
                                                       "instructions require of reference XSLTs there are imported.")] = None,
) -> dict[str, Any]:
    """
    Write the XSLT of a reference-data pipeline (a child of the reference-data template) from a mapping: for
    each record and map, a <reference> with the map name, key and value in reference-data:2. The events
    pipeline then names the feed as a pipeline reference and its mapping reads the map with lookup.
    """
    version = schema_version
    if not version:
        cache = ctx.lifespan_context.setdefault('schemas', SchemaCache(gateway_from(ctx)))
        ids = [s for s in await cache.system_ids() if 'reference-data' in s]
        version = ids[-1].split('-v')[-1].removesuffix('.xsd') if ids else '2.0.1'
    required = [r for r in await _required_for(ctx, 'reference', build) if r['href'] not in mapping.imports] \
        if build else []
    if required:
        mapping = mapping.model_copy(update={'imports': [*mapping.imports, *(r['href'] for r in required)]})
    result = generate_reference(mapping, version)
    result['schema_version'] = version
    if required:
        result['required_imports'] = await _import_notes(ctx, required)
    result['hint'] = ("Fix the problems and call again." if not result['ok'] else
                      "save_xslt it (give build here first, so the imports standing instructions require are in "
                      "it); create_pipeline from the reference-data template (find_pipeline_templates "
                      "stage=reference) with combinedParser.textConverter (if the feed is text) and translationFilter.xslt; "
                      "create_processor_filter on the Raw Reference stream; wait_for_processing output_type='Reference'. "
                      "Then give the events pipeline references=[{feed, loader_pipeline}].")
    return result


async def draft_translation_mapping(
        ctx: Context,
        samples: Annotated[SampleTexts | None, Field(description="Only for samples that are not files (pasted text): "
                                                                "their text, by name or as a list, to tell the format and "
                                                                "fields from: " + SAMPLE_GUIDE + ". Uploaded samples are "
                                                                "read from their streams (stream_ids).")] = None,
        source_name: Annotated[str, Field(description="The source, e.g. 'Acme door controller': names the system and generator "
                                                      "until the user confirms them.")] = '',
        system_name: Annotated[str | None, Field(description="EventSource/System/Name, if the user has said.")] = None,
        environment: Annotated[str | None, Field(description="EventSource/System/Environment, if the user has said, e.g. Prod.")] = None,
        stream_ids: SampleStreams = [],
        splitter: Annotated[SplitterSpec | None, Field(
            description="The Data Splitter spec the text is read with (build_data_splitter's spec), when you "
                        "settled on one, e.g. named columns for a file with no header: the draft then uses its "
                        "field names. Inferred from the sample when left out.")] = None,
        build: Annotated[str | None, Field(description="The build holding the source notes (record_source_notes): the draft "
                                                       "then follows the user's documentation.")] = None,
        schema_version: Annotated[str | None, Field(
            description="Event-logging version the translation writes, e.g. '4.0.2', if the user has said. Left out, "
                        "the user is asked which of the versions this Stroom holds (the newest recommended).")] = None,
) -> dict[str, Any]:
    """
    A starting translation mapping drafted from the sample, to edit rather than write from nothing: the input
    kind and layout from the profile, the obvious event-logging homes for fields by name (time with its
    pattern, host, client and server addresses and ports, user, event type, message), one rule per kind of
    event the naming field shows (Authenticate for logon and logoff kinds, Unknown with Data for the rest, to
    replace with the right action element: build_translation_xslt refuses a rule with conditions that keeps
    Unknown, unless it sets allow_unknown), every other field carried as Data, a text of key/value pairs (Key:
    value, Key: [value], key=value) read as one extract entry a key, its keys then fields like any other (the kind
    of event among them), and notes on what is left to
    decide. For text formats the Data Splitter spec comes with it. Then build_translation_xslt with the edited
    mapping, the sample and the splitter. The user chooses the event-logging version it writes (a form, or
    needs_guidance), from those this Stroom holds.
    """
    if samples is None and not stream_ids:
        raise ToolError("Give the samples' text, or stream_ids of the uploaded sample streams")
    # Asked first: the schema the draft is checked against, and nothing read before the user answers.
    version = await target_version(ctx, schema_version, build)
    if not isinstance(version, str):
        return version
    if samples is None and stream_ids:
        samples, _ = await read_sample_streams(ctx, stream_ids)
    if not samples:
        raise ToolError("Give the samples' text, or stream_ids of the uploaded sample streams")
    source_notes = None
    if build:
        from utils.sourcenotes import merged, notes_in_build
        source_notes = merged(await notes_in_build(ctx, build))
    detail_check = None
    if source_notes and source_notes.get('events'):
        from utils.sourcenotes import detail_problem
        try:
            schema = await event_schema(ctx, version)
            detail_check = lambda detail: detail_problem(schema, detail)   # noqa: E731
        except Exception:   # the notes are followed unchecked rather than not at all
            pass
    if isinstance(splitter, dict):
        splitter = SplitterSpec.model_validate(splitter)
    result = await asyncio.to_thread(draft_mapping, samples, source_name, system_name, environment, source_notes,
                                     detail_check, splitter)
    result['mapping'] = compact_pairs(compact_rules(result['mapping']))
    if result['mapping'].get('input') == 'xml_fragments':
        # Read in the namespace of the wrapper the environment uses, which need not be records:2.
        from tools.translation import with_fragment_setup
        from utils.profile import profile
        from utils.samples import as_named_samples
        setup = await with_fragment_setup(ctx, profile(next(iter(as_named_samples(samples).values()))))
        if setup.get('xslt_input'):
            result['mapping']['xml_namespace'] = setup['xslt_input']['namespace']
    try:
        schema = await event_schema(ctx, version)
        checked = await asyncio.to_thread(generate, TranslationMapping.model_validate(result['mapping']), schema, version)
        # Each problem's first sentence: build_translation_xslt gives them whole, and the draft's notes say the rest.
        result['schema_check'] = {'ok': checked['ok'],
                                  'problems': [_first_sentence(p) for p in grouped(checked['problems'])],
                                  'warnings': grouped(checked['warnings'])[:6]}
    except ToolError as e:
        result['schema_check'] = {'ok': None, 'note': str(e)}
    result['schema_version'] = version
    settled = version_settled(ctx, build, version)
    if settled:
        result['schema_version_settled'] = settled
    # Kept for this session, so the first save sends only what changes (seen in testing: a 50 KB draft, written out
    # whole in one call, never reached the server).
    _remember_mapping(ctx, _DRAFT + (build or ''), result['mapping'])
    if build:
        _remember_mapping(ctx, _DRAFT, result['mapping'])
    result['hint'] = ((f"{settled} " if settled else '') +
                      "Decide what the notes ask (the action element per kind of event, System Name and Environment, the "
                      "time zone), then build_translation_xslt(build=, name= to save it, changes= only the rules and "
                      "entries you change, schema_version=this schema_version, stream_ids=the sample streams (or "
                      "sample=the texts), splitter=this splitter): the server keeps this draft for the session, so it "
                      "needn't be sent whole. A rule's data lists the fields it carries as Data of its action element; "
                      "keep it when you change the element.")
    return result


ALL_TOOLS = [build_translation_xslt, build_data_splitter, build_reference_xslt, draft_translation_mapping]
