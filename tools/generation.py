"""Generating translation code from a mapping, so the model does not have to write XSLT by hand."""
import re
import json
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from tools.instructions import applicable_instructions
from utils.eventschema import EventSchema
from utils.params import ONE_OR_MORE
from utils.schemas import SchemaCache, event_logging_system_id
from utils.consent import consent_from
from utils.stroom import gateway_from
from tools.stepping import _outputs, _Pipeline
from tools.streams import SampleStreams, read_sample_streams
from pydantic import ValidationError

from utils.draftmap import draft_mapping
from utils.dsgen import EXAMPLES, SplitterSpec, dry_run, generate_splitter, infer_spec
from utils.samples import SampleTexts, check_sample
from utils.localcheck import check_mapping, sample_records
from utils.xpathcheck import check_extractions, check_xpaths
from utils.profile import _inventory
from utils.refgen import ReferenceMapping, generate_reference
from utils.fielddoc import field_mapping_markdown, sampled_events
from utils.xsltgen import TranslationMapping, compact_rules, generate, grouped, kept_unknown


def _first_sentence(text: str, limit: int = 220) -> str:
    first = re.split(r'(?<=\.) ', text, maxsplit=1)[0]
    return first if len(first) <= limit else first[:limit - 20].rsplit(' ', 1)[0] + ' …'


async def event_schema(ctx: Context, version: str) -> EventSchema:
    schemas = ctx.lifespan_context.setdefault('event_schemas', {})
    if version not in schemas:
        cache = ctx.lifespan_context.setdefault('schemas', SchemaCache(gateway_from(ctx)))
        schemas[version] = EventSchema.parse(await cache.source(event_logging_system_id(version)))
    return schemas[version]


_KIND = re.compile(r'(type|action|event|kind|category|operation|activity|status|result)', re.I)


def _kinds(held: str) -> str:
    """From what a rule's records hold ('device: FW; event_type: SYSTEM, ADMIN; action: START, ...'), the fields that
    say what kind of record they are, for the confirmation's one line."""
    parts = [p for p in held.split('; ') if not p.startswith('e.g. ')]
    named = [p for p in parts if _KIND.search(p.split(':', 1)[0])]
    return '; '.join(named or parts[:2])


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


