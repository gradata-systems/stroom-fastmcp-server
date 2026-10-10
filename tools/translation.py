"""Tools that create and change translation content (text converters and XSLTs) in a build."""
import re
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field

from security.guard import guard_from
from utils.consent import consent_from
from tools.validation import check_xslt
from utils.fieldplan import FieldPlan
from utils.mappingstore import normalise_xslt, with_mapping
from utils.stroom import gateway_from
from utils.xsltgen import TranslationMapping

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed, e.g. 'acme-door-v1.3'.")]
Version = Annotated[str | None, Field(
    description="The document version from the last read; the save is refused if it changed since.")]


def _pending(ctx: Context, description: str | None, previous: dict[str, Any] | None, change: str,
             code: str | None = None) -> str:
    """The description with this change pending: the build's changes become one line of the XSLT's version history
    when the build is promoted, not a line a save."""
    from tools.plan import _user
    from utils.xsltversion import agent_line, with_pending
    # The saved code's digest: a later difference from what its mapping generates is then a hand edit only when the
    # code changed since; otherwise the generator did (build_status said "edited by hand" after an upgrade).
    return with_pending(description, previous, _user(ctx), change, agent_line(ctx), code=code)


def _previewed(doc: dict[str, Any]) -> str:
    """The description with the build's changes so far previewed in its version history, marked Unreleased."""
    from utils.xsltversion import preview
    return preview(doc.get('description'), doc.get('data') or '')


def _summary(doc: dict[str, Any]) -> dict[str, Any]:
    return {'type': doc.get('type'), 'uuid': doc.get('uuid'), 'name': doc.get('name'), 'version': doc.get('version')}


def _check_converter(converter_type: str, code: str) -> None:
    """A Data Splitter is a <dataSplitter> document. JSON needs no text converter at all: the JSONParser element
    of a JSON translation template parses it, and a converter holding a <jsonParser> element parses nothing."""
    if converter_type == 'XML_FRAGMENT':
        if not (re.search(r'<!ENTITY\s+fragment\s+SYSTEM\s+["\']fragment["\']', code) and '&fragment;' in code):
            raise ToolError("Not saved: an XML_FRAGMENT converter is the wrapper the XMLFragmentParser puts round the "
                            "fragments: a DOCTYPE declaring <!ENTITY fragment SYSTEM \"fragment\"> and &fragment; "
                            "inside the root element where the fragments go. profile_sample gives one "
                            "(stroom://guide/data-splitter).")
        return
    if converter_type != 'DATA_SPLITTER':
        return
    if re.search(r'<\s*json', code, re.IGNORECASE):
        raise ToolError("Not saved: a text converter cannot parse JSON. Use a translation template whose parser is the JSONParser (find_pipeline_templates), whose "
                        "JSONParser element parses the raw JSON (JSON lines included) with no text converter; the "
                        "XSLT then reads map/string elements in namespace http://www.w3.org/2013/XSL/json "
                        "(stroom://guide/json-input).")
    try:
        root = etree.fromstring(code.encode('utf-8'))
    except etree.XMLSyntaxError as e:
        raise ToolError(f"Not saved: the Data Splitter is not well-formed XML (line {e.lineno}): {e.msg}") from e
    if etree.QName(root).localname != 'dataSplitter':
        raise ToolError(f"Not saved: a DATA_SPLITTER converter's root element is <dataSplitter xmlns=\"data-splitter:3\">, "
                        f"not <{etree.QName(root).localname}> (stroom://guide/data-splitter)")


