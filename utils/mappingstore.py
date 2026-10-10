"""The mapping an XSLT was generated from, kept with the XSLT.

An XSLT doc's description (its Documentation tab in Stroom) carries the translation mapping (or an indexing field
plan, or a CEF plan), so the documentation's Field mapping section can be regenerated later, a change can start from
the mapping rather than the XSLT, and hand edits to the XSLT since the mapping are detected. It is kept hidden, as
compact JSON in an HTML comment, after a visible note asking people to leave it be: shown as it was (1,400 lines of
JSON for one translation), it buried the tab's own text. The same goes for a build's pending changes and a
pipeline's agreed index template. Blocks written before (shown, between --- markers) are still read, and are
rewritten hidden when the document is next saved.
"""
import hashlib
import json
import re
from typing import Any

NOTE = ("*Below, hidden, is what stroom-mcp keeps for this document: the mapping it is generated from, a build's "
        "pending changes, an agreed index template. Please don't edit or delete it: change the document through the "
        "agent.*")
# Shown blocks, as written before: read, and replaced by hidden ones when next written.
_SHOWN = re.compile(r'\n*--- stroom-mcp ([a-z ]+?) (?:\([^\n]*\) )?---\n(.*?)\n--- end of stroom-mcp [a-z ]+? ---\n*', re.S)
_HIDDEN = re.compile(r'\n*<!-- stroom-mcp ([a-z ]+): kept by the server, do not edit or delete\n(.*?)\n-->\n*', re.S)
_NOTE = re.compile(r'\n*' + re.escape(NOTE) + r'\n*')
# Where the server's part of a description begins: its note, a hidden block, or a shown one written before.
SERVER_PART = re.compile(r'(?:^|\n)(?:' + re.escape(NOTE[:20]) + r'|<!-- stroom-mcp [a-z ]+: kept by the server'
                         r'|--- stroom-mcp )')


def read_block(description: str | None, name: str) -> Any:
    """A block's JSON, hidden or (written before) shown; None when there is none or it doesn't parse."""
    for pattern in (_HIDDEN, _SHOWN):
        for match in pattern.finditer(description or ''):
            if match.group(1) == name:
                try:
                    return json.loads(match.group(2))
                except ValueError:
                    return None
    return None


def without_block(description: str | None, name: str | None = None) -> str:
    """The description without the named block, hidden or shown; with name None, without every server block and
    the note: the document's own text (and its version history)."""
    def drop(match: re.Match) -> str:
        return '\n\n' if name is None or match.group(1) == name else match.group(0)
    text = _SHOWN.sub(drop, _HIDDEN.sub(drop, description or ''))
    if name is None or not (_HIDDEN.search(text) or _SHOWN.search(text)):
        text = _NOTE.sub('\n\n', text)      # the note stays only while a block it speaks of does
    return text.strip()


def with_block(description: str | None, name: str, payload: Any) -> str:
    """The description with the block (hidden, compact JSON) replacing any earlier one, after the note. A comment
    can't hold '--': written '-\\u002d', which JSON reads back as '--' (it only occurs inside a string)."""
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('--', '-\\u002d')
    block = f"<!-- stroom-mcp {name}: kept by the server, do not edit or delete\n{body}\n-->"
    text = _NOTE.sub('\n\n', without_block(description, name)).strip()
    at = SERVER_PART.search(text)
    head, tail = (text, '') if not at else (text[:at.start()], text[at.start():])
    return '\n\n'.join(part.strip() for part in (head, NOTE, tail, block) if part.strip())


def with_mapping(description: str | None, kind: str, payload: dict[str, Any]) -> str:
    """The description with the mapping block replacing any earlier one (of any kind)."""
    text = description or ''
    for other in ('translation', 'index', 'cef'):
        if other != kind:
            text = without_block(text, f'{other} mapping')
    return with_block(text, f'{kind} mapping', payload)


def read_mapping(description: str | None) -> tuple[str, dict[str, Any]] | None:
    """(kind, payload) from a description, or None when it holds no mapping block."""
    for kind in ('translation', 'index', 'cef'):
        payload = read_block(description, f'{kind} mapping')
        if payload is not None:
            return kind, payload
    return None


def normalise_xslt(text: str) -> str:
    """XSLT text with the differences that are not edits removed: the declaration, whitespace between tags."""
    from utils.xsltversion import strip
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', strip(text or ''))     # its version history isn't code either
    return re.sub(r'>\s+<', '><', text).strip()


def digest(*parts: str) -> str:
    """A short digest of the mapping and the XSLT text the documentation was generated from."""
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()[:16]


DOC_MARK = '<!-- stroom-mcp field-mapping {digest} -->'
_DOC_MARK = re.compile(r'<!-- stroom-mcp field-mapping ([0-9a-f]{16}) -->')


def doc_digest(markdown: str) -> str | None:
    match = _DOC_MARK.search(markdown or '')
    return match.group(1) if match else None


def replace_section(markdown: str, heading: str, body: str, exact: bool = False) -> str:
    """markdown with the `## heading` section's body replaced (or the section added before Output, else at the
    end, before any change log). exact: only a heading that is exactly this, not one starting with it."""
    rest = r'[ \t]*' if exact else r'[^\n]*'
    pattern = re.compile(rf'(^## {re.escape(heading)}{rest}\n)(.*?)(?=^## |\Z)', re.M | re.S)
    section = f'## {heading}\n\n{body.strip()}\n\n'
    if pattern.search(markdown):
        return pattern.sub(lambda m: section, markdown, count=1)
    for anchor in ('## Output', '## Conformance', '## Open items', '## Change log'):
        at = markdown.find(anchor)
        if at >= 0:
            return markdown[:at] + section + markdown[at:]
    return markdown.rstrip() + '\n\n' + section


# The Elasticsearch index template the user agreed for an indexing pipeline, kept in the pipeline's description: what
# the cluster admin was asked to apply, for the index and cluster and the indexing XSLT it was agreed against.
def with_agreed_template(description: str | None, agreed: dict[str, Any]) -> str:
    return with_block(description, 'agreed index template', agreed)


def read_agreed_template(description: str | None) -> dict[str, Any] | None:
    return read_block(description, 'agreed index template')
