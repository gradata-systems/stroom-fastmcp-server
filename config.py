from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration, read from STROOM_MCP_* environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_prefix='STROOM_MCP_', env_file='.env', extra='ignore')

    # Stroom. Until Keycloak token exchange is in place, the server calls Stroom with one API key;
    # Stroom then applies that key's owner's permissions to every caller.
    stroom_url: str
    stroom_api_key: SecretStr
    stroom_ca_certs: Path | None = None
    stroom_request_timeout: float = 60.0
    # Path of the data receiver, relative to stroom_url.
    datafeed_path: str = '/stroom/datafeed'

    # Where the agent builds everything before it is promoted.
    workspace_folder: str = 'MCP Workspace'
    event_logging_version: str = '3.5.2'
    # Ask the user through MCP elicitation when the client supports it.
    use_elicitation: bool = True
    # Most raw streams reprocess_streams will take in one call.
    max_reprocess_streams: int = 20
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

    # Audit trail as JSON lines; stdout when unset (suits Kubernetes log shipping).
    audit_log_file: Path | None = None

    # Keycloak (OAuth2 authorization server)
    keycloak_realm_url: str
    keycloak_audience: str
    public_base_url: str

    # HTTP listener. Leave TLS unset only when TLS is terminated in front of the server.
    host: str = '0.0.0.0'
    port: int = 8000
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
