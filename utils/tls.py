import ssl
from pathlib import Path


def trust(ca_file: Path | None) -> ssl.SSLContext:
    """TLS context trusting the system CAs plus `ca_file`, if given (a private CA)."""
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=str(ca_file))
    return context
