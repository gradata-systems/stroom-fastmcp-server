"""The mapping an XSLT was generated from, kept with the XSLT.

An XSLT doc's description carries the translation mapping (or an indexing field plan) as JSON between markers,
so the documentation's Field mapping section can be regenerated later, a change can start from the mapping
rather than the XSLT, and hand edits to the XSLT since the mapping are detected.
"""
import hashlib
import json
import re
from typing import Any

START = '--- stroom-mcp {kind} mapping (generated; change the mapping and regenerate, rather than the XSLT) ---'
END = '--- end of stroom-mcp mapping ---'
_BLOCK = re.compile(r'--- stroom-mcp (translation|index) mapping[^\n]*---\n(.*?)\n--- end of stroom-mcp mapping ---',
                    re.S)


def with_mapping(description: str | None, kind: str, payload: dict[str, Any]) -> str:
    """The description with the mapping block replacing any earlier one."""
    text = _BLOCK.sub('', description or '').strip()
    block = f"{START.format(kind=kind)}\n{json.dumps(payload, indent=1, ensure_ascii=False)}\n{END}"
    return f"{text}\n\n{block}".strip() if text else block


def read_mapping(description: str | None) -> tuple[str, dict[str, Any]] | None:
    """(kind, payload) from a description, or None when it holds no mapping block."""
    match = _BLOCK.search(description or '')
    if not match:
        return None
    try:
        return match.group(1), json.loads(match.group(2))
    except ValueError:
        return None


def normalise_xslt(text: str) -> str:
    """XSLT text with the differences that are not edits removed: the declaration, whitespace between tags."""
    text = re.sub(r'^\s*<\?xml[^>]*\?>', '', text or '')
    return re.sub(r'>\s+<', '><', text).strip()


def digest(*parts: str) -> str:
    """A short digest of the mapping and the XSLT text the documentation was generated from."""
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()[:16]


DOC_MARK = '<!-- stroom-mcp field-mapping {digest} -->'
_DOC_MARK = re.compile(r'<!-- stroom-mcp field-mapping ([0-9a-f]{16}) -->')


def doc_digest(markdown: str) -> str | None:
    match = _DOC_MARK.search(markdown or '')
    return match.group(1) if match else None


def replace_section(markdown: str, heading: str, body: str) -> str:
    """markdown with the `## heading` section's body replaced (or the section added before Output, else at the
    end, before any change log)."""
    pattern = re.compile(rf'(^## {re.escape(heading)}[^\n]*\n)(.*?)(?=^## |\Z)', re.M | re.S)
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
_AGREED_START = '--- stroom-mcp agreed index template (confirmed by the user; check_index_template to change it) ---'
_AGREED_END = '--- end of stroom-mcp agreed index template ---'
_AGREED = re.compile(r'--- stroom-mcp agreed index template[^\n]*---\n(.*?)\n--- end of stroom-mcp agreed index template ---',
                     re.S)


def with_agreed_template(description: str | None, agreed: dict[str, Any]) -> str:
    text = _AGREED.sub('', description or '').strip()
    block = f"{_AGREED_START}\n{json.dumps(agreed, indent=1, ensure_ascii=False)}\n{_AGREED_END}"
    return f"{text}\n\n{block}".strip() if text else block


def read_agreed_template(description: str | None) -> dict[str, Any] | None:
    match = _AGREED.search(description or '')
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None
