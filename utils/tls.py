import ssl
from pathlib import Path


def trust(ca_file: Path | None) -> ssl.SSLContext:
    """TLS context trusting the system CAs plus `ca_file`, if given (a private CA)."""
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=str(ca_file))
    return context


def stroom_trust(settings) -> ssl.SSLContext | bool:
    """How Stroom's certificate is checked: against the system CAs plus stroom_ca_certs, or not at all when
    stroom_tls_verify is off (a development Stroom with a self-signed certificate)."""
    return trust(settings.stroom_ca_certs) if settings.stroom_tls_verify else False