async def fragment_wrappers(ctx: Context, limit: int = 100) -> list[dict[str, Any]]:
    """The environment's XML_FRAGMENT converters outside the workspace: name, uuid, path, code, and the root and
    namespace the fragments are read in. Seen: the server proposed its records:2 wrapper where every wrapper in the
    environment was an event-logging:3 <Events> one."""
    import asyncio
    from utils.profile import wrapper_root
    stroom = gateway_from(ctx)
    workspace = stroom.settings.workspace_folder
    found = await stroom.find_documents('*', ['TextConverter'], limit)
    refs = [(v['docRef'], v.get('path') or '') for v in found.get('values') or []
            if (v.get('docRef') or {}).get('type') == 'TextConverter' and workspace not in (v.get('path') or '')]

    async def read(ref: dict[str, Any], path: str) -> dict[str, Any] | None:
        try:
            doc = await stroom.get_doc('TextConverter', ref['uuid'])
        except ToolError:
            return None       # the find index can name a document since deleted
        if doc.get('converterType') != 'XML_FRAGMENT' or '&fragment;' not in (doc.get('data') or ''):
            return None
        root, namespace = wrapper_root(doc['data'])
        return {'name': ref.get('name'), 'uuid': ref['uuid'], 'path': path, 'code': doc['data'], 'root': root,
                'namespace': namespace}

    return [w for w in await asyncio.gather(*(read(r, p) for r, p in refs)) if w]


async def with_fragment_setup(ctx: Context, profiled: dict[str, Any]) -> dict[str, Any]:
    """A profile of XML fragments, with the wrapper this environment uses: its own (event-logging fragments: an
    event-logging:3 one, else the most used), else the standard one (an <Events> one for event-logging fragments, in
    the configured version)."""
    first = next(iter((profiled.get('files') or {}).values()), profiled)     # several files: as the first is read
    record = profiled.get('record_element') or first.get('record_element')
    if profiled.get('format') != 'xml fragments' or not record:
        return profiled
    from collections import Counter
    from utils.profile import EVENTS_FRAGMENT_WRAPPER, xml_fragment_setup
    version = gateway_from(ctx).settings.event_logging_version
    try:
        found = await fragment_wrappers(ctx)
    except Exception:     # a convenience on top of the standard wrapper: never fail a profile over it
        found = []
    events = first.get('event_logging')
    fitting = [w for w in found if w['namespace'] == 'event-logging:3'] if events else found
    if fitting:
        most = Counter((w['root'], w['namespace']) for w in fitting).most_common(1)[0][0]
        wrapper = next(w for w in fitting if (w['root'], w['namespace']) == most)
    else:
        wrapper = {'code': EVENTS_FRAGMENT_WRAPPER.format(version=version)} if events else None
    others = [w for w in found if w is not wrapper]
    return {**profiled, **xml_fragment_setup(first.get('namespace'), record, wrapper, others)}


async def _generated(index_plan: FieldPlan | None) -> str:
    """The indexing XSLT an index plan generates, for a save given no code."""
    if index_plan is None:
        raise ToolError("Give code (an XSLT written by hand), or index_plan to save the indexing XSLT generated from it. "
                        "A translation from a mapping is saved by build_translation_xslt (build and name, or uuid).")
    return index_plan.xslt()


async def _checked(ctx: Context, xslt: str, build: str | None = None) -> None:
    input_namespace = None
    if build:
        # A source's own XML in no namespace: its <records><record> are not a Data Splitter's records:2.
        from tools.plan import sample_format
        try:
            sample = await sample_format(ctx, build)
        except Exception:
            sample = None
        if sample and sample.get('format') == 'xml':
            input_namespace = sample.get('namespace') or ''
    result = await check_xslt(ctx, xslt, input_namespace=input_namespace)
    if not result['ok']:
        raise ToolError(f"XSLT not saved: {'; '.join(result['errors'])}")


