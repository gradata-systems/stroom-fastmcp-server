"""Tools that create and change translation content (text converters and XSLTs) in a build."""
import re
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field

from security.guard import guard_from
from tools.validation import check_xslt
from utils.fieldplan import FieldPlan
from utils.mappingstore import normalise_xslt, with_mapping
from utils.stroom import gateway_from
from utils.xsltgen import TranslationMapping

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed, e.g. 'acme-door-v1.3'.")]
Version = Annotated[str | None, Field(
    description="The document version from the last read; the save is refused if it changed since.")]


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


async def _described(ctx: Context, doc: dict[str, Any], code: str, mapping: TranslationMapping | None,
                     index_plan: FieldPlan | None) -> dict[str, Any]:
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
    return extra


async def create_xslt(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Document name, following the environment's naming.")],
        code: Annotated[str, Field(description="The complete XSLT.")],
        mapping: Mapping = None,
        index_plan: IndexPlan = None,
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
    extra = await _described(ctx, doc, code, mapping, index_plan)
    from tools.plan import with_next
    return await with_next(ctx, build, {**_summary(await stroom.put_doc(doc)), **extra})


async def update_xslt(
        ctx: Context,
        uuid: Annotated[str, Field(description="XSLT UUID.")],
        code: Annotated[str, Field(description="The complete new XSLT.")],
        version: Version = None,
        mapping: Mapping = None,
        index_plan: IndexPlan = None,
) -> dict[str, Any]:
    """
    Replace an XSLT's code, after check_xslt passes. Only XSLTs this server created (including working
    copies of production XSLTs) can be changed; prove the change with step_sample and draft_code first. Give
    the mapping the new code was generated from, so the documentation follows the change.
    """
    await _checked(ctx, code)
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('XSLT', uuid)
    await guard_from(ctx).check_managed({'type': 'XSLT', 'uuid': uuid, 'name': doc.get('name')})
    doc['data'] = code
    extra = await _described(ctx, doc, code, mapping, index_plan)
    return {**_summary(await stroom.put_doc(doc, version)), **extra}


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
) -> dict[str, Any]:
    """
    Save an XSLT written by hand, or an indexing XSLT from its plan (index_plan, no code): create it in the build,
    or with uuid replace the code of one this server created (including working copies of production XSLTs;
    prove a hand edit with step_sample and draft_code first). It is checked with check_xslt and not saved if that
    fails. A translation generated from a mapping is saved by build_translation_xslt (build and name, or uuid),
    which keeps the mapping with it for the documentation.
    """
    if code is None:
        code = await _generated(index_plan)
    if uuid:
        return await update_xslt(ctx, uuid, code, version, None, index_plan)
    return await create_xslt(ctx, build, name, code, None, index_plan)


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