async def build_translation_xslt(
        ctx: Context,
        mapping: Annotated[TranslationMapping | dict[str, Any] | list[Any] | str, Field(
            description="The translation mapping: {input, common: [{path, field|value|...}], events: [{name, when, fields}]}. "
                        "Start from draft_translation_mapping and edit it. A field inventory is not a mapping.")],
        schema_version: Annotated[str | None, Field(
            description="Event-logging version, e.g. '3.5.2'. Defaults to the configured version.")] = None,
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
) -> dict[str, Any]:
    """
    Write the event-logging translation XSLT from a field mapping instead of by hand. Give the input kind
    (data_splitter, json or xml), fields every event shares (time, System, Device...), and one rule per
    kind of event with its conditions and fields. Text fields that hold several values (a message string with
    a time, user and action) are parsed with extract: a regular expression whose groups become fields; no
    substring-before/after chains. JSON held in a string is read with an xpath using json-to-xml(). Paths are
    checked against the schema: unknown paths come back with suggestions, constants are checked against
    allowed values, and elements are written in schema order with empty inputs left out. Fix any problems in the
    mapping and call again. With build and name (or uuid, to replace the XSLT saved before) the XSLT is saved
    with its mapping and the document comes back instead of the code, so step_sample steps it as saved; without
    them nothing is saved and the code comes back. The standing instructions (AGENTS docs) that apply
    come back with the result: check the mapping follows them, and set mapping.style from any XSLT style
    section in them (naming, variables, xsl:maps). With pipeline_uuid and stream_ids it also returns
    field_mapping, the Field mapping section of the pipeline's documentation, to read before writing it; the
    saved doc's section is generated by write_documentation itself from the mapping kept with the XSLT,
    stepped over the sample streams.
    """
    version = schema_version or gateway_from(ctx).settings.event_logging_version
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except json.JSONDecodeError as e:
            around = mapping[max(0, e.pos - 30):e.pos + 30]
            raise ToolError(f"mapping is text that is not JSON ({e.msg} at character {e.pos}, around `{around}`): pass "
                            f"the mapping as an object, not a string. " + ESCAPING)
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
                draft = draft_mapping(sample if isinstance(sample, list) else [sample])
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
    schema = await event_schema(ctx, version)
    result = generate(mapping, schema, version)
    result['schema_version'] = version
    if unknown_keys:
        result['warnings'].append(f"mapping keys ignored, as a mapping has no such key: {unknown_keys} (it has "
                                  f"{sorted(TranslationMapping.model_fields)})")
    if result['problems']:
        # Fast: what the schema says is wrong comes back before the sample is read and checked (seen: minutes a call
        # on a large sample, for a mapping that could not have generated anyway).
        result.update(warnings=grouped(result.get('warnings') or []), sample_check='not run: fix the problems first',
                      hint="Fix the problems in the mapping (not the XSLT) and call again; the sample is checked "
                           "once they are fixed.")
        return result
    if sample is None and stream_ids:
        texts, read_notes = await read_sample_streams(ctx, stream_ids)
        sample = list(texts.values())
        if read_notes:
            result['warnings'] += [f"sample: {n}" for n in read_notes]
    elif sample is not None:
        for text in (sample if isinstance(sample, list) else [sample]):
            check_sample(text)
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
        records, note = sample_records(mapping, sample, splitter)
        check = check_mapping(mapping, records)
        check['warnings'] += check_xpaths(mapping, sample, splitter)
        extraction_problems, extraction_warnings = check_extractions(mapping, sample, splitter, records)
        check['problems'] += extraction_problems
        check['warnings'] += extraction_warnings
        if inferred:
            # Read with a guess at the converter, a field the guess doesn't name may be one the build's own converter
            # does (seen: a hand-written syslog splitter's 'pwd' refused, as the inferred one has no such field).
            missing = [p for p in check['problems'] if p.startswith("field '") and ' is in none of the ' in p]
            check['problems'] = [p for p in check['problems'] if p not in missing]
            check['warnings'] += [f"{p} (the sample was read with an inferred Data Splitter: give splitter if the "
                                  f"build's converter names its fields otherwise)" for p in missing]
        result['sample_check'] = {**check, **({'note': note} if note else {})}
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
        result['pipeline_properties'] = {
            'jsonParser.addRootObject': mapping.json_layout == 'lines',
            'note': ("JSON lines need the parser's root map (addRootObject true, the default), which the XSLT "
                     "matches as /map/map." if mapping.json_layout == 'lines' else
                     "A JSON array is matched with or without the parser's root map; set addRootObject false on "
                     "create_pipeline to keep the output simple, as sibling pipelines do.") + " No text converter."}
    if result['ok'] and field_mapping and pipeline_uuid and stream_ids:
        stroom = gateway_from(ctx)
        pipeline = await _Pipeline.load(stroom, pipeline_uuid)
        element = pipeline.default_outputs()[0]
        # The documentation run steps a copy that marks each Event with its rule; the returned XSLT has no marks.
        marked = generate(mapping, schema, version, mark_rules=True)['xslt']
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
    if result['ok'] and (build or uuid):
        kept = kept_unknown(mapping)
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
            details = {}
            for r in kept:
                held = sampled.get(r.name)
                details[f"rule '{r.name}'"] = {
                    'reason given': r.allow_unknown,
                    'sample records it keeps Unknown': (f"{held['records']} of {check['records']}" if held else
                                                        f"none of the {check['records']}"),
                    **({'what they hold': held['sample']} if held else {}),
                    **({'their values suggest': held['suggested']} if held and held.get('suggested') else {})}
            caught = '; '.join(f"{r.name}: {sampled[r.name]['records']} records ({_kinds(sampled[r.name]['sample'])})"
                               for r in kept if r.name in sampled)
            against = [k['rule'] for k in sampled.values() if k.get('against_suggestion')]
            gate = await consent_from(ctx).require(ctx, 'confirmation', 'build_translation_xslt',
                                                   f"Keep {', '.join(r.name for r in kept)} as Unknown (what happened is "
                                                   f"not known) in the saved XSLT" + (f": {caught}" if caught else '')
                                                   + (f". For {', '.join(against)}, the records' values show their "
                                                      f"action (see 'their values suggest')" if against else ''),
                                                   details, confirmation_id)
            if gate:
                return gate
        from tools.translation import create_xslt, update_xslt
        if uuid:
            saved = await update_xslt(ctx, uuid, result['xslt'], mapping=mapping)
        elif name:
            saved = await create_xslt(ctx, build, name, result['xslt'], mapping=mapping)
        else:
            raise ToolError("To save the XSLT give name (a new XSLT in the build) or uuid (the one saved before)")
        result['saved'] = {k: saved[k] for k in ('type', 'uuid', 'name', 'version')}
        result.update({k: saved[k] for k in ('next', 'done') if k in saved})
        if not include_xslt:
            result.pop('xslt')
    if not result['ok']:
        result['hint'] = "Fix the problems in the mapping (not the XSLT) and call again."
    elif saved:
        result['hint'] = (f"Saved with its mapping as '{saved['name']}' ({saved['uuid']}). If no pipeline uses it yet, "
                          f"create_pipeline (it takes the build's XSLT); then step_sample over every sample stream. "
                          f"To change it, edit the mapping and call again with uuid='{saved['uuid']}'.")
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
    result = generate_reference(mapping, version)
    result['schema_version'] = version
    result['hint'] = ("Fix the problems and call again." if not result['ok'] else
                      "save_xslt it; create_pipeline from the reference-data template (find_pipeline_templates "
                      "stage=reference) with combinedParser.textConverter (if the feed is text) and translationFilter.xslt; "
                      "create_processor_filter on the Raw Reference stream; wait_for_processing output_type='Reference'. "
                      "Then give the events pipeline references=[{feed, loader_pipeline}].")
    return result