async def create_text_converter(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Document name, following the environment's naming.")],
        converter_type: Annotated[Literal['DATA_SPLITTER', 'XML_FRAGMENT'], Field(
            description="DATA_SPLITTER for text (CSV, syslog, key=value); XML_FRAGMENT: the wrapper for XML "
                        "fragments (several root elements), read by an XMLFragmentParser. JSON and single-document "
                        "XML sources take no text converter: their template's parser reads them.")],
        code: Annotated[str, Field(description="The converter definition, e.g. a <dataSplitter> document.")],
) -> dict[str, Any]:
    """
    Create a text converter in the build folder (see stroom://guide/data-splitter), for templates whose parser
    needs one (DSParser.textConverter in find_pipeline_templates). Not for JSON: a JSON translation template's
    template's JSONParser parses JSON, one object per line or an array, with no converter.
    """
    _check_converter(converter_type, code)
    stroom = gateway_from(ctx)
    ref = await guard_from(ctx).create('TextConverter', name, build)
    doc = await stroom.get_doc('TextConverter', ref['uuid'])
    doc.update(converterType=converter_type, data=code)
    from tools.plan import with_next
    return await with_next(ctx, build, _summary(await stroom.put_doc(doc)))


async def update_text_converter(
        ctx: Context,
        uuid: Annotated[str, Field(description="Text converter UUID.")],
        code: Annotated[str, Field(description="The complete new converter definition.")],
        version: Version = None,
) -> dict[str, Any]:
    """Replace a text converter's code. Only converters this server created can be changed."""
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('TextConverter', uuid)
    _check_converter(doc.get('converterType') or 'DATA_SPLITTER', code)
    await guard_from(ctx).check_managed({'type': 'TextConverter', 'uuid': uuid, 'name': doc.get('name')})
    doc['data'] = code
    return _summary(await stroom.put_doc(doc, version))


Mapping = Annotated[TranslationMapping | None, Field(
    description="The mapping build_translation_xslt generated this code from. Kept with the XSLT (in its description), "
                "so write_documentation regenerates the Field mapping section from it and later changes start from the "
                "mapping; build_status reports an XSLT edited by hand since.")]
IndexPlan = Annotated[FieldPlan | None, Field(
    description="For an indexing XSLT: the field plan draft_index_mapping drafted it from, kept with the XSLT for the "
                "documentation.")]


LOST = ("{type} '{name}' was saved by stroom-mcp with the {what} it is generated from, kept in its Documentation tab "
        "(hidden, after a note asking that it be left), and it is gone: deleted, or edited so it no longer reads. Until it "
        "is back the server treats the XSLT as written by hand (no regenerating, no changes=, no generated Field mapping "
        "section), and its version history went with it. Tell the user. To recover: rebuild_mapping uuid='{uuid}' "
        "stream_ids=<the sample streams> reads the mapping back from the XSLT and proves it on the sample before "
        "saving it.")


async def lost_mapping(ctx: Context, ref: dict[str, Any], description: str | None) -> str | None:
    """Why an XSLT has no kept mapping, when it should have one: tagged as saved with one, and none reads now."""
    from security.guard import KEPT_MAPPING
    from utils.mappingstore import read_mapping
    if read_mapping(description):
        return None
    try:
        tags = await guard_from(ctx).tags(ref)
    except Exception:
        return None
    if KEPT_MAPPING not in tags:
        return None
    return LOST.format(type=ref.get('type', 'XSLT'), name=ref.get('name'), uuid=ref.get('uuid'),
                       what='mapping (or plan)')


async def _tag_kept(ctx: Context, saved: dict[str, Any], kept: bool) -> None:
    """Tag an XSLT saved with its mapping, so a mapping later deleted from its Documentation tab is noticed."""
    if not kept:
        return
    from security.guard import KEPT_MAPPING
    try:
        await guard_from(ctx).tag([{k: saved.get(k) for k in ('type', 'uuid', 'name')}], [KEPT_MAPPING])
    except Exception:   # a tag is a safeguard: the save stands without it
        pass


