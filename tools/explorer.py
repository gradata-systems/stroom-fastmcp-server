"""Tools for finding and reading Stroom documents."""
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from utils.stroom import gateway_from

DocType = Literal['Feed', 'Pipeline', 'XSLT', 'TextConverter', 'XMLSchema', 'Dictionary', 'ElasticIndex',
                  'ElasticCluster', 'Dashboard', 'Documentation', 'Folder']

# REST resource for each readable document type.
_RESOURCES = {
    'Feed': 'feed/v1',
    'Pipeline': 'pipeline/v1',
    'XSLT': 'xslt/v1',
    'TextConverter': 'textConverter/v1',
    'XMLSchema': 'xmlSchema/v1',
    'Dictionary': 'dictionary/v1',
    'ElasticIndex': 'elasticIndex/v1',
    'ElasticCluster': 'elasticCluster/v1',
    'Dashboard': 'dashboard/v1',
    'Documentation': 'documentation/v1',
}
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
        types: Annotated[list[DocType] | None, Field(
            description="Document types to include, e.g. ['Pipeline', 'XSLT']. All types when omitted.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of documents to return.")] = 100,
) -> dict[str, Any]:
    """
    Find Stroom documents by name and type, returning each one's type, UUID, name and folder path.
    Start here to locate feeds, pipelines, XSLTs, template pipelines or existing Elastic indices.
    """
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
    resource = _RESOURCES.get(type)
    if resource is None:
        raise ToolError(f"Documents of type '{type}' cannot be read with this tool")
    doc = await gateway_from(ctx).get(f'/{resource}/{uuid}')
    return _redact(doc)


ALL_TOOLS = [find_documents, get_document]
