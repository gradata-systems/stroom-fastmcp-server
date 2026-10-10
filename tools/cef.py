"""CEF output pipelines: Events flattened to ArcSight's Common Event Format, one line per Event, sent through Kafka
(or written as text). Drafting the mapping from sample Events, saving its XSLT, and reviewing a CEF pipeline that
exists already."""
import json
import re
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field, ValidationError

from tools.instructions import applicable_instructions
from tools.streams import SampleStreams
from utils import cef
from utils.params import ONE_OR_MORE
from utils.stroom import gateway_from

CUSTOM_QUESTION = ("May the CEF output use keys outside ArcSight's CEF dictionary (e.g. myCustomField1)? ArcSight "
                   "usually doesn't index those. Its built-in custom slots (cs1-cs6, cn1-cn3 and the rest, each with a "
                   "label) are used either way; without keys outside the dictionary, values no key or slot takes are "
                   "not sent, and the documentation lists them.")
CUSTOM_OPTIONS = ["No: only keys in ArcSight's CEF dictionary", "Yes: keys outside the dictionary may be added"]


async def _instructions(ctx: Context, folders: list[str], feeds: list[str]) -> tuple[list[str], dict[str, Any]]:
    found = await applicable_instructions(ctx, folders, feeds)
    texts = [i.get('instructions') or '' for i in found.get('instructions') or []]
    return texts, found


async def _custom_keys(ctx: Context, given: bool | None, said: dict[str, Any]) -> Any:
    """The user's answer: given, from the standing instructions, or asked now (a form; else the agent asks)."""
    if given is not None:
        return given
    if 'custom_keys' in said:
        return said['custom_keys']
    store = (getattr(ctx, 'lifespan_context', None) or {}).get('consent')
    chosen = await store.choose(ctx, 'draft_cef_mapping', CUSTOM_QUESTION, CUSTOM_OPTIONS) if store else None
    if isinstance(chosen, str):
        return chosen == CUSTOM_OPTIONS[1]
    return chosen      # a form to return, or None


async def _stepped(ctx: Context, pipeline_uuid: str, stream_ids: list[int], element: str | None,
                   cap: int) -> tuple[list[str], list[etree._Element], str, dict[str, str]]:
    """The CEF lines a pipeline writes from these Events streams, the Events they came from (in order), the element
    read, and how it sends them ({'output': kafka or text, 'topic': ...})."""
    from tools.stepping import _Pipeline, _step
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    element = element or pipeline.default_outputs()[0]
    lines: list[str] = []
    events: list[etree._Element] = []
    sent = {'output': 'text'}
    for stream_id in stream_ids:
        result = await _step(stroom, pipeline, stream_id, 'FIRST', None, None)
        while result.get('foundRecord') and len(lines) < cap:
            elements = (result.get('stepData') or {}).get('elementMap') or {}
            step = elements.get(element) or {}
            made = cef.lines_in(step.get('output') or '')
            if '<kafkaRecord' in (step.get('output') or ''):
                topic = re.search(r'<(?:\w+:)?kafkaRecord[^>]*topic="([^"]*)"', step['output'])
                sent = {'output': 'kafka', **({'topic': topic.group(1)} if topic else {})}
            try:
                given = cef.events_of(step.get('input') or '') if step.get('input') else []
            except etree.XMLSyntaxError:
                given = []
            lines += made
            events += given[:len(made)] + [None] * (len(made) - len(given[:len(made)]))
            result = await _step(stroom, pipeline, stream_id, 'FORWARD', result['foundLocation'], None)
    return lines, events, element, sent


async def _events(ctx: Context, stream_ids: list[int]) -> list[etree._Element]:
    from tools.validation import _stream_events
    xml, _ = await _stream_events(ctx, stream_ids)
    return cef.events_of(xml)