async def draft_translation_mapping(
        ctx: Context,
        samples: Annotated[SampleTexts | None, Field(description="The sample files' text, by file name or as a list, "
                                                                                   "not paths: text to tell the format and fields from: the start of each file is enough (your reader may cut it: VS Code's read_file cuts a line at 2,000 characters). Never trimmed further, completed or repaired. The files themselves go to Stroom whole with upload_sample files=[their paths], never as this text.")] = None,
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
) -> dict[str, Any]:
    """
    A starting translation mapping drafted from the sample, to edit rather than write from nothing: the input
    kind and layout from the profile, the obvious event-logging homes for fields by name (time with its
    pattern, host, client and server addresses and ports, user, event type, message), one rule per kind of
    event the naming field shows (Authenticate for logon and logoff kinds, Unknown with Data for the rest, to
    replace with the right action element: build_translation_xslt refuses a rule with conditions that keeps
    Unknown, unless it sets allow_unknown), every other field carried as Data, and notes on what is left to
    decide. For text formats the Data Splitter spec comes with it. Then build_translation_xslt with the edited
    mapping, the sample and the splitter.
    """
    if samples is None and stream_ids:
        samples, _ = await read_sample_streams(ctx, stream_ids)
    if not samples:
        raise ToolError("Give the samples' text, or stream_ids of the uploaded sample streams")
    source_notes = None
    if build:
        from utils.sourcenotes import merged, notes_in_build
        source_notes = merged(await notes_in_build(ctx, build))
    version = gateway_from(ctx).settings.event_logging_version
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
    result = draft_mapping(samples, source_name, system_name, environment, source_notes, detail_check, splitter)
    result['mapping'] = compact_rules(result['mapping'])
    if result['mapping'].get('input') == 'xml_fragments':
        # Read in the namespace of the wrapper the environment uses, which need not be records:2.
        from tools.translation import with_fragment_setup
        from utils.profile import profile
        from utils.samples import as_named_samples
        setup = await with_fragment_setup(ctx, profile(next(iter(as_named_samples(samples).values()))))
        if setup.get('xslt_input'):
            result['mapping']['xml_namespace'] = setup['xslt_input']['namespace']
    try:
        checked = generate(TranslationMapping.model_validate(result['mapping']), await event_schema(ctx, version), version)
        # Each problem's first sentence: build_translation_xslt gives them whole, and the draft's notes say the rest.
        result['schema_check'] = {'ok': checked['ok'],
                                  'problems': [_first_sentence(p) for p in grouped(checked['problems'])],
                                  'warnings': grouped(checked['warnings'])[:6]}
    except ToolError as e:
        result['schema_check'] = {'ok': None, 'note': str(e)}
    result['hint'] = ("Decide what the notes ask (the action element per kind of event, System Name and Environment, the "
                      "time zone), then build_translation_xslt(mapping=this mapping, stream_ids=the sample streams (or sample=the "
                      "texts), splitter=this splitter, build and name to save it). A rule's data lists the fields it carries as Data "
                      "of its action element; keep it when you change the element.")
    return result


ALL_TOOLS = [build_translation_xslt, build_data_splitter, build_reference_xslt, draft_translation_mapping]
