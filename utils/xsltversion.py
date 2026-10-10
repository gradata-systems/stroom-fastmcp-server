"""The version history of each XSLT this server saves, kept in its description (the XSLT's Documentation tab in
Stroom), not in the code.

One row per version: its number, date, author, whether the agent made it (through this server) or someone changed
the XSLT by hand (in Stroom's editor), what changed, and for an agent change the server's version, the client and
the model the agent said it is, and the digest of the code it describes.

Within a build an XSLT is saved many times; the user asked for one row for the lot, written when the build is done.
So each save adds its change to the pending changes kept in the description, and promote_build turns them into one
row. Until then the table previews it: the row (or rows, an edit made by hand before the build too) promotion will
write, marked Unreleased instead of numbered, rewritten each save. The user asked for that, to see what a build has
done before it is released, and for the history to stay out of the code (a comment there changed the code, and every
check comparing code had to look past it); an XSLT saved with such a comment has it moved here when next saved.

    ## Version history

    | Version | Date | Author | How | Change | By | Digest |
    | --- | --- | --- | --- | --- | --- | --- |
    | 1 | 2026-10-10 | peter.kimberley | agent | Created from its mapping | stroom-mcp 0.16.42, Visual Studio Code 1.141 | #3f2a9c1e |
    | 2 | 2026-10-12 | jane.doe | by hand | Changed outside the agent (Stroom's editor) |  | #77ab01c2 |
    | Unreleased | 2026-10-14 | peter.kimberley | agent | Rule for VPN events | stroom-mcp 0.16.45 | #9b10d4e3 |
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

HEADING = '## Version history'
_INTRO = ("Each version of this XSLT: changes made by the agent through stroom-mcp, and changes made by hand in Stroom's "
          "editor. Unreleased rows are a build's changes so far, recorded as a version when the build is promoted.")
# The table ends where the server's part of the description begins (its note, its hidden blocks), or at the end.
_SECTION = re.compile(r'\n*' + re.escape(HEADING) + r'\n.*?(?=\n\*Below, hidden, is what stroom-mcp|'
                      r'\n<!-- stroom-mcp [a-z ]+: kept by the server|\n--- stroom-mcp |\Z)', re.S)
_ROW = re.compile(r'^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*#?([0-9a-f]*)\s*\|\s*$')
# The comment block XSLTs carried before (0.16.43 to 0.16.45): read, to move it here, and left out of the code.
_BLOCK = re.compile(r'[ \t]*<!-- stroom-mcp version history\n(.*?)\nend of version history -->[ \t]*\n?', re.S)
_DECL = re.compile(r'^\s*<\?xml[^>]*\?>[ \t]*\n?')
PENDING = 'pending changes'      # a hidden block of the description (utils/mappingstore)


def strip(code: str) -> str:
    """The XSLT without the version history comment an earlier version put in it."""
    return _BLOCK.sub('', code or '', count=1)


def _digest(code: str) -> str:
    body = re.sub(r'>\s+<', '><', _DECL.sub('', strip(code))).strip()
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:8]


def _comment_rows(code: str) -> list[dict[str, str]]:
    """The rows of a version history comment in the code (as 0.16.43 to 0.16.45 wrote it)."""
    found = _BLOCK.search(code or '')
    out = []
    for line in (found.group(1).splitlines() if found else []):
        parts = [p.strip() for p in line.strip().split('|')]
        if len(parts) >= 7 and re.fullmatch(r'v\d+', parts[0]):
            out.append({'version': parts[0][1:], 'date': parts[1], 'author': parts[2], 'how': parts[3],
                        'change': parts[4], 'by': parts[5], 'digest': parts[6].lstrip('#')})
    return out


def history(description: str | None) -> list[dict[str, str]]:
    """The released versions, oldest first (not the Unreleased preview)."""
    found = _SECTION.search(description or '')
    out = []
    for line in (found.group(0).splitlines() if found else []):
        m = _ROW.match(line.strip())
        if m:
            out.append({'version': m.group(1), 'date': m.group(2).strip(), 'author': m.group(3).strip(),
                        'how': m.group(4).strip(), 'change': m.group(5).strip(), 'by': m.group(6).strip(),
                        'digest': m.group(7)})
    return out


def _clean(text: str) -> str:
    """Fit for a table cell: one line, no column separators."""
    return re.sub(r'\s+', ' ', str(text or '')).replace('|', '/').strip()[:600]


def _section(rows: list[dict[str, str]]) -> str:
    lines = [HEADING, '', _INTRO, '', '| Version | Date | Author | How | Change | By | Digest |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    lines += [f"| {r['version']} | {r['date']} | {_clean(r['author']) or '-'} | {r['how']} | {_clean(r['change']) or '-'} | "
              f"{_clean(r.get('by', ''))} | #{r['digest']} |" for r in rows]
    return '\n'.join(lines)


def _with_section(description: str | None, rows: list[dict[str, str]]) -> str:
    """The description with the history table (after its own text, before the server's blocks), or without one."""
    from utils.mappingstore import SERVER_PART
    text = _SECTION.sub('', description or '').strip()
    if not rows:
        return text
    at = SERVER_PART.search(text)
    head, tail = (text, '') if not at else (text[:at.start()], text[at.start():])
    return '\n\n'.join(part.strip() for part in (head, _section(rows), tail) if part.strip())


def adopt(description: str | None, previous_code: str | None) -> str:
    """The description with the history an earlier version kept in the code moved into it, when it has none."""
    if history(description) or not _comment_rows(previous_code or ''):
        return description or ''
    return _with_section(description, _comment_rows(previous_code or ''))


def pending_of(description: str | None) -> dict[str, Any] | None:
    from utils.mappingstore import read_block
    found = read_block(description, PENDING)
    return found if isinstance(found, dict) else None


def last_saved(description: str | None, code: str | None) -> dict[str, str] | None:
    """What the server recorded of its last save of this code: the newest pending entry with a digest, else the
    newest version ({'digest', 'by'}); None when nothing was recorded (saved before digests were)."""
    entries = [e for e in (pending_of(description) or {}).get('entries') or [] if e.get('digest')]
    if entries:
        return {'digest': entries[-1]['digest'], 'by': entries[-1].get('by') or ''}
    rows = history(description) or _comment_rows(code or '')
    return {'digest': rows[-1]['digest'], 'by': rows[-1].get('by') or ''} if rows else None


def untouched(description: str | None, code: str | None) -> bool | None:
    """Whether the code is exactly what the server last saved (True), changed since (False), or unknown (None)."""
    saved = last_saved(description, code)
    return None if saved is None else saved['digest'] == _digest(code or '')


def without_pending(description: str | None) -> str:
    from utils.mappingstore import without_block
    return without_block(description, PENDING)


def with_pending(description: str | None, previous: dict[str, Any] | None, author: str, change: str, by: str,
                 now: datetime | None = None, code: str | None = None) -> str:
    """The description with this change added to the build's pending changes. The first also keeps what the code
    was before the build touched it, so an edit made by hand before then is recorded too."""
    now = now or datetime.now(timezone.utc)
    pending = pending_of(description) or {'entries': []}
    if 'base' not in pending:
        old = (previous or {}).get('data') or ''
        pending['base'] = {'digest': _digest(old), 'user': (previous or {}).get('updateUser'),
                           'time_ms': (previous or {}).get('updateTimeMs')} if old else None
    pending['entries'].append({'date': now.strftime('%Y-%m-%d'), 'author': author or '-', 'change': change, 'by': by,
                               **({'digest': _digest(code)} if code is not None else {})})
    from utils.mappingstore import with_block
    return with_block(without_pending(description), PENDING, pending)


def _distinct(values: list[str | None]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _coming(released: list[dict[str, str]], pending: dict[str, Any], code: str, now: datetime) -> list[dict[str, str]]:
    """The rows promotion writes for the pending changes: an edit made by hand before them, then the build's."""
    out: list[dict[str, str]] = []
    base = pending.get('base')
    if base and (not released or released[-1]['digest'] != base['digest']):
        when = base.get('time_ms')
        out.append({'version': str(len(released) + 1),
                    'date': datetime.fromtimestamp(when / 1000, timezone.utc).strftime('%Y-%m-%d') if when else '?',
                    'author': base.get('user') or '?', 'how': 'by hand',
                    'change': "Changed outside the agent (Stroom's editor)" if released
                    else 'Written before the agent kept a history', 'by': '', 'digest': base['digest']})
    entries = pending.get('entries') or []
    out.append({'version': str(len(released) + len(out) + 1), 'date': now.strftime('%Y-%m-%d'),
                'author': ', '.join(_distinct([e.get('author') for e in entries])) or '-', 'how': 'agent',
                'change': '; '.join(_distinct([e.get('change') for e in entries])) or '-',
                'by': '; '.join(_distinct([e.get('by') for e in entries])), 'digest': _digest(code)})
    return out


def preview(description: str | None, code: str, now: datetime | None = None) -> str:
    """The description with its released versions and, while a build's changes are pending, the rows promotion
    will write for them, each marked Unreleased."""
    released = history(description)
    pending = pending_of(description)
    if not pending or not pending.get('entries'):
        return _with_section(description, released)
    coming = _coming(released, pending, code, now or datetime.now(timezone.utc))
    return _with_section(description, released + [{**r, 'version': 'Unreleased'} for r in coming])


def consolidate(description: str | None, code: str, now: datetime | None = None) -> str:
    """The description with the build's pending changes as released versions (one row, and an edit made by hand
    before them as its own), and nothing pending."""
    released = history(description)
    pending = pending_of(description)
    if pending and pending.get('entries'):
        released += _coming(released, pending, code, now or datetime.now(timezone.utc))
    return _with_section(without_pending(description), released)


def agent_line(ctx: Any) -> str:
    """Who made an agent change: this server's version, the client (from the MCP handshake) and the model the
    agent said it is (remembered for the session once given)."""
    from utils.version import SERVER_VERSION
    parts = [f'stroom-mcp {SERVER_VERSION}']
    try:
        info = ctx.session.client_params.clientInfo
        parts.append(f"{info.name} {info.version}".strip())
    except Exception:
        pass
    model = remembered_model(ctx)
    if model:
        parts.append(f"model {model} (as the agent said)")
    return ', '.join(parts)


def remember_model(ctx: Any, model: str | None) -> None:
    if not model:
        return
    try:
        from tools.plan import _user
        ctx.lifespan_context.setdefault('agent_models', {})[_user(ctx)] = model.strip()[:80]
    except Exception:
        pass


def remembered_model(ctx: Any) -> str | None:
    try:
        from tools.plan import _user
        return ctx.lifespan_context.get('agent_models', {}).get(_user(ctx))
    except Exception:
        return None