def _apply(plan: cef.CefPlan, overrides: list[cef.Override]) -> tuple[cef.CefPlan, list[str]]:
    """Overrides on a plan that exists: a path moved to another key (or label), or left out."""
    data = plan.model_dump()
    done = []
    for o in overrides:
        lists = [('common', data['common'])] + [(k, v) for k, v in data['events'].items()
                                               if o.event_type in (None, k)]
        hit = False
        for kind, fields in lists:
            for f in list(fields):
                if cef._matches(o.path, f['path']):
                    hit = True
                    if o.drop:
                        fields.remove(f)
                        data['not_sent'].append({'path': f['path'], 'event_type': kind, 'why': 'left out as instructed'})
                        done.append(f"[{kind}] {f['path']} left out")
                    else:
                        f['key'] = cef.key_of(o.key or f['key'])
                        f['label'] = o.label or (f.get('label') if f['key'] in cef.SLOT_KEYS else None) or \
                            (cef.label_for(f['path']) if f['key'] in cef.SLOT_KEYS else None)
                        done.append(f"[{kind}] {f['path']} -> {f['key']}")
        if not hit and o.key:
            target = data['events'].setdefault(o.event_type, []) if o.event_type else data['common']
            target.append({'path': o.path, 'key': cef.key_of(o.key), 'label': o.label})
            data['not_sent'] = [n for n in data['not_sent'] if not cef._matches(o.path, n['path'])]
            done.append(f"[{o.event_type or 'every event'}] {o.path} -> {cef.key_of(o.key)} (added)")
    return cef.CefPlan.model_validate(data), done


async def _templates(ctx: Context, said: dict[str, Any]) -> dict[str, Any]:
    """Where the pipeline comes from: a template the standing instructions name (they decide), forwarding templates,
    and the CEF pipelines there are already, with the templates they inherit from."""
    stroom = gateway_from(ctx)
    out: dict[str, Any] = {}
    if said.get('template'):
        found = (await stroom.find_documents(said['template'], ['Pipeline'], 5)).get('values') or []
        named = [{'uuid': v['docRef']['uuid'], 'name': v['docRef']['name'], 'path': v.get('path')} for v in found
                 if v['docRef'].get('type') == 'Pipeline']
        out['from_instructions'] = {'named': said['template'], 'found': named,
                                    'note': "The standing instructions name this template: use it, whatever else is found."}
    try:
        from tools.templates import find_pipeline_templates
        found = await find_pipeline_templates(ctx, 'forwarding')
        out['forwarding_templates'] = [{k: c.get(k) for k in ('uuid', 'name', 'path', 'backend', 'children',
                                                                'child_must_supply')} for c in found.get('candidates') or []]
    except Exception as e:      # the search is advice; a broken pipeline elsewhere must not stop the draft
        out['forwarding_templates_error'] = str(e)[:200]
    # Pipelines and XSLTs in the workspace are drafts, not the environment's practice (seen: twenty leftover builds'
    # pipelines filled the list, and the production one it was looking for wasn't in it).
    from security.guard import guard_from
    from tools.builds import _path
    drafts = f"System/{guard_from(ctx).workspace}/"

    def draft(path: Any) -> bool:
        return (_path(path) + '/').startswith(drafts)
    existing = {}
    for pattern in ('*CEF*', '*ArcSight*', '*Arcsight*'):
        for v in (await stroom.find_documents(pattern, ['Pipeline'], 200)).get('values') or []:
            if v['docRef'].get('type') == 'Pipeline' and not draft(v.get('path')):
                existing[v['docRef']['uuid']] = {'uuid': v['docRef']['uuid'], 'name': v['docRef']['name'],
                                                 'path': v.get('path')}
    for entry in list(existing.values())[:20]:
        try:
            parent = (await stroom.get(f"/pipeline/v1/{entry['uuid']}")).get('parentPipeline') or {}
            if parent.get('uuid'):
                entry['inherits_from'] = {'uuid': parent['uuid'], 'name': parent.get('name')}
        except ToolError:
            pass
    if existing:
        out['existing_cef_pipelines'] = list(existing.values())[:20]
    try:
        body = await stroom.post('/explorer/v2/findInContent', {
            'filter': {'matchType': 'CONTAINS', 'pattern': 'CEF:0', 'caseSensitive': False},
            'pageRequest': {'offset': 0, 'length': 200}})
        xslts = [(v.get('docContentMatch') or {}).get('docRef') or {} for v in (body or {}).get('values') or []
                 if not draft(v.get('path'))]
        xslts = [{'uuid': r.get('uuid'), 'name': r.get('name')} for r in xslts if r.get('type') == 'XSLT']
        if xslts:
            out['xslts_writing_cef'] = xslts[:10]
    except Exception:
        pass
    return out


