"""Steps that are the user's, as entries a client can show as buttons (asked for by the user: Stroom's Ask Stroom AI
shows each on the tool call's card, where a click is the user's consent; the model can neither make nor click one).
The same steps stay in each result's text for other clients, and the server never takes a step as done: it reads
Stroom's state (wait_for_processing, build_status).

Four actions only: enable_processor_filter, disable_processor_filter, open_document, elasticsearch_request. A summary
is fixed wording with ids and document names, never record values: results carry data from Stroom, and a button's
words are the server's alone.
"""
import re
from typing import Any

from utils.stroom import doc_link

ACTIONS = ('enable_processor_filter', 'disable_processor_filter', 'open_document', 'elasticsearch_request')
# The only Elasticsearch requests offered: committing an index template, removing one (the old name's, after
# rename_index), dropping one index or data stream. An index name never starts with _ - or + (so not _all), and a
# wildcard or comma would name several.
_REQUEST = re.compile(r'^(PUT _index_template/[A-Za-z0-9][A-Za-z0-9._-]*\n\{.*\}|DELETE _index_template/[A-Za-z0-9][A-Za-z0-9._-]*'
                      r'|DELETE (_data_stream/)?[a-z0-9][a-z0-9._-]*)$', re.S)


def _doc(doc: dict[str, Any]) -> dict[str, str]:
    return {'type': doc.get('type') or 'Pipeline', 'uuid': doc['uuid'], 'name': doc.get('name') or doc['uuid']}


def enable_filter(settings: Any, filter_id: int, pipeline: dict[str, Any], what: str = '',
                  after: list[str] = ()) -> dict[str, Any]:
    doc = _doc({**pipeline, 'type': 'Pipeline'})
    return {'id': f'enable-{filter_id}', 'action': 'enable_processor_filter', 'filter_id': filter_id, 'doc': doc,
            'link': doc_link(settings, 'Pipeline', doc['uuid']),
            'summary': f"Enable filter {filter_id} on pipeline '{doc['name']}'" + (f": {what}" if what else ''),
            **({'after': list(after)} if after else {})}


def disable_filter(settings: Any, filter_id: int, pipeline: dict[str, Any], why: str,
                   optional: bool = False) -> dict[str, Any]:
    doc = _doc({**pipeline, 'type': 'Pipeline'})
    return {'id': f'disable-{filter_id}', 'action': 'disable_processor_filter', 'filter_id': filter_id, 'doc': doc,
            'link': doc_link(settings, 'Pipeline', doc['uuid']),
            'summary': f"Disable filter {filter_id} on pipeline '{doc['name']}': {why}",
            **({'optional': True} if optional else {})}


def open_document(settings: Any, doc: dict[str, Any], why: str) -> dict[str, Any]:
    doc = _doc(doc)
    return {'id': f"open-{doc['uuid'][:8]}", 'action': 'open_document', 'doc': doc,
            'link': doc_link(settings, doc['type'], doc['uuid']),
            'summary': f"Open {doc['type']} '{doc['name']}': {why}"}


def es_request(action_id: str, request: str, why: str, by: str = 'cluster admin') -> dict[str, Any] | None:
    """An Elasticsearch request for the cluster admin to run, shown for copying; None for any request not of the
    shapes offered (a template commit, one index or data stream dropped), so a malformed one is never a button."""
    request = request.strip()
    if not _REQUEST.match(request):
        return None
    return {'id': action_id, 'action': 'elasticsearch_request', 'by': by, 'request': request, 'summary': why}


def actions(*entries: dict[str, Any] | None) -> dict[str, Any]:
    """{'user_actions': [...]} for a result, without the entries left out; {} when none."""
    kept = [e for e in entries if e]
    return {'user_actions': kept} if kept else {}
