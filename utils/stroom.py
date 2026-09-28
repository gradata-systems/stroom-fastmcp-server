import asyncio
import logging
import ssl
import time
from typing import Any

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError

from config import Settings
from security.audit import audit

logger = logging.getLogger(__name__)


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
        return str(body.get('message') or body.get('details') or body.get('error') or body)
    return str(body)


class StroomGateway:
    """Calls the Stroom REST API (`/api/...`) on behalf of the MCP caller.

    Stroom applies the permissions of whichever identity the request carries. For now that is
    the configured API key's owner; Keycloak token exchange will replace it with the caller's own
    identity.
    """

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.stroom_url.rstrip('/') + '/api',
            headers={'Authorization': f'Bearer {settings.stroom_api_key.get_secret_value()}',
                     'Accept': 'application/json'},
            # The OS trust store, so an internal CA the host already trusts works without extra config.
            verify=ssl.create_default_context(cafile=str(settings.stroom_ca_certs) if settings.stroom_ca_certs else None),
            timeout=settings.stroom_request_timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        """Send one request and return the decoded JSON body (None when empty).

        Failures become ToolErrors with Stroom's own reason, so the model can correct itself.
        """
        started = time.perf_counter()
        who = {'method': method, 'path': path}
        try:
            response = await self._client.request(method, path, json=body)
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

    async def find_meta(self, terms: list[dict[str, Any]], limit: int, op: str = 'AND') -> dict[str, Any]:
        """Stream metadata matching expression terms, newest first."""
        return await self.post('/meta/v1/find', {
            'expression': {'type': 'operator', 'op': op, 'children': terms},
            'pageRequest': {'offset': 0, 'length': limit},
            'sortList': [{'id': 'Id', 'desc': True}],
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
