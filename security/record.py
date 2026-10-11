"""The build record: one Documentation doc, 'Build record', in each build's folder.

It holds what the server remembers about the build's documents between calls: which are working copies of production
documents (and of which), the code digests that stepped clean, passed index verification or gave Events that passed
check_events (what promotion checks for), and the feeds whose samples are files on the user's disk.

Earlier versions kept these as explorer tags (mcp-build-<build>, mcp-copy-of-<uuid>, mcp-stepped-<digest>, ...). Tags
are what people filter the explorer by, and those grew Stroom's tag list with every build and every change of code;
only mcp-generated and mcp-managed (and mcp-kept-mapping on XSLTs) are tags now. Like the tags, the record is seen by
every replica and survives restarts. Two replicas writing it at once lose nothing: Stroom refuses a save of a doc
changed since it was read, and the change is made again on the newer doc. A build's old tags are folded into its
record the first time it is read, then removed.
"""
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError

from utils.stroom import body_text, set_body_text

logger = logging.getLogger(__name__)

NAME = 'Build record'
KINDS = ('stepped', 'verified', 'validated')
FENCE = re.compile(r'```json build-record\n(.*?)\n```', re.S)
# The tags earlier versions kept these in, and the entry each one fills (None: the build, now the folder it is in).
LEGACY = {'mcp-copy-of-': 'copy_of', 'mcp-stepped-': 'stepped', 'mcp-verified-': 'verified',
          'mcp-validated-': 'validated', 'mcp-sample-files': 'sample_files', 'mcp-build-': None}
_ATTEMPTS = 5


def empty() -> dict[str, Any]:
    return {'docs': {}}


def parse(text: str | None) -> dict[str, Any]:
    match = FENCE.search(text or '')
    if not match:
        return empty()
    try:
        state = json.loads(match.group(1))
    except ValueError:
        return empty()
    return state if isinstance(state.get('docs'), dict) else empty()


def legacy_tags(tags: list[str]) -> list[str]:
    return [t for t in tags if any(t.startswith(prefix) for prefix in LEGACY)]


def fold(entry: dict[str, Any], tag: str) -> None:
    """Put what an old tag recorded into the doc's entry."""
    prefix = next(p for p in LEGACY if tag.startswith(p))
    key = LEGACY[prefix]
    if key == 'copy_of':
        entry.setdefault('copy_of', tag[len(prefix):])
    elif key == 'sample_files':
        entry['sample_files'] = True
    elif key in KINDS:
        # 'mcp-stepped-<digest>', or the older 'mcp-stepped-<UTC time>-<digest>'.
        digest = tag.rsplit('-', 1)[-1]
        entry[key] = sorted({*entry.get(key, []), digest})


def render(build: str, state: dict[str, Any]) -> str:
    def digests(entry: dict[str, Any], kind: str) -> str:
        return ', '.join(d[:8] for d in entry.get(kind, [])) or '-'

    lines = [f"# {NAME}: {build}", '',
             "What the MCP server remembers about this build's documents: which are working copies of production "
             "documents, and the code (by digest) that stepped clean, passed index verification, or gave Events that "
             "passed check_events, which promotion checks for. The server writes it; don't edit it. Don't move or "
             "rename it or anything else in this folder either: the server finds a build's documents by the folder "
             "they are in. Promotion removes it.", '',
             '| Document | Type | Working copy of | Stepped clean | Index verified | Events checked | Sample files |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for uuid, entry in sorted(state['docs'].items(), key=lambda kv: (kv[1].get('type') or '', kv[1].get('name') or '')):
        lines.append(f"| {entry.get('name') or uuid} | {entry.get('type') or ''} | {entry.get('copy_of') or '-'} | "
                     f"{digests(entry, 'stepped')} | {digests(entry, 'verified')} | {digests(entry, 'validated')} | "
                     f"{'yes' if entry.get('sample_files') else '-'} |")
    lines += ['', '## Record', '', 'Read by the server; do not edit.', '',
              '```json build-record', json.dumps(state, indent=1, sort_keys=True), '```', '']
    return '\n'.join(lines)


def _record_doc(contents: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((d for d in contents if d['type'] == 'Documentation' and d['name'] == NAME), None)


def _ref(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: doc[k] for k in ('type', 'uuid', 'name')}


async def load(guard: Any, build: str) -> dict[str, Any]:
    """The build's record ({'docs': {uuid: entry}}), empty when it has none; old tags folded in first."""
    contents = await guard.folder_contents(build, with_record=True)
    found = _record_doc(contents)
    old = {d['uuid']: (d, legacy_tags(d['tags'])) for d in contents if legacy_tags(d['tags'])}
    if old:
        def migrate(state: dict[str, Any]) -> None:
            for uuid, (doc, tags) in old.items():
                entry = _entry(state, doc)
                for tag in tags:
                    fold(entry, tag)
        state = await _save(guard, build, found, migrate)
        for doc, tags in old.values():
            await guard.untag([_ref(doc)], tags)
        return state
    if found is None:
        return empty()
    return parse(body_text(await guard.stroom.get_doc('Documentation', found['uuid'])))


async def entry(guard: Any, build: str | None, uuid: str) -> dict[str, Any]:
    """One document's entry in its build's record ({} when it has none, or isn't in a build)."""
    if not build:
        return {}
    return (await load(guard, build))['docs'].get(uuid) or {}


async def update(guard: Any, build: str, doc: dict[str, Any], change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Change one document's entry (change(entry) edits it in place) and save the record."""
    def apply(state: dict[str, Any]) -> None:
        change(_entry(state, doc))
    found = _record_doc(await guard.folder_contents(build, with_record=True))
    return await _save(guard, build, found, apply)


async def forget(guard: Any, build: str, uuids: list[str]) -> None:
    """Drop documents' entries (promoted out of the build); the record goes once it holds nothing."""
    found = _record_doc(await guard.folder_contents(build, with_record=True))
    if found is None:
        return
    state = parse(body_text(await guard.stroom.get_doc('Documentation', found['uuid'])))
    if not set(state['docs']) - set(uuids):
        await guard.stroom.request('DELETE', '/explorer/v2/delete', {'docRefs': [_ref(found)]})
        return
    await _save(guard, build, found, lambda s: [s['docs'].pop(u, None) for u in uuids])


def _entry(state: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    entry = state['docs'].setdefault(doc['uuid'], {})
    entry.update({k: doc[k] for k in ('type', 'name') if doc.get(k)})
    return entry


async def _save(guard: Any, build: str, found: dict[str, Any] | None,
                change: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    stroom = guard.stroom
    for attempt in range(_ATTEMPTS):
        try:
            if found is None:
                async def write(ref: dict[str, Any]) -> dict[str, Any]:
                    doc = await stroom.get_doc('Documentation', ref['uuid'])
                    state = empty()
                    change(state)
                    set_body_text(doc, render(build, state))
                    await stroom.put_doc(doc)
                    return state
                return await guard.create_filled('Documentation', NAME, build, write, record=True)
            doc = await stroom.get_doc('Documentation', found['uuid'])
            state = parse(body_text(doc))
            change(state)
            set_body_text(doc, render(build, state))
            await stroom.put_doc(doc)
            return state
        except ToolError as e:
            # Another replica saved or created it since it was read: do the change again on what it saved.
            if attempt == _ATTEMPTS - 1 or not ('modified by another user' in str(e) or 'already in build' in str(e)):
                raise
            logger.info("Build record of %s changed while writing it (%s); again", build, str(e)[:120])
            found = _record_doc(await guard.folder_contents(build, with_record=True))
    raise AssertionError('unreachable')