async def draft_cef_mapping(
        ctx: Context,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Events streams (the events pipeline's output): the sample the mapping is drafted from, or that "
                        "an existing CEF pipeline is stepped over to review it.")],
        custom_keys: Annotated[bool | None, Field(description=(
            "Whether keys outside ArcSight's CEF dictionary may be sent: the user's answer. Left out, the standing "
            "instructions decide, or the user is asked."))] = None,
        overrides: Annotated[list[cef.Override] | str, ONE_OR_MORE, Field(description=(
            "Mappings the user gives: {path, key (short or full name), label (for a custom slot), event_type (one kind "
            "of event)} or {path, drop: true}. Mappings in the standing instructions ('<Event path> -> <CEF key>') are "
            "applied first."))] = [],
        header: Annotated[dict[str, cef.CefValue | str] | str, Field(description=(
            "Header fields to set: vendor, product, version, signature (Device Event Class ID), name, severity; each a "
            "constant, or {source: <XPath on the Event>, map: {value: header value}, default}."))] = {},
        output: Annotated[Literal['kafka', 'text'] | None, Field(description=(
            "kafka (a Kafka record per Event, value the CEF line; StandardKafkaProducer) or text (lines; TextWriter). "
            "Left out: the pipeline template's kind, else kafka."))] = None,
        topic: Annotated[str | None, Field(description="kafka: the topic ArcSight reads (the user, or the standing "
                                                       "instructions, say which).")] = None,
        kafka_key: Annotated[str | None, Field(description="kafka: an XPath on the Event for the record key.")] = None,
        pipeline_uuid: Annotated[str | None, Field(description=(
            "Review an existing CEF pipeline instead of drafting: its output is stepped over stream_ids, parsed and "
            "checked against the CEF dictionary, with what each Event sends and doesn't; its plan (kept with an XSLT "
            "this server saved) or the mapping its lines imply comes back to change."))] = None,
        build: Annotated[str | None, Field(description="With name: save the XSLT in this build with its plan.")] = None,
        name: Annotated[str | None, Field(description="With build: the new XSLT's name.")] = None,
        uuid: Annotated[str | None, Field(description=(
            "An XSLT this server saved with a CEF plan: start from that plan (with the overrides applied) and save "
            "it again in place."))] = None,
        change: Annotated[str | None, Field(description=(
            "What this change is and why, in a line: recorded in the XSLT's version history (and the documentation's "
            "version control when it is written), e.g. 'Rule for Secret Checkout events, seen once the whole feed was "
            "processed'."))] = None,
        agent_model: Annotated[str | None, Field(description=(
            "The model you are, e.g. 'claude-haiku-5-5', for the version history; once given, remembered for the "
            "session."))] = None,
        feeds: Annotated[list[str] | str, ONE_OR_MORE, Field(description="The Events feed: its folder's standing "
                                                                         "instructions apply.")] = [],
        folders: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Folders the pipeline will live in.")] = [],
) -> dict[str, Any]:
    """
    CEF (ArcSight Common Event Format) output: draft which Event value goes to which CEF key, as flattened text,
    one CEF line per Event. First asks the user whether keys outside ArcSight's CEF dictionary are allowed (unless
    the standing instructions say). Infers a mapping from the sample Events: the header (vendor, product, version,
    class id, name, severity), fields every event shares, and per kind of event (its action element) the rest:
    ArcSight's standard keys first, then its custom slots (cs1-cs6, cn1-cn3, ...) with labels, and lists what no key
    takes (not sent). Mappings in the standing instructions and in overrides take precedence. Returns the plan, its
    problems, the documentation's tables, and where the pipeline should come from: a template the standing
    instructions name, else the environment's forwarding templates or the template its CEF pipelines inherit from.
    With build and name (or uuid) it saves the XSLT with the plan: a Kafka record per Event (kafka-records:1, one
    Event a record) or lines of text. With pipeline_uuid it reviews an existing CEF pipeline instead.
    """
    from utils.xsltversion import remember_model
    remember_model(ctx, agent_model)
    if isinstance(overrides, str):
        try:
            overrides = json.loads(overrides)
        except json.JSONDecodeError as e:
            raise ToolError(f"overrides is text that is not JSON ({e.msg}): pass a list of objects")
    if isinstance(header, str):
        header = json.loads(header) if header.strip() else {}
    try:
        given = [o if isinstance(o, cef.Override) else cef.Override.model_validate(o) for o in overrides or []]
        heads = {k: (v if isinstance(v, cef.CefValue) else cef.CefValue(value=v) if isinstance(v, str)
                     else cef.CefValue.model_validate(v)) for k, v in (header or {}).items()}
    except ValidationError as e:
        raise ToolError(f"overrides or header: {e.errors()[0]['msg']}")
    unknown = sorted(set(heads) - {n for n, _, _ in cef.HEADER})
    if unknown:
        raise ToolError(f"header has no {unknown}: its fields are {[n for n, _, _ in cef.HEADER]}")
    texts, standing = await _instructions(ctx, list(folders), list(feeds))
    said = cef.from_instructions(texts)
    allowed = await _custom_keys(ctx, custom_keys, said)
    if allowed is None:
        return {'status': 'needs_guidance', 'question': CUSTOM_QUESTION, 'options': CUSTOM_OPTIONS,
                'hint': "Ask the user exactly this, offering these options, then call again with custom_keys=false "
                        "(the first) or true (the second). Don't choose for them."}
    if not isinstance(allowed, bool):
        return allowed      # the form, for the client to show
    stroom = gateway_from(ctx)
    cap = stroom.settings.max_sample_records
    applied = said['overrides'] + given
    result: dict[str, Any] = {}
    if pipeline_uuid:
        from tools.builds import kept_mapping
        kept = await kept_mapping(ctx, pipeline_uuid)
        lines, events, element, sent = await _stepped(ctx, pipeline_uuid, stream_ids, kept['element'] if kept else None, cap)
        if not lines:
            raise ToolError(f"Stepping the pipeline over streams {stream_ids} gave no CEF lines at its element "
                            f"'{element}': check stream_ids are Events streams it reads, and step_sample it")
        reviewed = cef.review(lines, [e for e in events if e is not None], allowed)
        result['review'] = reviewed
        if kept and kept['kind'] == 'cef':
            plan = cef.CefPlan.model_validate(kept['payload'])
            result['plan_from'] = f"the plan kept with XSLT '{kept['xslt'].get('name')}'"
        else:
            sample = cef.parse(lines[0])['header']
            constants = {n: cef.CefValue(value=v) for (n, _, _), v in zip(cef.HEADER, sample[1:])}
            plan = cef.CefPlan(output=sent['output'], topic=sent.get('topic'), custom_keys=allowed, **{**constants, **heads},
                               events={k: [cef.CefField(**{x: m[x] for x in ('path', 'key', 'label') if x in m})
                                           for m in maps] for k, maps in reviewed['implied_mapping'].items()},
                               not_sent=[{'path': n['path'], 'event_type': n['event_type'], 'why': 'not in its lines'}
                                         for n in reviewed['not_sent']])
            result['plan_from'] = ("the mapping its lines imply (its XSLT keeps no plan; header fields as the first line "
                                   "had them): check it before saving a new XSLT from it")
        plan, done = _apply(plan, applied)
        examples = cef.examples_from(plan, lines, [e for e in events if e is not None])
    else:
        events = await _events(ctx, stream_ids)
        if uuid:
            from utils.mappingstore import read_mapping
            kept = read_mapping((await stroom.get_doc('XSLT', uuid)).get('description'))
            if not kept or kept[0] != 'cef':
                raise ToolError(f"XSLT {uuid} keeps no CEF plan: draft without uuid, or review its pipeline "
                                f"(pipeline_uuid)")
            plan, done = _apply(cef.CefPlan.model_validate(kept[1]), applied)
            if heads:
                plan = plan.model_copy(update=heads)
        else:
            choice = output
            if choice is None:
                templates = await _templates(ctx, said)
                kinds = {t.get('backend') for t in templates.get('forwarding_templates') or []}
                choice = 'text' if kinds == {'text'} else 'kafka'
                result['templates'] = templates
            plan, notes = cef.draft(events, allowed, applied, heads, choice, topic or said.get('topic'), kafka_key)
            done = [f"{o.path} -> " + (f"{cef.key_of(o.key)}" + (f" ({o.key})" if cef.key_of(o.key) != o.key else '')
                                       if o.key else 'not sent') for o in applied]
            if notes:
                result['notes'] = notes
        if topic or kafka_key or output:
            plan = plan.model_copy(update={k: v for k, v in (('topic', topic), ('kafka_key', kafka_key),
                                                             ('output', output)) if v})
        examples = {}
        if events:
            # The lines the plan writes for the sample, for the documentation's examples and a review of its own.
            try:
                from saxonche import PySaxonProcessor
                xml = etree.tostring(etree.fromstring(
                    b'<Events xmlns="event-logging:3">' + b''.join(etree.tostring(e) for e in events[:cap]) + b'</Events>'))
                with PySaxonProcessor(license=False) as proc:
                    out = proc.new_xslt30_processor().compile_stylesheet(stylesheet_text=plan.xslt()).transform_to_string(
                        xdm_node=proc.parse_xml(xml_text=xml.decode()))
                lines = cef.lines_in(out)
                examples = cef.examples_from(plan, lines, events)
                checked = cef.review(lines, events, allowed)
                if checked['problems']:
                    result['sample_check'] = checked['problems']
                result['example_line'] = lines[0] if lines else None
            except Exception as e:      # the check is advice; the plan stands on its own problems
                result['sample_check_error'] = str(e)[:300]
    if 'templates' not in result and not pipeline_uuid:
        result['templates'] = await _templates(ctx, said)
    problems = plan.problems()
    result.update({
        'custom_keys': allowed,
        **({'applied': done} if done else {}),
        'plan': plan.model_dump(exclude_defaults=True),
        'problems': problems,
        'not_sent': plan.not_sent,
        'documentation': '## Field mapping\n\n' + plan.markdown(examples),
    })
    if said['overrides'] or 'custom_keys' in said or said.get('topic') or said.get('template'):
        result['from_standing_instructions'] = {
            **({'mappings': [f"{o.path} -> {o.key}" for o in said['overrides']]} if said['overrides'] else {}),
            **{k: said[k] for k in ('custom_keys', 'topic', 'vendor', 'template') if k in said}}
    if standing.get('instructions'):
        result['standing_instructions'] = standing['instructions']
    configs = (await stroom.find_documents('*', ['KafkaConfig'], 50)).get('values') or []
    result['kafka_configs'] = [{'uuid': v['docRef']['uuid'], 'name': v['docRef']['name'], 'path': v.get('path')}
                               for v in configs if v['docRef'].get('type') == 'KafkaConfig']
    if (build and name) or uuid:
        if problems:
            result['saved'] = None
            result['hint'] = "Not saved: fix the problems (overrides, header, topic) and call again."
            return result
        from tools.translation import create_xslt, update_xslt
        target = uuid or ((await _kept_xslt(ctx, pipeline_uuid)) if pipeline_uuid else None)
        said = change or (('CEF plan changes: ' + '; '.join(done)) if target and done else
                          'Saved again from its CEF plan' if target else 'Created from its CEF plan')
        saved = await (update_xslt(ctx, target, plan.xslt(), cef_plan=plan, change=said) if target else
                       create_xslt(ctx, build, name, plan.xslt(), cef_plan=plan, change=said))
        result['saved'] = {k: saved.get(k) for k in ('type', 'uuid', 'name', 'version')}
        result['hint'] = _next(plan, saved.get('uuid'), result)
    else:
        result['hint'] = ("Show the user the plan's tables (documentation) and the not-sent list; with their changes as "
                          "overrides, call again with build and name to save the XSLT. " + _next(plan, None, result))
    return result


