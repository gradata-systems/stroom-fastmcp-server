"""Tool annotations and _meta, for clients that decide from them whether to ask first and which tools to offer together
(asked for by the user, for Stroom's Ask Stroom AI)."""
from fastmcp import Client, FastMCP

import main_tools
from tools.toolmeta import GROUPS, TOOLS, registration


def test_every_tool_is_classified_and_nothing_else():
    names = {t.__name__ for m in main_tools.TOOL_MODULES for t in m.ALL_TOOLS}
    assert names == set(TOOLS), (sorted(names - set(TOOLS)), sorted(set(TOOLS) - names))
    assert {group for group, *_ in TOOLS.values()} == set(GROUPS)
    # Nothing both read-only and destructive; whatever starts processing or touches production confirms in a form.
    assert not any(read_only and destructive for _, read_only, destructive, _ in TOOLS.values())
    assert all(confirms for _, _, destructive, confirms in TOOLS.values() if destructive)


async def test_clients_see_the_annotations_and_groups():
    server = FastMCP('t')
    for module in main_tools.TOOL_MODULES:
        for tool in module.ALL_TOOLS:
            server.tool(tool, **registration(tool.__name__))
    async with Client(server) as client:
        listed = {t.name: t for t in await client.list_tools()}
    assert listed['find_documents'].annotations.read_only_hint is True
    assert listed['find_documents'].annotations.open_world_hint is False
    promote = listed['promote_build']
    assert promote.annotations.read_only_hint is False and promote.annotations.destructive_hint is True
    assert promote.meta['group'] == 'builds' and promote.meta['confirms'] is True and promote.meta['groupSummary']
    # A tool that records its result in the build is not read-only, nor destructive.
    step = listed['step_sample']
    assert step.annotations.read_only_hint is False and step.annotations.destructive_hint is False
    assert 'confirms' not in step.meta
