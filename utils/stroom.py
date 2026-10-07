import asyncio
import logging
import re
import time
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from config import Settings
from security.audit import audit, spent_in_stroom
from utils.tls import trust

logger = logging.getLogger(__name__)


# REST resource for each document type the tools read or write.
RESOURCES = {
    'Feed': 'feed/v1', 'Pipeline': 'pipeline/v1', 'XSLT': 'xslt/v1', 'TextConverter': 'textConverter/v1',
    'XMLSchema': 'xmlSchema/v1', 'Dictionary': 'dictionary/v1', 'ElasticIndex': 'elasticIndex/v1',
    'ElasticCluster': 'elasticCluster/v1', 'Index': 'index/v2', 'Dashboard': 'dashboard/v1',
    'Documentation': 'documentation/v1',
}


def body_text(doc: dict[str, Any]) -> str:
    """The text of a Documentation doc: its body (data), which the Stroom UI shows and edits. Falls back to
    the Documentation tab (documentation), where 0.6.0 and earlier wrote it, and where people sometimes write."""
    return doc.get('data') or doc.get('documentation') or ''


def set_body_text(doc: dict[str, Any], text: str) -> None:
    """Write a Documentation doc's body. Text an earlier version of the server put in the Documentation tab
    instead (the body empty) is moved, not left behind as a stale copy."""
    if not doc.get('data') and doc.get('documentation'):
        doc['documentation'] = None
    doc['data'] = text


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


# Writes that change pipelines or where documents sit: a pipeline doc itself, or the explorer tree.
_PIPELINE_DOC = re.compile(r'^/pipeline/v1/([0-9a-fA-F-]{36})$')
_CHANGES_PIPELINES = re.compile(r'^/(pipeline/v1/|explorer/v2/(create|copy|move|delete))')


