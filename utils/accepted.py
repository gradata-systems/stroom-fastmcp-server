"""Errors a user has accepted as benign for a pipeline, kept in the pipeline's Documentation doc.

When the user says an error is benign and can be ignored (a lookup miss for accounts the reference data does not
hold, say), the agent records it with the user's reason; triage then classes that kind of error as benign for the
pipeline, saying why, so later reviews do not raise it again. An entry matches by element and by message with its
variable parts (quoted values, numbers) masked, as triage groups errors.
"""
import fnmatch
import json
import re
from typing import Any

from utils.triage import normalise

_BLOCK = re.compile(r'<!-- stroom-mcp accepted errors\n(.*?)\n-->', re.S)


def read_accepted(markdown: str | None) -> list[dict[str, Any]]:
    """The accepted errors recorded in a Documentation doc's text."""
    match = _BLOCK.search(markdown or '')
    if not match:
        return []
    try:
        entries = json.loads(match.group(1))
    except ValueError:
        return []
    return [e for e in entries if isinstance(e, dict) and e.get('pattern')]


def block(entries: list[dict[str, Any]]) -> str:
    return f"<!-- stroom-mcp accepted errors\n{json.dumps(entries, indent=1, ensure_ascii=False)}\n-->" if entries else ''


def entry(element: str | None, example: str, reason: str, date: str, matches: str | None = None) -> dict[str, Any]:
    """matches: the kind of message covered, with * for the parts that vary (e.g. 'No HR record for user svc-*');
    without it, the example, with its numbers and quoted values varying."""
    return {'element': element or None, 'pattern': normalise(matches or example), 'example': example[:300],
            'reason': reason, 'accepted': date}


def merge(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New entries replace old ones for the same element and pattern."""
    keyed = {(e.get('element'), e['pattern']): e for e in old}
    keyed.update({(e.get('element'), e['pattern']): e for e in new})
    return list(keyed.values())


def match(entries: list[dict[str, Any]], element: str, message: str) -> dict[str, Any] | None:
    """The accepted entry an error falls under, if any."""
    seen = normalise(message)
    return next((e for e in entries if e.get('element') in (None, element)
                 # A pattern with * covers messages containing it (Stroom prefixes some, e.g. 'Log - ').
                 and (fnmatch.fnmatchcase(seen, f"*{e['pattern']}*") if '*' in e['pattern'] else e['pattern'] == seen)), None)


async def accepted_for(stroom, pipeline_uuid: str) -> list[dict[str, Any]]:
    """Every accepted error recorded for a pipeline: in the Documentation docs named after it (beside it, or in a
    build), or after the pipeline a working copy was made from."""
    try:
        pipeline = await stroom.get_doc('Pipeline', pipeline_uuid)
        names = {pipeline.get('name')}
        node = await stroom.post('/explorer/v2/getFromDocRef', {'type': 'Pipeline', 'uuid': pipeline_uuid,
                                                                'name': pipeline.get('name')})
        for tag in node.get('tags') or []:
            if tag.startswith('mcp-copy-of-'):
                names.add((await stroom.get_doc('Pipeline', tag[len('mcp-copy-of-'):])).get('name'))
        entries: list[dict[str, Any]] = []
        for name in filter(None, names):
            found = (await stroom.find_documents(name, ['Documentation'], 20)).get('values') or []
            for value in found:
                if value['docRef'].get('name') == name:
                    doc = await stroom.get_doc('Documentation', value['docRef']['uuid'])
                    entries = merge(entries, read_accepted(doc.get('data') or doc.get('documentation')))
        return entries
    except Exception:       # accepted errors are a courtesy: never a reason for triage to fail
        return []
