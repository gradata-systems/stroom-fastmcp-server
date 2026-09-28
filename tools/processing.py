"""Read-only processing tools: processor filter state and task progress."""
from collections import Counter
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from utils.stroom import gateway_from


def _terms(expression: dict[str, Any] | None) -> list[str]:
    out = []
    for child in (expression or {}).get('children') or []:
        if child.get('type') == 'operator':
            out.append(f"{child.get('op')}({', '.join(_terms(child))})")
        else:
            out.append(f"{child.get('field')} {child.get('condition')} {child.get('value')}")
    return out


async def processing_status(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="Pipeline whose processor filters to report.")],
) -> dict[str, Any]:
    """
    Processor filters for a pipeline and their progress: enabled, priority, what they select, tracker
    status and counts, and processor tasks by status. Use it to see whether processing has finished.
    """
    stroom = gateway_from(ctx)
    rows = await stroom.post('/processorFilter/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []}})
    filters = [row['processorFilter'] for row in rows.get('values') or []
               if row.get('processorFilter') and row['processorFilter'].get('pipelineUuid') == pipeline_uuid
               and not row['processorFilter'].get('deleted')]
    tasks = await stroom.post('/processorTask/v1/find', {'expression': {'type': 'operator', 'op': 'AND', 'children': []},
                                                         'pageRequest': {'offset': 0, 'length': 1000}})
    by_filter: dict[int, Counter] = {}
    for task in tasks.get('values') or []:
        fid = (task.get('processorFilter') or {}).get('id')
        by_filter.setdefault(fid, Counter())[task.get('status')] += 1
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
    return {'pipeline_uuid': pipeline_uuid, 'filters': result,
            'hint': None if result else "No processor filters for this pipeline."}


ALL_TOOLS = [processing_status]
