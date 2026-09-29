"""Call one tool directly, without the MCP transport or Keycloak, for development.

    uv run python dev/try_tool.py find_pipeline_templates stage=translation
    uv run python dev/try_tool.py --live describe_pipeline uuid=...      # live instance, read-only tools only

Arguments are name=value; values are parsed as JSON when they can be (numbers, lists, dicts).
Local: http://127.0.0.1:18080 with the admin API key in dev/stroom/.env.
Live:  STROOM_URL / STROOM_API_KEY from .ai/secrets.
"""
import asyncio
import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from security.policy import AccessPolicy  # noqa: E402
from main_tools import TOOL_MODULES  # noqa: E402
from utils.consent import ConsentStore  # noqa: E402
from utils.elastic import ElasticTemplates  # noqa: E402
from utils.stroom import StroomGateway  # noqa: E402
from utils.triage import ErrorRules  # noqa: E402

TOOLS = {t.__name__: t for m in TOOL_MODULES for t in m.ALL_TOOLS}
# Tools that change Stroom; never run against the live instance from here.
WRITE_TOOLS = {t.__name__ for m in TOOL_MODULES if m.__name__.endswith(('_writes', 'translation', 'builds'))
               for t in m.ALL_TOOLS} | {'create_feed', 'upload_sample', 'record_source_notes', 'put_index_template',
                                        'set_index_fields', 'create_index_doc', 'create_indexing_pipeline',
                                        'create_verification_dashboard'}


CONSENT = ConsentStore(use_elicitation=False)


def env(path: Path) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in (line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)}


def parse(value: str):
    try:
        return json.loads(value)
    except ValueError:
        return value


async def main(argv: list[str]):
    live = '--live' in argv
    argv = [a for a in argv if a != '--live']
    name, args = argv[0], dict(a.split('=', 1) for a in argv[1:])
    if live and name in WRITE_TOOLS:
        raise SystemExit(f'{name} changes Stroom; run it against the local stack only')
    if live and 'build' in args:
        # e.g. survey_feed writes its survey doc into the build.
        raise SystemExit(f'{name} with a build writes to Stroom; leave build out against the live instance')
    if live:
        secrets = env(ROOT / '.ai' / 'secrets')
        url, key = secrets['STROOM_URL'], secrets['STROOM_API_KEY']
    else:
        url, key = 'http://127.0.0.1:18080', env(ROOT / 'dev' / 'stroom' / '.env')['STROOM_ADMIN_API_KEY']
    settings = Settings(_env_file=None, stroom_url=url, dev_no_auth=True, stroom_api_key=key, keycloak_realm_url='-',
                        keycloak_audience='-', public_base_url='-')
    gateway = StroomGateway(settings)
    ctx = SimpleNamespace(lifespan_context={
        'stroom': gateway, 'rules': ErrorRules.load(ROOT / 'error_rules.yaml'),
        'policy': AccessPolicy.load(ROOT / 'access_policy.yaml'),
        # No elicitation here: gated tools return an id; pass it back as confirmation_id / approval_id.
        'consent': CONSENT, 'elastic': ElasticTemplates(settings)})
    tool = TOOLS[name]
    kwargs = {k: parse(v) for k, v in args.items()}
    if 'ctx' in inspect.signature(tool).parameters:
        kwargs['ctx'] = ctx
    try:
        print(json.dumps(await tool(**kwargs), indent=1, default=str))
    finally:
        await gateway.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1:]))
