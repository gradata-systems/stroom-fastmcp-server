from pathlib import Path

from typing import Annotated

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration, read from STROOM_MCP_* environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_prefix='STROOM_MCP_', env_file='.env', extra='ignore')

    # Stroom. Every call acts as the user who asked: the caller's Keycloak access token is forwarded, so
    # its aud claim must include stroom_audience as well as keycloak_audience. The API key is only for
    # development with dev_no_auth (or tools run directly), where there is no caller token.
    stroom_url: str
    stroom_audience: str = 'stroom'
    # Base URL of the Stroom UI for links shown to users (defaults to stroom_url).
    stroom_ui_url: str | None = None
    stroom_api_key: SecretStr | None = None
    # CA that signed Stroom's HTTPS certificate, trusted in addition to the system CAs.
    stroom_ca_certs: Path | None = None
    stroom_request_timeout: float = 60.0
    # Path of the data receiver, relative to stroom_url.
    datafeed_path: str = '/stroom/datafeed'

    # Standing instructions: Documentation docs with this name apply to their folder and below.
    instructions_doc_name: str = 'AGENTS'
    # Where the agent builds everything before it is promoted.
    workspace_folder: str = 'MCP Workspace'
    event_logging_version: str = '3.5.2'
    # Ask the user through MCP elicitation when the client supports it.
    use_elicitation: bool = True
    # Reprocessing during development (pipelines this server built): streams per call, and the filter's
    # concurrent task limit. Reprocessing with production pipelines stays with the user.
    max_reprocess_streams: int = 10
    reprocess_max_tasks: int = 1
    # Task limit on processor filters over the build's sample streams.
    sample_max_tasks: int = 1
    # Task limit on feed-wide processor filters.
    max_feed_filter_tasks: int = 2

    # Upper bound on serialized tool output, to protect the model's context window.
    max_response_chars: int = 100_000
    max_stream_chars: int = 20_000
    # Records stepped per step_sample call before stopping.
    max_sample_records: int = 500
    # Error triage rules (see error_rules.yaml).
    error_rules_file: Path = Path('error_rules.yaml')
    # Template pipeline sources (and, later, write scope).
    access_policy_file: Path = Path('access_policy.yaml')

    # Elasticsearch, for index templates only (optional; Stroom-side indexing works without it).
    es_url: str | None = None
    es_api_key: SecretStr | None = None
    # CA that signed Elasticsearch's HTTPS certificate, trusted in addition to the system CAs.
    es_ca_certs: Path | None = None
    es_template_patterns: list[str] = ['stroom-*']
    # Field convention profiles (*.yaml) and the one used when the user names none.
    conventions_dir: Path = Path('conventions')
    default_convention: str | None = None

    # Audit trail as JSON lines; stdout when unset (suits Kubernetes log shipping).
    audit_log_file: Path | None = None

    # Keycloak (OAuth2 authorization server)
    keycloak_realm_url: str = ''
    keycloak_audience: str = ''
    # Algorithm Keycloak signs access tokens with.
    keycloak_token_algorithm: str = 'RS256'
    # CA that signed Keycloak's HTTPS certificate, trusted in addition to the system CAs for fetching the
    # realm's signing keys. Needed when Keycloak uses a private CA.
    keycloak_ca_certs: Path | None = None
    # External URL clients use to reach the server (https://...). Used in OAuth metadata.
    public_base_url: str = ''
    # Keys that seal the state carried between rounds of a form (confirmations and approvals), each at
    # least 32 characters, comma-separated; the first seals, all unseal (for rotation). Unset: a
    # per-process key, which is fine for one replica. Every replica must share them when there are several.
    request_state_keys: Annotated[list[SecretStr], NoDecode] = []
    # Development only: no authentication, and Stroom is called with stroom_api_key. Refused unless the
    # server listens on localhost.
    dev_no_auth: bool = False

    # HTTP listener. The server terminates TLS itself and refuses to start without a certificate, unless
    # tls_terminated_upstream says a proxy in front does, or it listens on localhost (development).
    host: str = '0.0.0.0'
    port: int = 8000
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    tls_terminated_upstream: bool = False

    @field_validator('request_state_keys', mode='before')
    @classmethod
    def _split_keys(cls, value):
        if isinstance(value, str):
            return [k.strip() for k in value.split(',') if k.strip()]
        return value