async def _kept_xslt(ctx: Context, pipeline_uuid: str) -> str | None:
    from tools.builds import kept_mapping
    kept = await kept_mapping(ctx, pipeline_uuid)
    return kept['xslt']['uuid'] if kept and kept['kind'] == 'cef' else None


def _next(plan: cef.CefPlan, xslt_uuid: str | None, result: dict[str, Any]) -> str:
    templates = result.get('templates') or {}
    where = ("the template the standing instructions name" if templates.get('from_instructions') else
             "a forwarding template (find_pipeline_templates stage=forwarding), or the one existing CEF pipelines "
             "inherit from" if templates.get('forwarding_templates') or templates.get('existing_cef_pipelines') else
             "no forwarding template exists: create_pipeline standalone='kafka' (the user confirms the chain)")
    xslt = f"'{xslt_uuid}'" if xslt_uuid else 'the saved XSLT'
    return (f"Pipeline: create_pipeline from {where}, setting the XSLT step's xslt to {xslt}"
            + (" and standardKafkaProducer.kafkaConfig to the KafkaConfig the user picks (kafka_configs)"
               if plan.output == 'kafka' else '') + ". Then step_sample over the Events streams (stepping sends "
            "nothing), draft_cef_mapping pipeline_uuid=<it> stream_ids=... to review what it writes, and "
            "write_documentation with the Events stream_ids: its Field mapping section is the plan's tables.")


ALL_TOOLS = [draft_cef_mapping]
