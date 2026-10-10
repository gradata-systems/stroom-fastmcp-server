"""Tools for finding and reading Stroom documents."""
import re
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from utils.params import ONE_OR_MORE
from utils.stroom import RESOURCES, gateway_from

DocType = Literal['Feed', 'Pipeline', 'XSLT', 'TextConverter', 'XMLSchema', 'Dictionary', 'ElasticIndex', 'Index',
                  'ElasticCluster', 'Dashboard', 'Documentation', 'Folder']

# Fields never returned to the model.
_SECRET_FIELDS = {'apiKeySecret', 'password', 'caCertificate'}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ('<redacted>' if k in _SECRET_FIELDS and v else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


async def find_documents(
        ctx: Context,
        name: Annotated[str, Field(
            description="Name to match, as in the Stroom explorer quick filter, e.g. 'Acme'. "
                        "Use '*' to match everything of the given types.")] = '*',
        types: Annotated[list[DocType] | str | None, ONE_OR_MORE, Field(
            description="Document types to include, e.g. ['Pipeline', 'XSLT']. All types when omitted.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of documents to return.")] = 100,
        content: Annotated[str | None, Field(
            description="Instead of the name, text the documents' content must contain, e.g. a vendor name, a source "
                        "field or an event id in XSLTs: finds existing translations of similar sources to learn from.")] = None,
) -> dict[str, Any]:
    """
    Find Stroom documents by name and type (or by text in their content), returning each one's type, UUID,
    name and folder path. Start here to locate feeds, pipelines, XSLTs, template pipelines or existing
    Elastic indices, and, with content, translations of similar sources to reuse ideas from.
    """
    if content:
        body = await gateway_from(ctx).post('/explorer/v2/findInContent', {
            'filter': {'matchType': 'CONTAINS', 'pattern': content, 'caseSensitive': False},
            'pageRequest': {'offset': 0, 'length': limit * 3}})
        matches = []
        for value in body.get('values') or []:
            match = value.get('docContentMatch') or {}
            ref = match.get('docRef') or {}
            if not types or ref.get('type') in types:
                matches.append({'type': ref.get('type'), 'uuid': ref.get('uuid'), 'name': ref.get('name'),
                                'path': (value.get('path') or '').replace(' / ', '/'), 'sample': (match.get('sample') or '')[:300]})
        return {'content': content, 'documents': matches[:limit]}
    body = await gateway_from(ctx).find_documents(name, list(types) if types else None, limit)
    documents = [{**v['docRef'], 'path': v.get('path')} for v in body.get('values', [])]
    if types:
        # Stroom also returns the folders that contain a match; keep only what was asked for.
        documents = [d for d in documents if d.get('type') in types]
    total = (body.get('pageResponse') or {}).get('total')
    result: dict[str, Any] = {'total': total, 'returned': len(documents), 'documents': documents}
    if total and total > len(documents):
        result['hint'] = "More documents match; narrow the name or types, or raise the limit."
    return result


async def get_document(
        ctx: Context,
        type: Annotated[DocType, Field(description="Document type, as returned by find_documents.")],
        uuid: Annotated[str, Field(description="Document UUID, as returned by find_documents.")],
) -> dict[str, Any]:
    """
    Fetch a document's full content by type and UUID. XSLT, TextConverter and Documentation
    content is returned verbatim in 'data'; credentials in cluster documents are redacted.
    """
    if type not in RESOURCES:
        raise ToolError(f"Documents of type '{type}' cannot be read with this tool")
    doc = await gateway_from(ctx).get_doc(type, uuid)
    return _redact(doc)


async def describe_document(
        ctx: Context,
        type: Annotated[DocType, Field(description="Document type, as returned by find_documents.")],
        uuid: Annotated[str, Field(description="Document UUID, as returned by find_documents. Left out with type "
                                               "XMLSchema and element: the configured event-logging schema.")] = '',
        element: Annotated[str | None, Field(
            description="XMLSchema of event-logging only: what this element takes, e.g. 'EventDetail' (the action "
                        "elements), 'EventDetail/Authenticate' or 'EventSource/User': its children (required, "
                        "repeatable, one of a choice), a leaf's type and allowed values, each described.")] = None,
        find: Annotated[str | None, Field(
            description="Documentation only: return just the passages mentioning this (case-insensitive; several "
                        "terms separated by |), e.g. an event id, for a long reference document.")] = None,
        context_lines: Annotated[int, Field(ge=0, le=60, description="With find: lines kept around each match.")] = 8,
        mapping: Annotated[bool, Field(description=(
            "XSLT only: the mapping kept with it, whole, to read. Not needed to change it (build_translation_xslt "
            "uuid= changes= only what changes) or to regenerate it (uuid= alone)."))] = False,
) -> dict[str, Any]:
    """
    A document's full content by type and UUID (XSLT, TextConverter and Documentation content verbatim in
    'data'; cluster credentials redacted), with what the server can say about it: for a Pipeline, how Stroom
    runs it (template chain, effective elements and properties and which layer set each, reference data,
    what it removes from its template); for an XSLT, what the translation does (each output element's
    source, the input fields read, imports, dictionaries and lookups); for an Elastic Index or Lucene Index doc, a
    survey of what the index holds, read through Stroom: its fields, the newest documents (how often each field
    is populated, sample values) and the pipelines that feed it. A long Documentation doc (a vendor manual) comes
    back cut short with its outline; find= returns the passages about one thing instead. For the event-logging
    XMLSchema, element= says what an element takes (children, required, choices, allowed values), uuid optional.
    """
    from tools.pipelines import describe_pipeline
    from tools.validation import describe_event_element, describe_translation
    if type == 'XMLSchema' and element is not None and not uuid:
        return await describe_event_element(ctx, element)
    if not uuid:
        raise ToolError("Give the document's uuid (find_documents)")
    doc = await get_document(ctx, type, uuid)
    version = re.search(r'event-logging-v([\d.]+)\.xsd', doc.get('systemId') or '') if type == 'XMLSchema' else None
    if version:
        # The whole XSD is over 100 KB (seen: an agent's client put it in a file, and the agent spent twelve minutes
        # reading it with PowerShell): what an element takes, EventDetail's action elements by default.
        return {'type': type, 'uuid': uuid, 'name': doc.get('name'), 'systemId': doc.get('systemId'),
                **await describe_event_element(ctx, element or 'EventDetail', version.group(1)),
                'hint': "element= any path below Event (e.g. EventDetail/Authenticate, EventSource/User) says what "
                        "it takes; the XSD itself isn't returned."}
    if type == 'Documentation':
        return _passages(doc, find, context_lines)
    if type == 'Pipeline':
        doc['pipeline'] = await describe_pipeline(uuid, ctx)
    elif type == 'XSLT':
        doc['translation'] = await describe_translation(ctx, xslt=doc.get('data') or '')
        _kept_summary(doc, uuid, mapping)
    elif type in ('ElasticIndex', 'Index'):
        from tools.indexing import survey_index
        try:
            survey = await survey_index(ctx, type, uuid)
        except ToolError as e:
            # The cluster may be unreachable: the doc itself is still worth having (e.g. to copy its settings).
            doc['survey_error'] = f"Could not survey the index through Stroom: {e}"
        else:
            survey.pop('documents', None)
            survey['fed_by'] = [{**p, 'plan': bool(p['plan'])} for p in survey['fed_by']]
            doc['survey'] = survey
    return doc


def _kept_summary(doc: dict[str, Any], uuid: str, whole: bool) -> None:
    """The mapping kept in an XSLT's description as a summary, and its pending changes as their lines: seen in
    production, the whole mapping (1,400 lines) was spilled by the client to a file, which the agent read in parts and
    then sent back whole to regenerate the XSLT."""
    from utils.mappingstore import read_mapping, without_block
    from utils.xsltversion import pending_of
    description = doc.get('description') or ''
    kept, pending = read_mapping(description), pending_of(description)
    if not kept and not pending:
        return
    doc['description'] = without_block(description, None)     # its own text and its version history
    if pending:
        doc['pending_changes'] = [e.get('change') for e in pending.get('entries') or []]
    if not kept:
        return
    kind, payload = kept
    if whole:
        doc['kept_mapping'] = {'kind': kind, **payload}
        return
    summary: dict[str, Any] = {'kind': kind}
    if kind == 'translation':
        from tools.describe import _event_kinds
        body = payload.get('mapping') or {}
        summary.update({'schema_version': payload.get('schema_version'), 'input': body.get('input'),
                        'rules': _event_kinds(body), 'common_entries': len(body.get('common') or []),
                        'extractions': len(body.get('extract') or []),
                        'style': body.get('style') or "the generator's current defaults"})
        summary['how'] = (f"build_translation_xslt uuid='{uuid}' changes={{...}} changes it (only what changes); "
                          f"uuid='{uuid}' alone regenerates it as the generator writes it now. describe_document "
                          f"mapping=true shows it whole, to read; never send it back whole.")
    else:
        summary.update({k: (len(v) if isinstance(v, list) else v) for k, v in payload.items()
                        if not isinstance(v, dict)})
    doc['kept_mapping'] = summary


DOC_CHARS = 30_000     # a Documentation doc's text returned whole up to this; past it, the outline and find=


def _passages(doc: dict[str, Any], find: str | None, context_lines: int) -> dict[str, Any]:
    """A Documentation doc for the model: whole when short; otherwise its outline and the start, or with find the
    passages that mention it, so a long manual is read where it matters, not all at once."""
    import re
    from utils.stroom import body_text
    text = body_text(doc)
    lines = text.splitlines()
    out = {k: v for k, v in doc.items() if k not in ('data', 'documentation')}
    out['characters'] = len(text)
    if find:
        terms = [t.strip().lower() for t in find.split('|') if t.strip()]
        hits = [n for n, line in enumerate(lines) if any(t in line.lower() for t in terms)]
        spans: list[list[int]] = []
        for n in hits:
            lo, hi = max(0, n - context_lines), min(len(lines), n + context_lines + 1)
            if spans and lo <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], hi)
            else:
                spans.append([lo, hi])
        shown, size = [], 0
        for lo, hi in spans:
            passage = '\n'.join(lines[lo:hi])
            if size + len(passage) > DOC_CHARS:
                break
            shown.append({'lines': f'{lo + 1}-{hi}', 'text': passage})
            size += len(passage)
        out.update(find=find, matches=len(hits), passages=shown)
        if len(shown) < len(spans):
            out['note'] = f"{len(spans) - len(shown)} more passages: narrow find, or lower context_lines"
        if not hits:
            out['note'] = "Nothing mentions it: try another term (the vendor's name for the event or field)."
        return out
    if len(text) <= DOC_CHARS:
        out['data'] = text
        return out
    out['outline'] = [line.strip() for line in lines if re.match(r'#{1,4} ', line)][:300]
    out['data'] = text[:DOC_CHARS]
    out['note'] = (f"Cut short: {DOC_CHARS:,} of {len(text):,} characters. Read the parts you need with find= (an event "
                   f"id, a field name, a heading from the outline).")
    return out


ALL_TOOLS = [find_documents, describe_document]