async def _described(ctx: Context, doc: dict[str, Any], code: str, mapping: TranslationMapping | None,
                     index_plan: FieldPlan | None, cef_plan: Any = None) -> dict[str, Any]:
    """The doc with its description carrying the mapping or plan the code came from, and whether the code is
    what the mapping generates (a hand-edited XSLT is kept, but reported)."""
    extra: dict[str, Any] = {}
    if mapping is not None:
        from tools.generation import event_schema
        version = gateway_from(ctx).settings.event_logging_version
        payload = {'schema_version': version, 'mapping': mapping.model_dump(exclude_none=True, exclude_defaults=True)}
        doc['description'] = with_mapping(doc.get('description'), 'translation', payload)
        try:
            from utils.xsltgen import generate
            regenerated = generate(mapping, await event_schema(ctx, version), version)['xslt']
            extra['matches_mapping'] = normalise_xslt(regenerated or '') == normalise_xslt(code)
            if not extra['matches_mapping']:
                extra['warning'] = ("The code differs from what the mapping generates: the documentation will say the "
                                    "XSLT was edited by hand. Prefer changing the mapping and regenerating.")
        except Exception:   # the schema may be unavailable here; the comparison is advice, not a gate
            pass
    elif index_plan is not None:
        doc['description'] = with_mapping(doc.get('description'), 'index', index_plan.model_dump())
        if index_plan.convention_problems():
            # Saved all the same: a field of the user's own may be meant. Said so they decide.
            extra['ecs_check'] = index_plan.convention_problems()
    elif cef_plan is not None:
        doc['description'] = with_mapping(doc.get('description'), 'cef', cef_plan.model_dump(exclude_defaults=True))
    return extra


async def create_xslt(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Document name, following the environment's naming.")],
        code: Annotated[str, Field(description="The complete XSLT.")],
        mapping: Mapping = None,
        index_plan: IndexPlan = None,
        cef_plan: Any = None,
        change: str | None = None,
) -> dict[str, Any]:
    """
    Create an XSLT in the build folder. It is checked with check_xslt first and not saved if that fails. Give the
    mapping (or index plan) it was generated from: it is kept with the XSLT, and the pipeline's documentation
    is generated from it.
    """
    await _checked(ctx, code, build)
    stroom = gateway_from(ctx)
    ref = await guard_from(ctx).create('XSLT', name, build)
    doc = await stroom.get_doc('XSLT', ref['uuid'])
    doc['data'] = code
    extra = await _described(ctx, doc, code, mapping, index_plan, cef_plan)
    doc['description'] = _pending(ctx, doc.get('description'), None, change or 'Created', doc['data'])
    doc['description'] = _previewed(doc)
    saved = await stroom.put_doc(doc)
    await _tag_kept(ctx, saved, mapping is not None or index_plan is not None or cef_plan is not None)
    from tools.plan import with_next
    return await with_next(ctx, build, {**_summary(saved), **extra})


DiscardHandEdit = Annotated[bool, Field(description=(
    "Save over an edit made by hand in Stroom's editor since the server saved the XSLT, though the new code undoes "
    "it. Only when the user has said to drop their edit: a save that would undo one is refused, saying what it "
    "changed, so it can be carried into the mapping or plan instead."))]

_CARRY = {
    'translation': "carry it into the mapping: rebuild_mapping uuid='{uuid}' stream_ids=<the sample streams> reads it "
                   "from the XSLT, proven on the sample (or build_translation_xslt changes=)",
    'index': "carry it into the index plan: a field the edit added as a field of index_plan (its name, type, and source: "
             "the XPath it reads, from the Event), one it removed left out, a source it changed changed",
    'cef': "carry it into the CEF plan (overrides), as the edit made it",
}


async def _kept_code(ctx: Context, kind: str, payload: dict[str, Any]) -> str | None:
    """What the mapping or plan kept with an XSLT generates now: the code the server saved, but for its own changes."""
    try:
        if kind == 'translation':
            from tools.generation import event_schema
            from utils.xsltgen import generate
            version = payload.get('schema_version') or gateway_from(ctx).settings.event_logging_version
            generated = generate(TranslationMapping.model_validate(payload['mapping']), await event_schema(ctx, version),
                                 version)
            return generated['xslt'] if generated['ok'] else None
        if kind == 'index':
            return FieldPlan.model_validate(payload).xslt()
        if kind == 'cef':
            from utils.cef import CefPlan
            return CefPlan.model_validate(payload).xslt()
    except Exception:       # no longer generates: the whole XSLT then counts as the edit's
        return None
    return None


