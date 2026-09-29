import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from mcp.server.request_state import RequestStateSecurity
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from config import Settings
from security.audit import AuditMiddleware, configure_audit_log
from security.auth import oidc_auth, oidc_http_client
from security.policy import AccessPolicy
from main_tools import TOOL_MODULES
from tools import resources
from utils.consent import ConsentStore
from utils.elastic import ElasticTemplates
from utils.stroom import StroomGateway
from utils.triage import ErrorRules

logger = logging.getLogger(__name__)

settings = Settings()
configure_audit_log(settings.audit_log_file)
rules = ErrorRules.load(settings.error_rules_file)
policy = AccessPolicy.load(settings.access_policy_file)
consent = ConsentStore(settings.use_elicitation)


@asynccontextmanager
async def lifespan(server: FastMCP):
    stroom = StroomGateway(settings)
    elastic = ElasticTemplates(settings)
    try:
        yield {'stroom': stroom, 'elastic': elastic, 'rules': rules, 'policy': policy, 'consent': consent}
    finally:
        await stroom.close()
        await elastic.close()


LOCALHOST = ('127.0.0.1', 'localhost', '::1')
# Plain HTTP is only for a server that nothing but this machine can reach (local development).
loopback = settings.host in LOCALHOST
tls = bool(settings.tls_certfile and settings.tls_keyfile)
if bool(settings.tls_certfile) != bool(settings.tls_keyfile):
    raise SystemExit("Set both STROOM_MCP_TLS_CERTFILE and STROOM_MCP_TLS_KEYFILE")
if not (tls or settings.tls_terminated_upstream or loopback):
    raise SystemExit("TLS is required: set STROOM_MCP_TLS_CERTFILE and _TLS_KEYFILE, or "
                     "STROOM_MCP_TLS_TERMINATED_UPSTREAM=true when a proxy in front terminates TLS")

if settings.dev_no_auth:
    if settings.host not in LOCALHOST:
        raise SystemExit("dev_no_auth is only allowed when the server listens on localhost")
    if not settings.stroom_api_key:
        raise SystemExit("dev_no_auth needs STROOM_MCP_STROOM_API_KEY to call Stroom")
    logger.warning("Authentication is disabled (dev_no_auth); for local development only")
    auth = None
else:
    if not (settings.oidc_issuer_url and settings.oidc_audience and settings.public_base_url):
        raise SystemExit("Set STROOM_MCP_OIDC_ISSUER_URL, _OIDC_AUDIENCE and _PUBLIC_BASE_URL")
    if not (settings.public_base_url.startswith('https://') or loopback):
        raise SystemExit("STROOM_MCP_PUBLIC_BASE_URL must be an https:// URL")
    if settings.stroom_api_key:
        # Every call acts as the signed-in user; a shared key would hand its owner's rights to every caller.
        raise SystemExit("STROOM_MCP_STROOM_API_KEY is for dev_no_auth only; Stroom calls use the caller's token")
    # The HTTP client lives as long as the process; it fetches the provider's signing keys.
    auth = oidc_auth(settings, oidc_http_client(settings.oidc_ca_certs))

if any(len(k.get_secret_value()) < 32 for k in settings.request_state_keys):
    raise SystemExit("Each of STROOM_MCP_REQUEST_STATE_KEYS must be at least 32 characters")
request_state_security = (
    RequestStateSecurity(keys=[k.get_secret_value() for k in settings.request_state_keys], audience='stroom')
    if settings.request_state_keys else None)

mcp = FastMCP("stroom", lifespan=lifespan, auth=auth, middleware=[AuditMiddleware()],
              request_state_security=request_state_security)


@mcp.custom_route('/healthz', methods=['GET'], include_in_schema=False)
async def healthz(request: Request) -> Response:
    """Liveness and readiness probe. Unauthenticated, and deliberately independent of Stroom, the OIDC provider and
    Elasticsearch so an outage there doesn't restart every replica."""
    return PlainTextResponse('ok')


for tool in (t for module in TOOL_MODULES for t in module.ALL_TOOLS):
    mcp.tool(tool)
resources.register(mcp, settings.conventions_dir)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn_config = {}
    if tls:
        uvicorn_config = {'ssl_certfile': str(settings.tls_certfile), 'ssl_keyfile': str(settings.tls_keyfile)}
    else:
        logger.warning("Serving plain HTTP: TLS is terminated in front of the server, or this is local development")
    mcp.run(
        transport='http',
        host=settings.host,
        port=settings.port,
        uvicorn_config=uvicorn_config,
    )
