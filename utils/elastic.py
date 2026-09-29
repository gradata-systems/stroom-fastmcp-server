"""Minimal Elasticsearch client for index templates: the only thing this server touches in ES directly.

Documents reach Elasticsearch through Stroom's ElasticIndexingFilter, and indexing is verified through
Stroom searches, so reads and writes here are limited to index and component templates.
"""
import fnmatch
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from config import Settings
from security.audit import audit
from utils.tls import trust


class ElasticTemplates:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = None
        if settings.es_url:
            headers = {'Accept': 'application/json'}
            if settings.es_api_key:
                headers['Authorization'] = f'ApiKey {settings.es_api_key.get_secret_value()}'
            self._client = httpx.AsyncClient(
                base_url=settings.es_url.rstrip('/'), headers=headers, timeout=30, transport=transport,
                verify=trust(settings.es_ca_certs))

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        if not self._client:
            raise ToolError("Elasticsearch is not configured on this server (STROOM_MCP_ES_URL); template tools "
                            "are unavailable, but Stroom-side indexing and verification still work")
        response = await self._client.request(method, path, json=body)
        audit('es_request', method=method, path=path, status=response.status_code)
        if response.status_code == 404:
            return None
        if response.is_error:
            try:
                reason = response.json().get('error', {}).get('reason')
            except ValueError:
                reason = response.text[:300]
            raise ToolError(f"Elasticsearch rejected the request ({response.status_code}): {reason}")
        return response.json()

    async def index_templates(self, pattern: str) -> list[dict[str, Any]]:
        body = await self._call('GET', f'/_index_template/{pattern}')
        return (body or {}).get('index_templates', [])

    async def component_templates(self, pattern: str) -> list[dict[str, Any]]:
        body = await self._call('GET', f'/_component_template/{pattern}')
        return (body or {}).get('component_templates', [])

    async def simulate(self, index_name: str) -> dict[str, Any] | None:
        return await self._call('POST', f'/_index_template/_simulate_index/{index_name}')

    async def put_index_template(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if not any(fnmatch.fnmatchcase(name, p) for p in self.settings.es_template_patterns):
            raise ToolError(f"Template name '{name}' is outside the allowed patterns {self.settings.es_template_patterns}")
        return await self._call('PUT', f'/_index_template/{name}', body)


def flatten_mapping(properties: dict[str, Any], prefix: str = '') -> dict[str, str]:
    """{'user': {'properties': {'name': {'type': 'keyword'}}}} -> {'user.name': 'keyword'}."""
    out: dict[str, str] = {}
    for name, spec in (properties or {}).items():
        path = f'{prefix}.{name}' if prefix else name
        if 'properties' in spec:
            out.update(flatten_mapping(spec['properties'], path))
        elif 'type' in spec:
            out[path] = spec['type']
    return out