KEEP, OVERWRITE = 'Keep my hand edit', 'Use the proposed field'
OVERWRITE_ALL, PER_FIELD = 'Overwrite the XSLT with the change', 'Decide field by field'
HandEditChoices = Annotated[dict[str, Literal['keep', 'overwrite', 'per_field']] | None, Field(description=(
    "Only from a needs_guidance reply about a hand edit, with the user's answers: {'*': 'overwrite'} to overwrite the "
    "XSLT with the change (every hand edit dropped), or for each field it names 'keep' (their hand edit) or "
    "'overwrite' (with the proposed field). Never chosen for them."))]
_CHOICES = 'hand_edit_choices'


def _remembered(ctx: Context, key: tuple) -> dict[str, str]:
    """The user's answers for this XSLT as it is now, kept for the session: asked once, however many calls."""
    try:
        return ctx.lifespan_context.setdefault(_CHOICES, {}).setdefault(key, {})
    except Exception:
        return {}


async def hand_edit_gate(ctx: Context, doc: dict[str, Any], code: str, kind: str | None = None,
                         payload: dict[str, Any] | None = None, choices: dict[str, str] | None = None) -> Any:
    """None when new code keeps the edit made by hand since the server saved the XSLT (doc, as it is now), or the user
    said to overwrite what it undoes; otherwise the question to return (a form, or needs_guidance), or ToolError
    saying what to carry. kind and payload: the mapping or plan the new code is from, to tell which of its fields the
    agent's change touches that the edit touched too: those the user decides."""
    import hashlib
    from tools.plan import _user
    from utils.handedit import collisions, undone
    from utils.mappingstore import code_diff, read_mapping
    from utils.xsltversion import untouched
    current = doc.get('data') or ''
    if untouched(doc.get('description'), current) is not False:
        return None     # as the server saved it, or saved before it recorded what (no edit to tell)
    kept = read_mapping(doc.get('description'))
    base = await _kept_code(ctx, *kept) if kept else None
    items = undone(base, current, code)
    if not items:
        return None
    by = f" (by {doc['updateUser']})" if doc.get('updateUser') else ''
    clashes = collisions(kind, kept[1], payload, items) if kept and kind == kept[0] else {}
    decided = _remembered(ctx, (_user(ctx), doc.get('uuid'), hashlib.sha256(current.encode()).hexdigest()[:16]))
    decided.update({k: v for k, v in (choices or {}).items() if k in clashes or k == '*'})
    # One question first (asked for by the user): overwrite the XSLT with the change, every hand edit dropped, or
    # decide field by field. Answered per field already, it isn't asked.
    upfront = None
    if clashes and '*' not in decided and not all(label in decided for label in clashes):
        others = len({str(u) for u in items} - {str(u) for _, _, hit in clashes.values() for u in hit})
        upfront = (f"XSLT '{doc.get('name')}' was edited by hand in Stroom{by}, and the agent's change changes "
                   f"{len(clashes)} field{'s' if len(clashes) > 1 else ''} the edit changed too: {', '.join(clashes)}"
                   + (f" (the edit changed {others} other thing{'s' if others > 1 else ''} as well)" if others else '')
                   + ". Overwrite the XSLT with the change, dropping your hand edits, or decide field by field?")
        chosen = await consent_from(ctx).choose(ctx, 'hand_edit', upfront, [OVERWRITE_ALL, PER_FIELD])
        if chosen in (OVERWRITE_ALL, PER_FIELD):
            decided['*'] = 'overwrite' if chosen == OVERWRITE_ALL else 'per_field'
            upfront = None
        elif chosen is not None:
            return chosen       # the form, for the client to show
    if decided.get('*') == 'overwrite':
        return None
    asked = []
    for label, (old, proposed, hit) in clashes.items():
        if label in decided:
            continue
        edit = '; '.join(f"{'took out' if u.again else 'added'} {u.item}" for u in hit[:3])
        question = (f"XSLT '{doc.get('name')}', field {label}: you edited it by hand in Stroom{by}, which {edit}. The "
                    f"agent's change " + (f"proposes {proposed}" if proposed else "removes it")
                    + (f" (the plan had {old})" if old and proposed else '') + ". Keep your hand edit, or use the "
                    f"proposed field?")
        # Unanswered up front (no form): the per-field questions go to the agent with it, for one round in the chat.
        chosen = None if upfront else await consent_from(ctx).choose(ctx, 'hand_edit', question, [KEEP, OVERWRITE])
        if chosen is None:
            asked.append({'field': label, 'question': question, 'options': {'keep': KEEP, 'overwrite': OVERWRITE}})
        elif chosen in (KEEP, OVERWRITE):
            decided[label] = 'keep' if chosen == KEEP else 'overwrite'
        else:
            return chosen       # the form, for the client to show
    if asked:
        first = ({'hand_edit': {'question': upfront, 'options': {'overwrite': OVERWRITE_ALL, 'per_field': PER_FIELD}}}
                 if upfront else {})
        return {'status': 'needs_guidance', 'saved': None, **first, 'hand_edit_collisions': asked,
                'hint': "Not saved: the user edited these fields by hand since the server saved the XSLT, and your "
                        "change changes them too. " + (
                            "Ask the user the hand_edit question first, exactly, offering both options. To overwrite: "
                            "call again with the same arguments and hand_edit_choices={'*': 'overwrite'}. Field by "
                            "field: ask each of the hand_edit_collisions questions too, " if upfront else
                            "Ask the user each question exactly, offering both options, ")
                        + "then call again with the same arguments and hand_edit_choices={field: 'keep' or "
                          "'overwrite'}. Don't choose for them."}
    overwritten = [label for label, choice in decided.items() if choice == 'overwrite' and label in clashes]
    left = [u for u in items if not any(u in clashes[label][2] for label in overwritten)]
    if not left:
        return None
    carry = _CARRY.get(kept[0] if kept else '', "write code that keeps it").format(uuid=doc.get('uuid'))
    kept_fields = [label for label, choice in decided.items() if choice == 'keep' and label in clashes]
    shown = (f" What the edit changed (- what its {'mapping' if kept[0] == 'translation' else kept[0] + ' plan'} "
             f"generates, + the XSLT): " + ' | '.join(code_diff(base, current)) + '.') if kept and base else ''
    keeps = (f" The user keeps their edit of {', '.join(kept_fields)}: leave your change to "
             f"{'it' if len(kept_fields) == 1 else 'them'} out, and make {'it' if len(kept_fields) == 1 else 'them'} "
             f"as the edit has {'it' if len(kept_fields) == 1 else 'them'}." if kept_fields else '')
    raise ToolError(f"Not saved: XSLT '{doc.get('name')}' was edited by hand since the server saved it{by}, and this "
                    f"code undoes the edit: it {'; '.join(str(u) for u in left[:8])}.{shown}{keeps} Tell the user, and "
                    f"{carry}; then save again. Only if the user says to drop their edit: discard_hand_edit=true.")


