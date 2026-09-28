import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from config import Settings
from security.audit import AuditMiddleware, configure_audit_log
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


if settings.dev_no_auth:
    if settings.host not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit("dev_no_auth is only allowed when the server listens on localhost")
    logger.warning("Authentication is disabled (dev_no_auth); for local development only")
    auth = None
else:
    if not (settings.keycloak_realm_url and settings.keycloak_audience and settings.public_base_url):
        raise SystemExit("Set STROOM_MCP_KEYCLOAK_REALM_URL, _KEYCLOAK_AUDIENCE and _PUBLIC_BASE_URL")
    auth = KeycloakAuthProvider(
        realm_url=settings.keycloak_realm_url,
        base_url=settings.public_base_url,
        audience=settings.keycloak_audience,
    )

mcp = FastMCP("stroom", lifespan=lifespan, auth=auth, middleware=[AuditMiddleware()])

for tool in (t for module in TOOL_MODULES for t in module.ALL_TOOLS):
    mcp.tool(tool)
resources.register(mcp)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn_config = {}
    if settings.tls_certfile and settings.tls_keyfile:
        uvicorn_config = {'ssl_certfile': str(settings.tls_certfile), 'ssl_keyfile': str(settings.tls_keyfile)}
    else:
        logger.warning("TLS not configured; only run like this behind a TLS-terminating proxy")
    mcp.run(
        transport='http',
        host=settings.host,
        port=settings.port,
        uvicorn_config=uvicorn_config,
    )
