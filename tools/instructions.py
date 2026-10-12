"""Standing instructions: AGENTS docs in Stroom, the equivalent of an AGENTS.md for building pipelines.

Anyone who looks after an area of Stroom can write a Documentation doc named AGENTS (configurable) in a folder.
It holds standing instructions for the agent, such as how certain fields should be treated or how things are
named, and applies to that folder and everything below it. A doc directly under a root folder applies
everywhere. Where several apply, they are returned from the most general to the most specific, so the more
specific one reads last. The user's own request takes precedence, and no instruction can lift an approval or
the write guard: those are enforced by the server whatever a doc says.
"""
import re
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from utils.params import ONE_OR_MORE
from utils.stroom import body_text, gateway_from

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
        folders: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="Folders the work touches, e.g. 'System/Feeds/Events/Acme' (where the feed or pipeline "
                        "lives, or where it will be promoted to).")] = [],
        feeds: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Feed names the work touches; their folders are used.")] = [],
        docs: Annotated[list[dict[str, str]] | str, ONE_OR_MORE, Field(
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
            text = body_text(await stroom.get_doc('Documentation', ref['uuid']))
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


# A standing instruction the server applies, not only hands back (asked for by the user): shared XSLTs every XSLT of a
# stage imports, e.g. Raw Events to Events translations importing Stream-Meta-V1. In an AGENTS doc:
#   ## Required imports
#   - translation: Stream-Meta-V1
#   - indexing: Common-Index-V1, Lookups-V2
_REQUIRED = re.compile(r'^#{1,6}\s*required imports\s*$', re.I | re.M)
STAGES = {'translation': 'translation', 'translations': 'translation', 'events': 'translation',
          'raw events to events': 'translation', 'raw event to event': 'translation', 'records': 'records',
          'raw records to records': 'records', 'raw record to record': 'records', 'indexing': 'indexing',
          'reference': 'reference', 'reference data': 'reference', 'cef': 'cef', 'forwarding': 'cef'}


def required_imports(applying: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """The imports the applicable AGENTS docs require, by stage: [{'href', 'from'}], the doc's folder. A line naming no
    known stage is left out."""
    out: dict[str, list[dict[str, str]]] = {}
    for entry in applying:
        text = entry.get('instructions') or ''
        found = _REQUIRED.search(text)
        if not found:
            continue
        section = re.split(r'^#{1,6}\s', text[found.end():], maxsplit=1, flags=re.M)[0]
        for line in section.splitlines():
            m = re.match(r'^\s*[-*]\s*([^:]+):\s*(.+)$', line)
            stage = STAGES.get(m.group(1).strip().strip('`*').lower()) if m else None
            if not stage:
                continue
            for href in (h.strip().strip('`').strip() for h in m.group(2).split(',')):
                if href and href not in {r['href'] for r in out.get(stage, [])}:
                    out.setdefault(stage, []).append({'href': href, 'from': entry.get('folder', '')})
    return out


# Component templates an environment's index templates always or never compose (asked for by the user), applied by
# the server:
#   ## Component templates
#   - include: logs-ilm-90d, org-base
#   - exclude: legacy-base
_COMPONENTS = re.compile(r'^#{1,6}\s*component templates\s*$', re.I | re.M)
_COMPONENT_RULES = {'include': 'include', 'always': 'include', 'add': 'include', 'use': 'include',
                    'exclude': 'exclude', 'never': 'exclude', 'omit': 'exclude', 'drop': 'exclude'}


def component_rules(applying: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """{'include': [{'name', 'from'}], 'exclude': [...]} from the applicable AGENTS docs' Component templates
    sections, the most general first; a more specific doc's rule for a name overrides a more general one's."""
    rules: dict[str, tuple[str, str]] = {}
    for entry in applying:
        text = entry.get('instructions') or ''
        found = _COMPONENTS.search(text)
        if not found:
            continue
        section = re.split(r'^#{1,6}\s', text[found.end():], maxsplit=1, flags=re.M)[0]
        for line in section.splitlines():
            m = re.match(r'^\s*[-*]\s*([^:]+):\s*(.+)$', line)
            kind = _COMPONENT_RULES.get(m.group(1).strip().strip('`*').lower()) if m else None
            if not kind:
                continue
            for name in (n.strip().strip('`').strip() for n in m.group(2).split(',')):
                if name:
                    rules[name] = (kind, entry.get('folder', ''))
    out: dict[str, list[dict[str, str]]] = {'include': [], 'exclude': []}
    for name, (kind, folder) in rules.items():
        out[kind].append({'name': name, 'from': folder})
    return out


async def components_ruled(ctx: Context, pipeline_uuid: str) -> dict[str, list[dict[str, str]]]:
    """The Component templates rules of the AGENTS docs that apply where this pipeline is."""
    try:
        name = (await gateway_from(ctx).get_doc('Pipeline', pipeline_uuid)).get('name') or ''
        found = await applicable_instructions(ctx, docs=[{'type': 'Pipeline', 'uuid': pipeline_uuid, 'name': name}])
    except Exception:
        return {'include': [], 'exclude': []}
    return component_rules(found['instructions'])


async def imports_required(ctx: Context, stage: str, folders: list[str] = (), feeds: list[str] = ()) -> list[dict[str, str]]:
    """The shared XSLTs every XSLT of this stage must import where these folders or feeds are."""
    found = await applicable_instructions(ctx, folders=list(folders), feeds=list(feeds))
    return required_imports(found['instructions']).get(stage, [])


ALL_TOOLS = [get_instructions]