async def update_xslt(
        ctx: Context,
        uuid: Annotated[str, Field(description="XSLT UUID.")],
        code: Annotated[str, Field(description="The complete new XSLT.")],
        version: Version = None,
        mapping: Mapping = None,
        index_plan: IndexPlan = None,
        cef_plan: Any = None,
        change: str | None = None,
        discard_hand_edit: bool = False,
        hand_edit_choices: dict[str, str] | None = None,
) -> Any:
    """
    Replace an XSLT's code, after check_xslt passes. Only XSLTs this server created (including working
    copies of production XSLTs) can be changed; prove the change with step_sample and draft_code first. Give
    the mapping the new code was generated from, so the documentation follows the change. Code that undoes an
    edit made by hand since the server saved the XSLT is refused (discard_hand_edit: the user said to drop it); where
    the change is to a field the edit changed too, the user is asked whether to keep their edit or overwrite it: the
    question comes back instead of the saved doc (anything without a uuid), for the caller to return.
    """
    await _checked(ctx, code)
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('XSLT', uuid)
    await guard_from(ctx).check_managed({'type': 'XSLT', 'uuid': uuid, 'name': doc.get('name')})
    if not discard_hand_edit:
        kind, payload = (('translation', {'mapping': mapping.model_dump(exclude_none=True, exclude_defaults=True)})
                         if mapping is not None else ('index', index_plan.model_dump()) if index_plan is not None
                         else ('cef', cef_plan.model_dump(exclude_defaults=True)) if cef_plan is not None
                         else (None, None))
        gate = await hand_edit_gate(ctx, doc, code, kind, payload, hand_edit_choices)
        if gate is not None:
            return gate
    previous = dict(doc)
    from utils.xsltversion import adopt, strip
    # The history lives in the description, not the code: one an earlier version kept in the code moves there.
    doc['description'] = adopt(doc.get('description'), previous.get('data'))
    doc['data'] = strip(code)
    extra = await _described(ctx, doc, code, mapping, index_plan, cef_plan)
    doc['description'] = _pending(ctx, doc.get('description'), previous, change or 'Changed', doc['data'])
    doc['description'] = _previewed(doc)
    saved = await stroom.put_doc(doc, version)
    await _tag_kept(ctx, saved, mapping is not None or index_plan is not None or cef_plan is not None)
    return {**_summary(saved), **extra}


