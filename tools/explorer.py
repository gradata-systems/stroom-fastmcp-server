"""Tools for finding and reading Stroom documents."""
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
            description="Name to match, as in the Stroom explorer quick filter, e.g. 'Keycloak'. "
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
        uuid: Annotated[str, Field(description="Document UUID, as returned by find_documents.")],
) -> dict[str, Any]:
    """
    A document's full content by type and UUID (XSLT, TextConverter and Documentation content verbatim in
    'data'; cluster credentials redacted), with what the server can say about it: for a Pipeline, how Stroom
    runs it (template chain, effective elements and properties and which layer set each, reference data,
    what it removes from its template); for an XSLT, what the translation does (each output element's
    source, the input fields read, imports, dictionaries and lookups).
    """
    from tools.pipelines import describe_pipeline
    from tools.validation import describe_translation
    doc = await get_document(ctx, type, uuid)
    if type == 'Pipeline':
        doc['pipeline'] = await describe_pipeline(uuid, ctx)
    elif type == 'XSLT':
        doc['translation'] = await describe_translation(ctx, xslt=doc.get('data') or '')
    return doc


ALL_TOOLS = [find_documents, describe_document]