class StroomGateway:
    """Calls the Stroom REST API (`/api/...`) as the MCP caller.

    The caller's access token is forwarded, so Stroom applies that user's own permissions
    and audits them by name. Only with dev_no_auth (no caller token) is the configured API key used.
    """

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 authorization: dict[str, str] | None = None):
        self.settings = settings
        self.pipelines_changed = 0.0      # when this server last changed a pipeline or the explorer tree
        self.pipelines_written: set[str] = set()   # pipelines this server wrote, which the search may not list yet
        # Outside a tool call (an upload with a ticket), the caller's token comes with the request, not the context.
        self._fixed_authorization = authorization
        self._client = httpx.AsyncClient(
            base_url=settings.stroom_url.rstrip('/') + '/api',
            headers={'Accept': 'application/json'},
            verify=trust(settings.stroom_ca_certs),
            timeout=settings.stroom_request_timeout,
            transport=transport,
            # One client serves every user: a cookie Stroom (or an ingress) sets for one request must not ride along on
            # another user's. A stepping follow-up carries its own first response's cookies explicitly (step).
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _authorization(self) -> dict[str, str]:
        if self._fixed_authorization:
            return self._fixed_authorization
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
                            f"'{self.settings.stroom_audience}'. The identity provider must add it to tokens "
                            f"for the client you signed in with (in Keycloak, an audience mapper).")
        if token.expires_at and token.expires_at <= time.time():
            # Ran out during the call (a form left open, a long wait): calling again carries a fresh token, and an
            # answer the user gave in this call is kept for it.
            from utils.consent import retry_after_expiry
            raise ToolError(retry_after_expiry()['hint'])
        return {'Authorization': f'Bearer {token.token}'}

    async def request(self, method: str, path: str, body: Any = None, cookie: str | None = None,
                      with_cookies: bool = False) -> Any:
        """Send one request and return the decoded JSON body (None when empty); with_cookies, (body, the cookies the
        response set, as a Cookie header). cookie is sent as the request's Cookie header.

        Failures become ToolErrors with Stroom's own reason, so the model can correct itself.
        """
        headers = {**self._authorization(), **({'Cookie': cookie} if cookie else {})}
        started = time.perf_counter()
        who = {'method': method, 'path': path}
        try:
            response = await self._client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as e:
            logger.exception("Stroom %s %s failed", method, path)
            audit('stroom_request', outcome='error', error=str(e), **who)
            raise ToolError("Stroom is unavailable") from e

        took_ms = round((time.perf_counter() - started) * 1000)
        spent_in_stroom(took_ms, method, path)
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
        if method != 'GET' and _CHANGES_PIPELINES.match(path):
            # A pipeline made, copied, moved, changed or deleted: what was cached about them is out of date.
            self.pipelines_changed = time.monotonic()
            written = _PIPELINE_DOC.match(path)
            if written and method == 'PUT':
                # And the explorer's search lists a new pipeline only a while later: it is remembered here.
                self.pipelines_written.add(written.group(1))
        data = response.json() if response.content else None
        if with_cookies:
            pairs = [c.split(';', 1)[0].strip() for c in response.headers.get_list('set-cookie')]
            return data, '; '.join(p for p in pairs if '=' in p) or None
        return data

    async def get(self, path: str) -> Any:
        return await self.request('GET', path)

    async def post(self, path: str, body: Any) -> Any:
        return await self.request('POST', path, body)

    async def find_documents(self, name: str, types: list[str] | None, limit: int, offset: int = 0) -> dict[str, Any]:
        return await self.post('/explorer/v2/find', {
            'filter': explorer_filter(types, name),
            'pageRequest': {'offset': offset, 'length': limit},
        })

    async def find_all_documents(self, name: str, types: list[str] | None, page: int = 1000) -> list[dict[str, Any]]:
        """Every match, page by page: an environment can hold more than one page of pipelines."""
        values: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(1000):
            found = await self.find_documents(name, types, page, len(values))
            batch = found.get('values') or []
            fresh = [v for v in batch if (v.get('docRef') or {}).get('uuid') not in seen]
            if not fresh:
                return values     # an empty page, or the same page again: Stroom ignored the offset
            seen.update((v.get('docRef') or {}).get('uuid') for v in fresh)
            values += fresh
            total = (found.get('pageResponse') or {}).get('total')
            if len(batch) < page or (total is not None and len(values) >= total):
                return values
        return values

    async def property_types(self) -> dict[tuple[str, str], str]:
        """{(element type, property name): the type Stroom declares ('boolean', 'int', 'long', 'String', a document
        type ...)}, read once: {} where Stroom has no such resource."""
        if getattr(self, '_property_types', None) is None:
            try:
                rows = await self.get('/pipeline/v1/propertyTypes')
            except ToolError:
                rows = []
            self._property_types = {((row.get('pipelineElementType') or {}).get('type'), name): (spec or {}).get('type')
                                    for row in rows or [] for name, spec in (row.get('propertyTypes') or {}).items()}
        return self._property_types

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
        who = {'method': 'POST', 'path': self.settings.datafeed_path, 'feed': feed, 'bytes': len(data)}
        if response.status_code in (401, 403):
            audit('access_denied', reason=f'stroom_{response.status_code}', status=response.status_code, **who)
        else:
            audit('stroom_request', outcome='success' if response.is_success else 'error',
                  status=response.status_code, **who)
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
            'sortList': [{'id': 'Id', 'desc': newest_first, 'ignoreCase': False}],
            # Stroom's booleans given, not left null: a null one is an ERROR in Stroom's log on every request.
            'fetchRelationships': False,
        })

    async def processor_filters(self, pipeline_uuid: str) -> list[dict[str, Any]]:
        """The pipeline's processor filters (not deleted). Asked of Stroom by pipeline: every filter on an instance
        (2,000 on the local stack) was fetched on each call, and Stroom logged a warning for each one whose pipeline is
        gone."""
        rows = await self.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': [
            {'type': 'term', 'field': 'Processor Pipeline', 'condition': 'IS_DOC_REF',
             'docRef': {'type': 'Pipeline', 'uuid': pipeline_uuid}}]}})
        return [r['processorFilter'] for r in rows.get('values') or []
                if r.get('processorFilter') and r['processorFilter'].get('pipelineUuid') == pipeline_uuid
                and not r['processorFilter'].get('deleted')]

    async def fetch_data(self, meta_id: int, record_index: int, record_count: int, mode: str = 'TEXT',
                         child_type: str | None = None) -> dict[str, Any]:
        """Records from a stream (TEXT), or its error markers (MARKER)."""
        return await self.post('/data/v1/fetch', {
            'sourceLocation': {'metaId': meta_id, 'partIndex': 0, 'recordIndex': record_index, 'childType': child_type},
            'displayMode': mode, 'showAsHtml': False,
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
        # The session lives on the Stroom node that started the step: a follow-up carries the cookies the first
        # response set, so an ingress with cookie affinity sends it to that node.
        result, cookie = await self.request('POST', '/stepping/v1/step', request, with_cookies=True)
        waited = 0.0
        while not result.get('complete'):
            if waited >= max_wait:
                await self.request('POST', '/stepping/v1/terminateStepping',
                                   {**request, 'sessionUuid': result['sessionUuid']}, cookie=cookie)
                raise ToolError("Stepping did not finish in time; try fewer records or a smaller stream")
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds
            try:
                result = await self.request('POST', '/stepping/v1/step', {**request, 'sessionUuid': result['sessionUuid']},
                                            cookie=cookie)
            except ToolError as e:
                if 'No stepping session found' not in str(e):
                    raise
                # Either the session's node dropped it (the step failed inside Stroom: seen, a pipeline property of
                # the wrong type, on one node as on several) or the follow-up reached another node.
                raise ToolError(
                    "A step outlasted Stroom's wait, and Stroom then had no session for it. Most often the pipeline "
                    "is at fault, not the network: a property Stroom can't use (describe_document shows them; set "
                    "it again with update_pipeline), or records too large to step (a JSON array whose pipeline has "
                    "jsonParser.addRootObject true steps as one record: set it false). Only when the pipeline steps "
                    "elsewhere: with several Stroom nodes, this server's STROOM_URL must reach Stroom through "
                    "something that keeps a client on one node (an ingress with cookie affinity, or a Service with "
                    "sessionAffinity: ClientIP), or raise STROOM_STEPPING_WAIT_MS.") from e
        return result
