"""Tools that create and change translation content (text converters and XSLTs) in a build."""
import re
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import Field

from security.guard import guard_from
from tools.validation import check_xslt
from utils.stroom import gateway_from

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed, e.g. 'keycloak-v1.3'.")]
Version = Annotated[str | None, Field(
    description="The document version from the last read; the save is refused if it changed since.")]


def _summary(doc: dict[str, Any]) -> dict[str, Any]:
    return {'type': doc.get('type'), 'uuid': doc.get('uuid'), 'name': doc.get('name'), 'version': doc.get('version')}


def _check_converter(converter_type: str, code: str) -> None:
    """A Data Splitter is a <dataSplitter> document. JSON needs no text converter at all: the JSONParser element
    of the Event Data (JSON) template parses it, and a converter holding a <jsonParser> element parses nothing."""
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
        raise ToolError("Not saved: a text converter cannot parse JSON. Use the Event Data (JSON) template, whose "
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


async def _checked(ctx: Context, xslt: str) -> None:
    result = await check_xslt(ctx, xslt)
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
    needs one (DSParser.textConverter in find_pipeline_templates). Not for JSON: the Event Data (JSON)
    template's JSONParser parses JSON, one object per line or an array, with no converter.
    """
    _check_converter(converter_type, code)
    stroom = gateway_from(ctx)
    ref = await guard_from(ctx).create('TextConverter', name, build)
    doc = await stroom.get_doc('TextConverter', ref['uuid'])
    doc.update(converterType=converter_type, data=code)
    return _summary(await stroom.put_doc(doc))


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


async def create_xslt(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Document name, following the environment's naming.")],
        code: Annotated[str, Field(description="The complete XSLT.")],
) -> dict[str, Any]:
    """Create an XSLT in the build folder. It is checked with check_xslt first and not saved if that fails."""
    await _checked(ctx, code)
    stroom = gateway_from(ctx)
    ref = await guard_from(ctx).create('XSLT', name, build)
    doc = await stroom.get_doc('XSLT', ref['uuid'])
    doc['data'] = code
    return _summary(await stroom.put_doc(doc))


async def update_xslt(
        ctx: Context,
        uuid: Annotated[str, Field(description="XSLT UUID.")],
        code: Annotated[str, Field(description="The complete new XSLT.")],
        version: Version = None,
) -> dict[str, Any]:
    """
    Replace an XSLT's code, after check_xslt passes. Only XSLTs this server created (including working
    copies of production XSLTs) can be changed; prove the change with step_sample and draft_code first.
    """
    await _checked(ctx, code)
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('XSLT', uuid)
    await guard_from(ctx).check_managed({'type': 'XSLT', 'uuid': uuid, 'name': doc.get('name')})
    doc['data'] = code
    return _summary(await stroom.put_doc(doc, version))


ALL_TOOLS = [create_text_converter, update_text_converter, create_xslt, update_xslt]
