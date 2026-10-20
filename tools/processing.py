"""Read-only processing tools: processor filter state and task progress."""
from collections import Counter
from typing import Annotated, Any

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from utils.stroom import gateway_from, refused_reason


def _terms(expression: dict[str, Any] | None) -> list[str]:
    out = []
    for child in (expression or {}).get('children') or []:
        if child.get('type') == 'operator':
            out.append(f"{child.get('op') or 'AND'}({', '.join(_terms(child))})")
        else:
            # A document by reference (Feed or Pipeline IS_DOC_REF) shows its name.
            value = child.get('value') if child.get('value') is not None else (child.get('docRef') or {}).get('name')
            out.append(f"{child.get('field')} {child.get('condition')} {value}")
    return out


async def pipeline_feeds(stroom: Any, pipeline_uuid: str, depth: int = 0,
                         filters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the pipeline's processor filters read: each filter's conditions, the feeds they select (named, by
    reference, in a Dictionary and the dictionaries it imports, or through a sample stream's id), the stream types,
    and the pipelines whose output they read (an indexing pipeline's events pipeline, with the feeds that one reads).
    Seen: asked which feeds a pipeline acts on, an agent found its filters' conditions unreadable (a dictionary of
    feeds showed only as 'Feed IN_DICTIONARY'), and fell back on a keyword search for documents."""
    from tools.coverage import filter_feeds, filter_terms
    try:
        filters = await stroom.processor_filters(pipeline_uuid) if filters is None else filters
    except ToolError as e:
        return {'filters_unreadable': f"Stroom refused to list this pipeline's processor filters: {refused_reason(e)}. "
                                      f"Listing them needs permission to view the pipeline's processors."}
    known: dict[Any, Any] = {}
    out, feeds, sources = [], set(), []
    for f in filters:
        expression = (f.get('queryData') or {}).get('expression')
        terms = filter_terms(expression)
        read = sorted(await filter_feeds(stroom, terms, known, lower=False))
        types = [t.get('value') for t in terms if t.get('field') == 'Type' and t.get('value')]
        upstream = [t.get('docRef') or {} for t in terms if t.get('field') == 'Pipeline' and t.get('docRef')]
        entry = {'id': f.get('id'), 'enabled': f.get('enabled'), 'selects': _terms(expression), 'feeds': read,
                 **({'stream_types': types} if types else {})}
        dictionaries = [(t.get('docRef') or {}).get('name') for t in terms if t.get('condition') == 'IN_DICTIONARY']
        if dictionaries:
            entry['feeds_from_dictionaries'] = [d for d in dictionaries if d]
        for ref in upstream:
            if not ref.get('uuid'):
                continue
            # Events written by another pipeline: the feeds that one reads are what this pipeline acts on in the end.
            above = await pipeline_feeds(stroom, ref['uuid'], depth + 1) if depth < 2 else {}
            entry.setdefault('output_of', []).append({'pipeline': ref.get('name'), 'uuid': ref['uuid'],
                                                      'its_feeds': above.get('feeds', [])})
            sources.append(ref.get('name'))
            feeds |= set(above.get('feeds', []))
        feeds |= set(read)
        out.append(entry)
    return {'filters': out, 'feeds': sorted(feeds), **({'reads_output_of': sources} if sources else {}),
            **({} if out else {'note': "No processor filters: this pipeline processes nothing on its own (it may be a "
                                       "template others inherit, or only stepped)."})}


async def processing_status(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="Pipeline whose processor filters to report.")],
) -> dict[str, Any]:
    """
    Processor filters for a pipeline and their progress: enabled, priority, what they select and the feeds that is
    (through dictionaries and stream ids too), tracker status and counts, and processor tasks by status. Use it to
    see whether processing has finished.
    """
    stroom = gateway_from(ctx)
    filters = await stroom.processor_filters(pipeline_uuid)
    by_filter: dict[int, Counter] = {}
    tasks_unreadable = None
    for f in filters:
        # Each filter's own tasks: on a busy instance a page of every task in the system may not include them.
        try:
            tasks = await stroom.post('/processorTask/v1/find', {
                'expression': {'type': 'operator', 'op': 'AND', 'children': [
                    {'type': 'term', 'field': 'Processor Filter Id', 'condition': 'EQUALS', 'value': str(f['id'])}]},
                'pageRequest': {'offset': 0, 'length': 1000}})
        except ToolError as e:      # the filters are still worth having without their tasks
            tasks_unreadable = refused_reason(e)
            tasks = {}
        by_filter[f['id']] = Counter(task.get('status') for task in tasks.get('values') or [])
    result = []
    for f in filters:
        tracker = f.get('processorFilterTracker') or {}
        counts = by_filter.get(f['id'], Counter())
        outstanding = sum(n for s, n in counts.items() if s not in ('COMPLETE', 'FAILED', 'DELETED'))
        result.append({'id': f['id'], 'enabled': f.get('enabled'), 'priority': f.get('priority'),
                       'selects': _terms((f.get('queryData') or {}).get('expression')),
                       'tracker': {'status': tracker.get('status'), 'streams_matched': tracker.get('metaCount'),
                                   'events': tracker.get('eventCount'), 'message': tracker.get('message')},
                       'tasks': dict(counts), 'finished': bool(counts) and outstanding == 0})
    read = await pipeline_feeds(stroom, pipeline_uuid, filters=filters)
    for entry, described in zip(result, read.get('filters') or []):
        entry['feeds'] = described['feeds']
        entry.update({k: described[k] for k in ('feeds_from_dictionaries', 'output_of') if k in described})
    return {'pipeline_uuid': pipeline_uuid, 'filters': result, 'feeds': read.get('feeds', []),
            **({'tasks_unreadable': tasks_unreadable} if tasks_unreadable else {}),
            'hint': None if result else "No processor filters for this pipeline."}


ALL_TOOLS = [processing_status]