async def create_dictionary(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Dictionary name, as the mapping's `dictionary` / `in_dictionary` name it.")],
        text: Annotated[str, Field(description="One entry per line: key=value lines for a value map, or plain lines "
                                               "for a list. Blank lines and whitespace round keys and values are ignored.")],
        description: Annotated[str, Field(description="What the entries are and where they came from.")] = '',
) -> dict[str, Any]:
    """
    Create a Dictionary doc in the build: a small static table a translation reads at run time with
    stroom:dictionary() (the mapping's `dictionary` for key=value lines, `in_dictionary` for a list). For
    data that changes or is large, use reference data instead (find_reference_data).
    """
    stroom = gateway_from(ctx)
    ref = await guard_from(ctx).create('Dictionary', name, build)
    doc = await stroom.get_doc('Dictionary', ref['uuid'])
    doc.update(data=text, description=description)
    return {**_summary(await stroom.put_doc(doc)), 'entries': sum(1 for l in text.splitlines() if l.strip())}


async def update_dictionary(
        ctx: Context,
        uuid: Annotated[str, Field(description="Dictionary UUID.")],
        text: Annotated[str, Field(description="The complete new content, one entry per line.")],
        version: Version = None,
) -> dict[str, Any]:
    """Replace a dictionary's entries. Only dictionaries this server created can be changed."""
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Dictionary', uuid)
    await guard_from(ctx).check_managed({'type': 'Dictionary', 'uuid': uuid, 'name': doc.get('name')})
    doc['data'] = text
    return {**_summary(await stroom.put_doc(doc, version)), 'entries': sum(1 for l in text.splitlines() if l.strip())}


Uuid = Annotated[str | None, Field(description="To change an existing document this server created: its UUID. "
                                              "Omit to create a new one in the build.")]


