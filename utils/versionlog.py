"""The version control block at the end of a pipeline's documentation: one row per released version, numbered,
dated, with who made it (and whether through the agent), what changed and why, and the code it describes.

Asked for by the user: changes such as event types found only once a whole feed was processed, and the rules added
for them, are recorded with the documentation they changed; and one row for a build, written when it is done, not a
row each time the documentation is rewritten while it is worked on. So write_documentation keeps the rows as they
are and adds its change to the pending changes (a comment at the end of the doc), and promote_build turns them into
one row. A doc written before this carries a bullet change log (`## Change log`, `- date: change`): its lines
become the first rows.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any

HEADING = '## Version control'
OLD_HEADING = '## Change log'
_ROW = re.compile(r'^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|(.*)\|([^|]*)\|\s*$')
_BULLET = re.compile(r'^-\s*(\d{4}-\d{2}-\d{2}):\s*(.*)$')
_PENDING = re.compile(r'\n*<!-- stroom-mcp pending changes (.*?) -->\n*', re.S)
NONE_YET = "No version released yet: this build's changes are recorded here when it is promoted."


def body_of(markdown: str) -> str:
    """The documentation without its version control block (or older change log) and pending changes."""
    markdown = _PENDING.sub('\n', markdown or '')
    for heading in (HEADING, OLD_HEADING):
        markdown = markdown.split(heading)[0]
    return markdown.rstrip()


def rows_of(markdown: str) -> list[dict[str, str]]:
    """The block's rows, oldest first; an older bullet change log read as rows."""
    markdown = _PENDING.sub('\n', markdown or '')
    rows: list[dict[str, str]] = []
    if HEADING in markdown:
        for line in markdown.split(HEADING, 1)[1].splitlines():
            m = _ROW.match(line.strip())
            if m:
                rows.append({'version': m.group(1), 'date': m.group(2).strip(), 'by': m.group(3).strip(),
                             'change': m.group(4).strip().replace('\\|', '|'), 'code': m.group(5).strip()})
    elif OLD_HEADING in markdown:
        for line in markdown.split(OLD_HEADING, 1)[1].splitlines():
            m = _BULLET.match(line.strip())
            if m:
                rows.append({'version': str(len(rows) + 1), 'date': m.group(1), 'by': '', 'change': m.group(2).strip(),
                             'code': ''})
    return rows


def pending_of(markdown: str) -> list[dict[str, str]]:
    found = _PENDING.search(markdown or '')
    if not found:
        return []
    try:
        return json.loads(found.group(1).replace('- -', '--'))
    except ValueError:
        return []


def _cell(text: str) -> str:
    return (text or '').replace('|', '\\|').replace('\n', ' ').strip()


def block(rows: list[dict[str, str]]) -> str:
    lines = [HEADING, '', '| Version | Date | By | Change | Code |', '| --- | --- | --- | --- | --- |']
    lines += [f"| {r['version']} | {r['date']} | {_cell(r.get('by', ''))} | {_cell(r['change'])} | "
              f"{_cell(r.get('code', ''))} |" for r in rows]
    if not rows:
        lines += ['', NONE_YET]
    return '\n'.join(lines) + '\n'


def _pending_comment(entries: list[dict[str, str]]) -> str:
    # An HTML comment may not hold '--': JSON with it spaced out, put back when read.
    return '<!-- stroom-mcp pending changes ' + json.dumps(entries).replace('--', '- -') + ' -->\n' if entries else ''


def with_pending(body: str, old: str, change: str, by: str, code: str = '', now: datetime | None = None) -> str:
    """The documentation: body, the version control rows as they were, and this change pending."""
    now = now or datetime.now(timezone.utc)
    entries = pending_of(old) + [{'date': now.strftime('%Y-%m-%d'), 'change': change, 'by': by, 'code': code}]
    return f"{body_of(body)}\n\n{block(rows_of(old))}\n{_pending_comment(entries)}"


def consolidate(markdown: str, now: datetime | None = None) -> str | None:
    """The documentation with its pending changes as one new row; None when nothing is pending."""
    entries = pending_of(markdown)
    if not entries:
        return None
    now = now or datetime.now(timezone.utc)
    rows = rows_of(markdown)
    distinct = lambda key: list(dict.fromkeys(e.get(key) for e in entries if e.get(key)))  # noqa: E731
    rows.append({'version': str(int(rows[-1]['version']) + 1 if rows else 1), 'date': now.strftime('%Y-%m-%d'),
                 'by': '; '.join(distinct('by')), 'change': '; '.join(distinct('change')),
                 'code': (distinct('code') or [''])[-1]})
    return f"{body_of(markdown)}\n\n{block(rows)}"


def code_of(kept: dict[str, Any] | None) -> str:
    """The code a row describes: the XSLT and its Stroom version, and the mapping's digest."""
    if not kept:
        return ''
    from tools.builds import mapping_digest
    xslt = kept.get('xslt') or {}
    version = (xslt.get('version') or '')[:8]
    return (f"XSLT {xslt.get('name')}" + (f" @{version}" if version else '')
            + f", {kept['kind']} mapping {mapping_digest(kept)[:8]}")
