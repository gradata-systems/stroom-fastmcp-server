import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from config import Settings
from security.audit import AuditMiddleware, configure_audit_log
from security.policy import AccessPolicy
from tools import explorer, feeds, pipelines, processing, resources, stepping, streams, templates, validation
from utils.stroom import StroomGateway
from utils.triage import ErrorRules

logger = logging.getLogger(__name__)

settings = Settings()
configure_audit_log(settings.audit_log_file)
rules = ErrorRules.load(settings.error_rules_file)
policy = AccessPolicy.load(settings.access_policy_file)


@asynccontextmanager
async def lifespan(server: FastMCP):
    stroom = StroomGateway(settings)
    try:
        yield {'stroom': stroom, 'rules': rules, 'policy': policy}
    finally:
        await stroom.close()


mcp = FastMCP(
    "stroom",
    lifespan=lifespan,
    auth=KeycloakAuthProvider(
        realm_url=settings.keycloak_realm_url,
        base_url=settings.public_base_url,
        audience=settings.keycloak_audience,
    ),
    middleware=[AuditMiddleware()],
)

for tool in (explorer.ALL_TOOLS + feeds.ALL_TOOLS + pipelines.ALL_TOOLS + templates.ALL_TOOLS
             + streams.ALL_TOOLS + stepping.ALL_TOOLS + processing.ALL_TOOLS + validation.ALL_TOOLS):
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
