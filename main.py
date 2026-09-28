import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider

from config import Settings
from security.audit import AuditMiddleware, configure_audit_log
from tools import explorer, pipelines
from utils.stroom import StroomGateway

logger = logging.getLogger(__name__)

settings = Settings()
configure_audit_log(settings.audit_log_file)


@asynccontextmanager
async def lifespan(server: FastMCP):
    stroom = StroomGateway(settings)
    try:
        yield {'stroom': stroom}
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

for tool in explorer.ALL_TOOLS + pipelines.ALL_TOOLS:
    mcp.tool(tool)


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
