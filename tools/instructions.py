"""Standing instructions: AGENTS docs in Stroom, the equivalent of an AGENTS.md for building pipelines.

Anyone who looks after an area of Stroom can write a Documentation doc named AGENTS (configurable) in a folder.
It holds standing instructions for the agent, such as how certain fields should be treated or how things are
named, and applies to that folder and everything below it. A doc directly under a root folder applies
everywhere. Where several apply, they are returned from the most general to the most specific, so the more
specific one reads last. The user's own request takes precedence, and no instruction can lift an approval or
the write guard: those are enforced by the server whatever a doc says.
"""
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from utils.stroom import gateway_from

MAX_CHARS = 20_000


def _parts(path: str | None) -> list[str]:
    return [p.strip() for p in (path or '').replace(' / ', '/').split('/') if p.strip()]


def applies(doc_folder: list[str], target: list[str]) -> bool:
    """A doc applies to its folder and below; one directly under a root folder applies everywhere."""
    return len(doc_folder) <= 1 or target[:len(doc_folder)] == doc_folder


async def _folder_of(ctx: Context, name: str, doc_type: str, uuid: str | None = None) -> list[str] | None:
    found = await gateway_from(ctx).find_documents(name, [doc_type], 50)
    for value in found.get('values') or []:
        ref = value['docRef']
        if ref.get('type') == doc_type and ref.get('name') == name and (uuid is None or ref.get('uuid') == uuid):
            return _parts(value.get('path'))
    return None


async def get_instructions(
        ctx: Context,
        folders: Annotated[list[str], Field(
            description="Folders the work touches, e.g. 'System/Feeds/Events/Keycloak' (where the feed or pipeline "
                        "lives, or where it will be promoted to).")] = [],
        feeds: Annotated[list[str], Field(description="Feed names the work touches; their folders are used.")] = [],
        docs: Annotated[list[dict[str, str]], Field(
            description="Documents the work touches, each {type, name, uuid?}, e.g. a pipeline being updated.")] = [],
) -> dict[str, Any]:
    """
    Standing instructions for building pipelines, from AGENTS Documentation docs in Stroom. Returns the docs
    that apply to the given folders, feeds or documents (a doc applies to its folder and below; one directly
    under a root folder applies everywhere), most general first, with their text. Follow them when drafting
    translations, mappings, indexes and names. The user's own request takes precedence, and no instruction
    lifts an approval or the write guard. Other AGENTS docs are listed by folder so you know they exist.
    """
    return await applicable_instructions(ctx, folders, feeds, docs)


async def applicable_instructions(ctx: Context, folders: list[str] = (), feeds: list[str] = (),
                                  docs: list[dict[str, str]] = ()) -> dict[str, Any]:
    """get_instructions, for other tools to hand back with their results."""
    stroom = gateway_from(ctx)
    name = stroom.settings.instructions_doc_name
    found = await stroom.find_documents(name, ['Documentation'], 200)
    candidates = [(v['docRef'], _parts(v.get('path'))) for v in found.get('values') or []
                  if v['docRef'].get('type') == 'Documentation' and v['docRef'].get('name', '').lower() == name.lower()]

    targets = [_parts(f) for f in folders]
    for feed in feeds:
        folder = await _folder_of(ctx, feed, 'Feed')
        if folder is not None:
            targets.append(folder)
    for doc in docs:
        folder = await _folder_of(ctx, doc.get('name', ''), doc.get('type', ''), doc.get('uuid'))
        if folder is not None:
            targets.append(folder)

    applying, elsewhere, used = [], [], 0
    for ref, folder in sorted(candidates, key=lambda c: (len(c[1]), '/'.join(c[1]))):
        scope = '/'.join(folder) or '(root)'
        if len(folder) <= 1 or any(applies(folder, t) for t in targets):
            text = (await stroom.get_doc('Documentation', ref['uuid'])).get('documentation') or ''
            room = max(0, MAX_CHARS - used)
            used += min(len(text), room)
            applying.append({'folder': scope, 'uuid': ref['uuid'],
                             'applies_to': 'everything' if len(folder) <= 1 else f'{scope} and below',
                             'instructions': text[:room] + ('\n[truncated]' if len(text) > room else '')})
        else:
            elsewhere.append({'folder': scope, 'uuid': ref['uuid']})
    return {
        'doc_name': name, 'instructions': applying, 'other_instruction_docs': elsewhere,
        'hint': ("Follow these standing instructions, the most specific last; the user's request takes precedence. "
                 "Call again with the folders, feeds or documents involved once you know them." if applying else
                 f"No {name} docs apply. Users can add one: a Documentation doc named {name} in a folder applies to that "
                 "folder and below."),
    }


ALL_TOOLS = [get_instructions]
