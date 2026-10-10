"""The version history comment at the start of each XSLT this server saves.

One line per version: its number, date, author, whether the agent made it (through this server) or someone changed
the XSLT by hand (in Stroom's editor), what changed, and for an agent change the server's version, the client and
the model the agent said it is.

Within a build an XSLT is saved many times; the user asked for one line for the lot, written when the build is
done. So each save carries the history over unchanged and adds its change to the pending changes kept in the XSLT's
description; promote_build turns them into one line. The first save in a build also keeps what the code was before
it (its digest, and who last changed it and when, as Stroom records), so an edit made by hand since the history's
last line gets its own line first.

    <!-- stroom-mcp version history
     v1 | 2026-10-10 | peter.kimberley | agent | Created from its mapping | stroom-mcp 0.16.42, Visual Studio Code 1.141, model claude-haiku-5-5 (as the agent said) | #3f2a9c1e
     v2 | 2026-10-12 | jane.doe | by hand | Changed outside the agent (Stroom's editor) |  | #77ab01c2
    end of version history -->
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

_BLOCK = re.compile(r'[ \t]*<!-- stroom-mcp version history\n(.*?)\nend of version history -->[ \t]*\n?', re.S)
_DECL = re.compile(r'^\s*<\?xml[^>]*\?>[ \t]*\n?')
_FIELD = ' | '
PENDING_START = '--- stroom-mcp pending changes (recorded in the version history when the build is promoted) ---'
PENDING_END = '--- end of stroom-mcp pending changes ---'
_PENDING = re.compile(r'\n*' + re.escape(PENDING_START) + r'\n(.*?)\n' + re.escape(PENDING_END) + r'\n*', re.S)


def strip(code: str) -> str:
    """The XSLT without its version history."""
    return _BLOCK.sub('', code or '', count=1)


def _digest(code: str) -> str:
    body = re.sub(r'>\s+<', '><', _DECL.sub('', strip(code))).strip()
    return hashlib.sha256(body.encode('utf-8')).hexdigest()[:8]


def rows(code: str) -> list[dict[str, str]]:
    found = _BLOCK.search(code or '')
    out = []
    for line in (found.group(1).splitlines() if found else []):
        parts = [p.strip() for p in line.strip().split('|')]
        if len(parts) >= 7 and parts[0].startswith('v'):
            out.append({'version': parts[0][1:], 'date': parts[1], 'author': parts[2], 'how': parts[3],
                        'change': parts[4], 'by': parts[5], 'digest': parts[6].lstrip('#')})
    return out


def _clean(text: str) -> str:
    """Fit for a line of an XML comment: no '--', no line breaks, no field separators."""
    text = re.sub(r'\s+', ' ', str(text or '')).replace('|', '/')
    while '--' in text:
        text = text.replace('--', '-')
    return text.strip(' -')[:600]


def _line(row: dict[str, str]) -> str:
    return ' v' + _FIELD.join([row['version'], row['date'], _clean(row['author']) or '-', row['how'],
                                _clean(row['change']) or '-', _clean(row.get('by', '')), '#' + row['digest']])


def _with_block(code: str, history: list[dict[str, str]]) -> str:
    body = strip(code)
    if not history:
        return body
    block = '<!-- stroom-mcp version history\n' + '\n'.join(_line(r) for r in history) + '\nend of version history -->\n'
    decl = _DECL.match(body)
    return (decl.group(0).rstrip() + '\n' + block + body[decl.end():]) if decl else block + body


def carry(code: str, previous_code: str | None) -> str:
    """New code with the previous code's version history, unchanged."""
    return _with_block(code, rows(previous_code or ''))


def pending_of(description: str | None) -> dict[str, Any] | None:
    found = _PENDING.search(description or '')
    if not found:
        return None
    try:
        return json.loads(found.group(1))
    except ValueError:
        return None


def last_saved(description: str | None, code: str | None) -> dict[str, str] | None:
    """What the server recorded of its last save of this code: the newest pending entry with a digest, else the
    newest version history line ({'digest', 'by'}); None when nothing was recorded (saved before digests were)."""
    entries = [e for e in (pending_of(description) or {}).get('entries') or [] if e.get('digest')]
    if entries:
        return {'digest': entries[-1]['digest'], 'by': entries[-1].get('by') or ''}
    history = rows(code or '')
    return {'digest': history[-1]['digest'], 'by': history[-1].get('by') or ''} if history else None


def untouched(description: str | None, code: str | None) -> bool | None:
    """Whether the code is exactly what the server last saved (True), changed since (False), or unknown (None)."""
    saved = last_saved(description, code)
    return None if saved is None else saved['digest'] == _digest(code or '')


def without_pending(description: str | None) -> str:
    return _PENDING.sub('\n\n', description or '').strip()


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
    return (without_pending(description) + '\n\n' + PENDING_START + '\n' + json.dumps(pending, indent=1) + '\n'
            + PENDING_END).strip()


def _distinct(values: list[str | None]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def consolidate(code: str, pending: dict[str, Any], now: datetime | None = None) -> str:
    """The code with the build's changes as one new line (and an edit made by hand before them as its own)."""
    now = now or datetime.now(timezone.utc)
    history = rows(code)
    base = pending.get('base')
    if base and (not history or history[-1]['digest'] != base['digest']):
        when = base.get('time_ms')
        history.append({'version': str(len(history) + 1),
                        'date': datetime.fromtimestamp(when / 1000, timezone.utc).strftime('%Y-%m-%d') if when else '?',
                        'author': base.get('user') or '?', 'how': 'by hand',
                        'change': "Changed outside the agent (Stroom's editor)" if history
                        else 'Written before the agent kept a history', 'by': '', 'digest': base['digest']})
    entries = pending.get('entries') or []
    history.append({'version': str(len(history) + 1), 'date': now.strftime('%Y-%m-%d'),
                    'author': ', '.join(_distinct([e.get('author') for e in entries])) or '-', 'how': 'agent',
                    'change': '; '.join(_distinct([e.get('change') for e in entries])) or '-',
                    'by': '; '.join(_distinct([e.get('by') for e in entries])), 'digest': _digest(code)})
    return _with_block(code, history)


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
