"""Tools that step pipelines record by record, optionally with draft (unsaved) code."""
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from tools.pipelines import merge_layers, own_elements
from utils.stroom import StroomGateway, gateway_from
from utils.triage import from_stored_error, triage

PipelineUuid = Annotated[str, Field(description="UUID of the pipeline to step.")]
DraftCode = Annotated[dict[str, str] | None, Field(
    description="Unsaved code to step with instead of the saved documents, keyed by element id, "
                "e.g. {'translationFilter': '<xsl:stylesheet ...>'}. Nothing is saved.")]


class _Pipeline:
    """What stepping needs to know about a pipeline, fetched once per tool call."""

    def __init__(self, doc: dict[str, Any], layers: list[dict[str, Any]]):
        self.doc = doc
        merged = merge_layers(layers)
        self.types = {e['id']: e['type'] for e in merged['elements']}
        self.own = own_elements(layers)

    @classmethod
    async def load(cls, stroom: StroomGateway, uuid: str) -> '_Pipeline':
        return cls(await stroom.get(f'/pipeline/v1/{uuid}'), await stroom.pipeline_layers(uuid))

    def default_outputs(self) -> list[str]:
        """The pipeline's own XSLT steps, which is where its translation happens."""
        own_xslt = [e for e, t in self.types.items() if e in self.own and t == 'XSLTFilter']
        return own_xslt or [e for e, t in self.types.items() if t == 'XSLTFilter'][-1:]


def _criteria(stream_id: int) -> dict[str, Any]:
    return {'expression': {'type': 'operator', 'op': 'AND', 'children': [
        {'type': 'term', 'field': 'Id', 'condition': 'EQUALS', 'value': str(stream_id)}]}}


async def _step(stroom: StroomGateway, pipeline: _Pipeline, stream_id: int, step_type: str,
                location: dict[str, Any] | None, code: dict[str, str] | None) -> dict[str, Any]:
    request = {'pipelineDoc': pipeline.doc, 'criteria': _criteria(stream_id), 'stepType': step_type, 'stepSize': 1,
               'timeout': 30000, 'code': code or {}}
    if location:
        request['stepLocation'] = location
    return await stroom.step(request)


def _markers(result: dict[str, Any], record: int | None = None) -> list[dict[str, Any]]:
    markers = []
    for element, data in ((result.get('stepData') or {}).get('elementMap') or {}).items():
        for error in ((data.get('indicators') or {}).get('uniqueErrorSet') or []):
            m = from_stored_error(error)
            m['element'] = m['element'] if m['element'] != 'unknown' else element
            m['record'] = record
            markers.append(m)
    for message in result.get('generalErrors') or []:
        markers.append({'severity': 'ERROR', 'element': 'pipeline', 'message': message, 'location': None,
                        'record': record})
    return markers


async def step_pipeline(
        ctx: Context,
        pipeline_uuid: PipelineUuid,
        stream_id: Annotated[int, Field(description="Stream to step through, e.g. a Raw Events sample.")],
        record: Annotated[int | Literal['first', 'last'], Field(
            description="Zero-based record index, or 'first' / 'last'.")] = 'first',
        draft_code: DraftCode = None,
        show: Annotated[list[str] | None, Field(
            description="Element ids whose input and output to return. Defaults to the pipeline's own "
                        "XSLT steps.")] = None,
) -> dict[str, Any]:
    """
    Step one record through a pipeline and show what each element does to it: the input and output of
    the chosen elements, and every element's errors and warnings, triaged. Use draft_code to try a
    translation change without saving it.
    """
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    if isinstance(record, int):
        result = await _step(stroom, pipeline, stream_id, 'REFRESH',
                             {'metaId': stream_id, 'partIndex': 0, 'recordIndex': record}, draft_code)
    else:
        result = await _step(stroom, pipeline, stream_id, record.upper(), None, draft_code)
    if not result.get('foundRecord'):
        raise ToolError(f"No record found in stream {stream_id} for {record!r}")

    elements = (result.get('stepData') or {}).get('elementMap') or {}
    wanted = show or pipeline.default_outputs()
    budget = stroom.settings.max_stream_chars // max(1, 2 * len(wanted))
    outputs = {e: {'type': pipeline.types.get(e), 'input': (elements.get(e) or {}).get('input', '')[:budget],
                   'output': (elements.get(e) or {}).get('output', '')[:budget]}
               for e in wanted if e in elements}
    location = result.get('foundLocation') or {}
    return {'pipeline': pipeline.doc.get('name'), 'stream_id': stream_id,
            'record': location.get('recordIndex'), 'draft_code_used': sorted(draft_code or {}),
            'elements': outputs, **triage(_markers(result, location.get('recordIndex')),
                                          ctx.lifespan_context['rules'], pipeline.own)}


async def step_sample(
        ctx: Context,
        pipeline_uuid: PipelineUuid,
        stream_ids: Annotated[list[int], Field(description="Sample streams to step through, every record.")],
        draft_code: DraftCode = None,
        max_records: Annotated[int | None, Field(
            ge=1, description="Stop after this many records (default: the server's max_sample_records).")] = None,
) -> dict[str, Any]:
    """
    Step every record of the sample streams to completion and return one verdict for the whole sample:
    error groups triaged (blocking / review / benign) with the records they affect, plus per-record
    status. This is the correctness check before processing; a blocking group means fix and step again.
    """
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    cap = min(max_records or stroom.settings.max_sample_records, stroom.settings.max_sample_records)
    markers: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    first_output = None
    for stream_id in stream_ids:
        result = await _step(stroom, pipeline, stream_id, 'FIRST', None, draft_code)
        while result.get('foundRecord') and len(records) < cap:
            location = result['foundLocation']
            key = f"{stream_id}:{location['recordIndex']}"
            found = _markers(result, key)
            markers += found
            records.append({'record': key, 'errors': len(found)})
            if first_output is None:
                elements = (result.get('stepData') or {}).get('elementMap') or {}
                first_output = {e: (elements.get(e) or {}).get('output', '')[:stroom.settings.max_stream_chars // 4]
                                for e in pipeline.default_outputs()}
            result = await _step(stroom, pipeline, stream_id, 'FORWARD', location, draft_code)

    summary = triage(markers, ctx.lifespan_context['rules'], pipeline.own, record_count=len(records))
    for group in summary['groups']:
        group['records'] = sorted({m['record'] for m in markers
                                   if (m['severity'], m['element']) == (group['severity'], group['element'])})[:20]
    result: dict[str, Any] = {'pipeline': pipeline.doc.get('name'), 'records_stepped': len(records),
                              'records_with_errors': sum(1 for r in records if r['errors']),
                              'draft_code_used': sorted(draft_code or {}), **summary,
                              'first_record_output': first_output}
    if len(records) >= cap:
        result['hint'] = f"Stopped at {cap} records; the sample has more."
    return result


ALL_TOOLS = [step_pipeline, step_sample]
