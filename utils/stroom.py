import asyncio
import logging
import ssl
import time
from typing import Any

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from config import Settings
from security.audit import audit

logger = logging.getLogger(__name__)


# REST resource for each document type the tools read or write.
RESOURCES = {
    'Feed': 'feed/v1', 'Pipeline': 'pipeline/v1', 'XSLT': 'xslt/v1', 'TextConverter': 'textConverter/v1',
    'XMLSchema': 'xmlSchema/v1', 'Dictionary': 'dictionary/v1', 'ElasticIndex': 'elasticIndex/v1',
    'ElasticCluster': 'elasticCluster/v1', 'Index': 'index/v2', 'Dashboard': 'dashboard/v1',
    'Documentation': 'documentation/v1',
}


def doc_link(settings: Settings, doc_type: str, uuid: str) -> str:
    """A URL that opens the document in the Stroom UI, as the explorer's 'Copy Link to Clipboard' makes."""
    base = (settings.stroom_ui_url or settings.stroom_url).rstrip('/')
    return f"{base}/?action=open-doc&docType={doc_type}&docUuid={uuid}"


def gateway_from(ctx: Context) -> 'StroomGateway':
    return ctx.lifespan_context['stroom']


def explorer_filter(types: list[str] | None = None, name: str = '*') -> dict[str, Any]:
    """An ExplorerTreeFilter as the Stroom UI sends it.

    Stroom returns nothing below System unless `requiredPermissions` is set, and `find`
    matches nothing with an empty name filter, so both always have a value here.
    """
    return {
        'includedTypes': types or None,
        'includedRootTypes': None,
        'tags': None,
        'nodeFlags': None,
        'requiredPermissions': ['VIEW'],
        'nameFilter': name or '*',
        'nameFilterChange': False,
        'recentItems': None,
    }


