"""List parameters take what models send: one value, a list as text, comma-separated text; the published schema says so."""
from fastmcp import Client, FastMCP

import main_tools
from tools import explorer, streams
from utils.params import _one_or_more


def test_one_or_more_reads_what_models_send():
    assert _one_or_more(None) is None and _one_or_more([1, 2]) == [1, 2]
    assert _one_or_more(15783601) == [15783601]
    assert _one_or_more("['Feed']") == ['Feed'] and _one_or_more('["Feed", "Pipeline"]') == ['Feed', 'Pipeline']
    assert _one_or_more('[1, 2]') == [1, 2] and _one_or_more('Feed') == ['Feed']
    assert _one_or_more('Feed, Pipeline') == ['Feed', 'Pipeline'] and _one_or_more('  ') == []
    assert _one_or_more('[{"field": "UserId", "value": "alice"}]') == [{'field': 'UserId', 'value': 'alice'}]
    assert _one_or_more('{"field": "UserId", "value": "alice"}') == [{'field': 'UserId', 'value': 'alice'}]


async def test_the_published_schema_accepts_a_string_for_every_list_parameter():
    server = FastMCP('test', lifespan=None)
    for module in main_tools.TOOL_MODULES:
        for tool in module.ALL_TOOLS:
            server.tool(tool)
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    narrow = []
    for name, tool in tools.items():
        for param, schema in (tool.input_schema.get('properties') or {}).items():
            options = schema.get('anyOf') or [schema]
            types = {o.get('type') for o in options}
            if 'array' in types and 'string' not in types:
                narrow.append(f'{name}.{param}')
    assert not narrow, narrow
    types = tools['find_documents'].input_schema['properties']['types']
    assert {o.get('type') for o in types['anyOf']} >= {'array', 'string', 'null'}


async def test_a_list_sent_as_text_reaches_the_tool_as_a_list():
    server = FastMCP('test', lifespan=None)
    server.tool(explorer.find_documents)
    server.tool(streams.summarise_streams)
    seen = {}

    async def fake_find(ctx, name='*', types=None, limit=100, content=None):
        seen['types'] = types
        return {'documents': []}
    import tools.explorer as ex
    original = ex.find_documents.__wrapped__ if hasattr(ex.find_documents, '__wrapped__') else None
    async with Client(server) as client:
        # The validator runs before the body: a string list literal arrives as a list of one type.
        result = await client.call_tool('find_documents', {'types': "['Feed']", 'name': 'FIREWALL-LOGS-V1'}, raise_on_error=False)
    # Without a Stroom to talk to the body fails later, but not on the arguments.
    assert 'must be array' not in str(result.content) and 'Input should be a valid list' not in str(result.content)


async def test_no_parameter_is_named_after_a_schema_keyword():
    # VS Code read a create_pipeline parameter called `properties` as the JSON Schema keyword and refused every
    # call with "must have required property 'properties'", although the schema required only `name`.
    # Keywords that give a schema its structure; `type` and `description` hold plain values and clients take them.
    keywords = {'properties', 'patternProperties', 'additionalProperties', 'required', 'items', 'anyOf', 'oneOf',
                'allOf', 'not', '$ref', '$defs', 'definitions'}
    server = FastMCP('test', lifespan=None)
    for module in main_tools.TOOL_MODULES:
        for tool in module.ALL_TOOLS:
            server.tool(tool)
    async with Client(server) as client:
        tools = await client.list_tools()
    clashes = [f'{t.name}.{p}' for t in tools for p in (t.input_schema.get('properties') or {}) if p in keywords]
    assert not clashes, clashes