async def save_text_converter(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Document name, following the environment's naming (new documents).")],
        converter_type: Annotated[Literal['DATA_SPLITTER', 'XML_FRAGMENT'], Field(
            description="DATA_SPLITTER for text (CSV, syslog, key=value); XML_FRAGMENT: the wrapper for XML "
                        "fragments (several root elements), read by an XMLFragmentParser. JSON and single-document "
                        "XML sources take no text converter: their template's parser reads them.")],
        code: Annotated[str, Field(description="The complete converter definition, e.g. a <dataSplitter> document.")],
        uuid: Uuid = None,
        version: Version = None,
) -> dict[str, Any]:
    """
    Save a text converter: create it in the build (see stroom://guide/data-splitter; build_data_splitter writes
    one from a spec), or with uuid replace the code of one this server created. For templates whose parser
    needs one (DSParser.textConverter, xmlFragmentParser.textConverter); not for JSON, which the JSONParser
    parses with no converter.
    """
    if uuid:
        return await update_text_converter(ctx, uuid, code, version)
    return await create_text_converter(ctx, build, name, converter_type, code)


async def save_xslt(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Document name, following the environment's naming (new documents).")],
        code: Annotated[str | None, Field(
            description="The complete XSLT, for one written by hand. Omit it with index_plan to save the indexing XSLT "
                        "generated from the plan, without it passing through you.")] = None,
        index_plan: IndexPlan = None,
        uuid: Uuid = None,
        version: Version = None,
        change: Annotated[str | None, Field(description=(
            "What this change is and why, in a line: recorded in the XSLT's version history (and the documentation's "
            "version control when it is written), e.g. 'Rule for Secret Checkout events, seen once the whole feed was "
            "processed'."))] = None,
        agent_model: Annotated[str | None, Field(description=(
            "The model you are, e.g. 'claude-haiku-5-5', for the version history; once given, remembered for the "
            "session."))] = None,
        discard_hand_edit: DiscardHandEdit = False,
        hand_edit_choices: HandEditChoices = None,
) -> dict[str, Any]:
    """
    Save an XSLT written by hand, or an indexing XSLT from its plan (index_plan, no code): create it in the build,
    or with uuid replace the code of one this server created (including working copies of production XSLTs;
    prove a hand edit with step_sample and draft_code first). It is checked with check_xslt and not saved if that
    fails. A translation generated from a mapping is saved by build_translation_xslt (build and name, or uuid),
    which keeps the mapping with it for the documentation. An edit the user made by hand since is kept: code
    that undoes it is refused, saying what it changed, to carry into the plan; where the plan changes a field the
    edit changed too, the user is asked whether to keep their edit or use the proposed field.
    """
    from utils.xsltversion import remember_model
    remember_model(ctx, agent_model)
    said = change or (('Saved again' if uuid else 'Created') + (' from its index plan' if index_plan and code is None
                                                                else ' by hand' if code else ''))
    if code is None:
        code = await _generated(index_plan)
    if uuid:
        return await update_xslt(ctx, uuid, code, version, None, index_plan, change=said,
                                 discard_hand_edit=discard_hand_edit, hand_edit_choices=hand_edit_choices)
    return await create_xslt(ctx, build, name, code, None, index_plan, change=said)


async def save_dictionary(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Dictionary name, as the mapping's `dictionary` / `in_dictionary` name it.")],
        text: Annotated[str, Field(description="One entry per line: key=value lines for a value map, or plain lines "
                                               "for a list. Blank lines and whitespace round keys and values are ignored.")],
        description: Annotated[str, Field(description="What the entries are and where they came from.")] = '',
        uuid: Uuid = None,
        version: Version = None,
) -> dict[str, Any]:
    """
    Save a Dictionary doc: a small static table a translation reads at run time with stroom:dictionary() (the
    mapping's `dictionary` for key=value lines, `in_dictionary` for a list). Create it in the build, or with
    uuid replace the entries of one this server created. Data that changes or is large belongs in reference
    data instead (find_reference_data).
    """
    if uuid:
        return await update_dictionary(ctx, uuid, text, version)
    return await create_dictionary(ctx, build, name, text, description)


ALL_TOOLS = [save_text_converter, save_xslt, save_dictionary]