def _reason(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or f'HTTP {response.status_code}'
    if isinstance(body, dict):
        message, details = body.get('message'), body.get('details')
        if message and details and details not in message:
            return f"{message} ({str(details)[:300]})"
        return str(message or details or body.get('error') or body)
    return str(body)


class StroomGateway:
    """Calls the Stroom REST API (`/api/...`) as the MCP caller.

    The caller's Keycloak access token is forwarded, so Stroom applies that user's own permissions
    and audits them by name. Only with dev_no_auth (no caller token) is the configured API key used.
    """

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.stroom_url.rstrip('/') + '/api',
            headers={'Accept': 'application/json'},
            # The OS trust store, so an internal CA the host already trusts works without extra config.
            verify=ssl.create_default_context(cafile=str(settings.stroom_ca_certs) if settings.stroom_ca_certs else None),
            timeout=settings.stroom_request_timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _authorization(self) -> dict[str, str]:
        if self.settings.dev_no_auth:
            if not self.settings.stroom_api_key:
                raise ToolError("dev_no_auth needs STROOM_MCP_STROOM_API_KEY to call Stroom")
            return {'Authorization': f'Bearer {self.settings.stroom_api_key.get_secret_value()}'}
        token = get_access_token()
        if token is None:
            raise ToolError("No caller identity to call Stroom with")
        audience = (token.claims or {}).get('aud')
        if self.settings.stroom_audience not in (audience if isinstance(audience, list) else [audience]):
            raise ToolError(f"Your access token is not valid for Stroom: its aud claim lacks "
                            f"'{self.settings.stroom_audience}'. Keycloak needs an audience mapper for it on the "
                            f"client you signed in with.")
        if token.expires_at and token.expires_at <= time.time():
            raise ToolError("Your access token expired during this call; sign in again or refresh, then call again")
        return {'Authorization': f'Bearer {token.token}'}

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        """Send one request and return the decoded JSON body (None when empty).

        Failures become ToolErrors with Stroom's own reason, so the model can correct itself.
        """
        headers = self._authorization()
        started = time.perf_counter()
        who = {'method': method, 'path': path}
        try:
            response = await self._client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as e:
            logger.exception("Stroom %s %s failed", method, path)
            audit('stroom_request', outcome='error', error=str(e), **who)
            raise ToolError("Stroom is unavailable") from e

        took_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code in (401, 403):
            audit('access_denied', reason=f'stroom_{response.status_code}', status=response.status_code, **who)
            raise ToolError("Stroom denied access to this request")
        if response.is_error:
            reason = _reason(response)
            audit('stroom_request', outcome='error', status=response.status_code, error=reason, took_ms=took_ms, **who)
            if response.status_code == 404:
                raise ToolError(f"Not found in Stroom: {path}")
            raise ToolError(f"Stroom rejected the request ({response.status_code}): {reason}")

        audit('stroom_request', outcome='success', status=response.status_code, took_ms=took_ms, **who)
        return response.json() if response.content else None

    async def get(self, path: str) -> Any:
        return await self.request('GET', path)

    async def post(self, path: str, body: Any) -> Any:
        return await self.request('POST', path, body)

    async def find_documents(self, name: str, types: list[str] | None, limit: int) -> dict[str, Any]:
        return await self.post('/explorer/v2/find', {
            'filter': explorer_filter(types, name),
            'pageRequest': {'offset': 0, 'length': limit},
        })

    async def pipeline_layers(self, uuid: str) -> list[dict[str, Any]]:
        return await self.post('/pipeline/v1/fetchPipelineLayers', {'type': 'Pipeline', 'uuid': uuid})

    async def get_doc(self, doc_type: str, uuid: str) -> dict[str, Any]:
        if doc_type not in RESOURCES:
            raise ToolError(f"Documents of type '{doc_type}' are not supported here")
        return await self.get(f'/{RESOURCES[doc_type]}/{uuid}')

    async def put_doc(self, doc: dict[str, Any], expected_version: str | None = None) -> dict[str, Any]:
        """Save a document. With expected_version, refuse if someone else saved it since it was read."""
        if expected_version is not None:
            current = await self.get_doc(doc['type'], doc['uuid'])
            if current.get('version') != expected_version:
                raise ToolError(f"{doc['type']} '{doc.get('name')}' changed since it was read (version "
                                f"{current.get('version')}); read it again and reapply the change")
        return await self.request('PUT', f"/{RESOURCES[doc['type']]}/{doc['uuid']}", doc)

    async def datafeed(self, feed: str, data: bytes, headers: dict[str, str]) -> httpx.Response:
        """POST data to Stroom's datafeed receiver, as a sending system would."""
        url = self.settings.stroom_url.rstrip('/') + self.settings.datafeed_path
        try:
            response = await self._client.post(url, content=data,
                                               headers={'Feed': feed, **headers, **self._authorization()})
        except httpx.HTTPError as e:
            audit('stroom_request', outcome='error', error=str(e), method='POST', path=self.settings.datafeed_path)
            raise ToolError("Stroom's datafeed is unavailable") from e
        audit('stroom_request', outcome='success' if response.is_success else 'error', status=response.status_code,
              method='POST', path=self.settings.datafeed_path, feed=feed, bytes=len(data))
        if response.is_error:
            raise ToolError(f"Stroom refused the upload ({response.status_code}): "
                            f"{response.headers.get('Stroom-Error') or response.text[:300]}")
        return response

    async def find_meta(self, terms: list[dict[str, Any]], limit: int, op: str = 'AND',
                        newest_first: bool = True) -> dict[str, Any]:
        """Stream metadata matching expression terms, newest first (or oldest first)."""
        return await self.post('/meta/v1/find', {
            'expression': {'type': 'operator', 'op': op, 'children': terms},
            'pageRequest': {'offset': 0, 'length': limit},
            'sortList': [{'id': 'Id', 'desc': newest_first}],
        })

    async def fetch_data(self, meta_id: int, record_index: int, record_count: int, mode: str = 'TEXT',
                         child_type: str | None = None) -> dict[str, Any]:
        """Records from a stream (TEXT), or its error markers (MARKER)."""
        return await self.post('/data/v1/fetch', {
            'sourceLocation': {'metaId': meta_id, 'partIndex': 0, 'recordIndex': record_index, 'childType': child_type},
            'displayMode': mode,
            'recordCount': record_count,
            'expandedSeverities': ['INFO', 'WARN', 'ERROR', 'FATAL'],
        })

    async def step(self, request: dict[str, Any], poll_seconds: float = 0.5, max_wait: float = 120) -> dict[str, Any]:
        """Run one stepping request to completion.

        Each step is a fresh request with no session id; Stroom creates a session, and drops it
        once the step completes or after 10 s idle. The session id is only used to poll a step
        that has not completed yet.
        """
        request = {k: v for k, v in request.items() if k != 'sessionUuid'}
        result = await self.post('/stepping/v1/step', request)
        waited = 0.0
        while not result.get('complete'):
            if waited >= max_wait:
                await self.post('/stepping/v1/terminateStepping', {**request, 'sessionUuid': result['sessionUuid']})
                raise ToolError("Stepping did not finish in time; try fewer records or a smaller stream")
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
            result = await self.post('/stepping/v1/step', {**request, 'sessionUuid': result['sessionUuid']})
        return result
