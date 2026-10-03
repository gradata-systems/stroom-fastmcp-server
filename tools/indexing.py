"""Indexing tools for either backend: Stroom's Lucene index or Elasticsearch."""
import asyncio
import re
import json
import time
import uuid as uuidlib
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from security.guard import guard_from
from tools.explorer import _redact
from tools.pipeline_writes import PropertyValue, create_pipeline
from tools.processing_writes import elastic_destination
from tools.stepping import _outputs, _Pipeline
from tools.streams import _meta, summarise_events
from tools.templates import _shape
from utils.consent import consent_from
from utils.fielddoc import index_field_mapping_markdown
from utils.fieldplan import Backend, FieldPlan, PlannedField
from utils.mappingstore import read_mapping
from utils.params import ONE_OR_MORE
from utils.stroom import doc_link, gateway_from
from utils.templatecheck import compare, json_xml_documents, parse_template

Build = Annotated[str, Field(description="Build name; its workspace folder is created if needed.")]
INDEX_TYPE = {'lucene': 'Index', 'elasticsearch': 'ElasticIndex'}


def _conventions(ctx: Context) -> dict[str, dict[str, Any]]:
    folder: Path = gateway_from(ctx).settings.conventions_dir
    out = {}
    for path in sorted(folder.glob('*.yaml')) if folder.is_dir() else []:
        profile = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        out[profile.get('name', path.stem)] = profile
    return out


async def get_field_conventions(
        ctx: Context,
        name: Annotated[str | None, Field(description="Convention profile to use; omit to list them.")] = None,
) -> dict[str, Any]:
    """
    Field naming conventions for indexes. Without a name (and no configured default) this lists the profiles
    and returns needs_guidance: the agent must ask the user which convention to follow, point at reference
    index docs, or describe one. It never picks a convention itself. With a name it returns the profile's field
    map plus the fields and types of its reference index docs (Lucene Index or Elastic Index docs, read
    through Stroom).
    """
    profiles = _conventions(ctx)
    name = name or gateway_from(ctx).settings.default_convention
    if not name:
        return {'status': 'needs_guidance', 'profiles': {n: p.get('description') for n, p in profiles.items()},
                'hint': "Ask the user which convention to use, which existing index docs to follow, "
                        "or how fields should be named. Do not assume one."}
    if name not in profiles:
        raise ToolError(f"No convention profile '{name}'. Profiles: {', '.join(profiles) or 'none'}")
    profile = profiles[name]
    stroom = gateway_from(ctx)
    reference: dict[str, dict[str, str]] = {}
    for doc_name in profile.get('reference_index_docs') or []:
        found = await stroom.find_documents(doc_name, ['Index', 'ElasticIndex'], 20)
        for value in found.get('values') or []:
            ref = value['docRef']
            if ref.get('name') == doc_name and ref.get('type') in ('Index', 'ElasticIndex'):
                fields = await stroom.post('/dataSource/v1/findFields', {
                    'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 500}})
                reference[f"{ref['type']} {doc_name}"] = {f['fldName']: f['fldType'] for f in fields.get('values') or []}
    return {'name': name, 'profile': profile, 'reference_fields': reference}


async def draft_index_mapping(
        ctx: Context,
        backend: Annotated[Backend, Field(description="From the chosen indexing template (find_pipeline_templates).")],
        index_name: Annotated[str, Field(description="Lucene index doc name, or ES index / data stream name.")],
        convention: Annotated[str, Field(description="Convention profile the user chose (get_field_conventions).")],
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams from stage 1, to see which "
                                                                  "event-logging paths are actually populated.")],
        extra_fields: Annotated[list[PlannedField] | str, ONE_OR_MORE, Field(
            description="Fields the user asked for beyond the convention's map.")] = [],
        drop_when: Annotated[list[str] | str, ONE_OR_MORE, Field(
            description="XPath tests on an Event for events the user wants kept out of the index, e.g. "
                        "\"EventDetail/TypeId = 'Heartbeat'\"; any that holds drops the event.")] = [],
) -> dict[str, Any]:
    """
    Draft the index for the build: a field plan (name, type and source path per field) from the chosen
    convention, limited to paths the sample events actually populate, plus StreamId and EventId. Returns
    the plan rendered for the backend (Lucene field list or Elasticsearch index template) and a draft
    indexing XSLT in the output form that backend's indexing filter reads, leaving out events drop_when
    names. Nothing is saved.
    """
    profiles = _conventions(ctx)
    if convention not in profiles:
        raise ToolError(f"No convention profile '{convention}'; ask the user and use get_field_conventions")
    profile = profiles[convention]
    events = await summarise_events(ctx, events_stream_ids, 200)
    populated = events['path_population']
    fields = [PlannedField(name='StreamId', type='id', source='@StreamId'),
              PlannedField(name='EventId', type='id', source='@EventId')]
    unused = []
    for path, spec in (profile.get('field_map') or {}).items():
        if populated.get(path):
            fields.append(PlannedField(name=spec['name'], type=spec['type'], source=path))
        else:
            unused.append(path)
    fields += [f for f in extra_fields if f.name not in {x.name for x in fields}]
    time_field = next((f.name for f in fields if f.source == 'EventTime/TimeCreated'), 'EventTime')
    if backend == 'elasticsearch' and not any(f.name == '@timestamp' for f in fields):
        fields.append(PlannedField(name='@timestamp', type='date', source='EventTime/TimeCreated'))
        time_field = '@timestamp'
    plan = FieldPlan(backend=backend, index_name=index_name, time_field=time_field, fields=fields, drop_when=drop_when)
    rendered = plan.lucene_fields() if backend == 'lucene' else plan.elastic_template(index_name)
    unmapped = sorted(p for p in populated if not any(p == f.source for f in fields))
    return {'plan': plan.model_dump(), 'problems': plan.required(), 'rendered': rendered, 'xslt': plan.xslt(),
            'convention_paths_not_in_sample': unused, 'populated_paths_not_mapped': unmapped[:40],
            'field_mapping': index_field_mapping_markdown(plan, populated),
            'hint': "Review unmapped paths with the user; add any they want as extra_fields and draft again. Save the "
                    "XSLT with save_xslt index_plan=plan and no code (it is generated from the plan), so "
                    "write_documentation generates the Field mapping section."}


async def set_index_fields(
        ctx: Context,
        index_uuid: Annotated[str, Field(description="A Lucene Index doc this server created.")],
        plan: Annotated[FieldPlan, Field(description="The field plan from draft_index_mapping (backend lucene).")],
) -> dict[str, Any]:
    """Lucene: add the plan's fields to the index doc (keywords as TEXT with the KEYWORD analyzer)."""
    if plan.backend != 'lucene':
        raise ToolError("create_index_doc (plan=...) is for Lucene; Elasticsearch fields come from the index template the user commits "
                        "(propose_index_template)")
    stroom = gateway_from(ctx)
    doc = await stroom.get_doc('Index', index_uuid)
    ref = {'type': 'Index', 'uuid': index_uuid, 'name': doc.get('name')}
    await guard_from(ctx).check_managed(ref)
    existing = await stroom.post('/dataSource/v1/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 500}})
    have = {f['fldName'] for f in existing.get('values') or []}
    added = []
    for field in plan.lucene_fields():
        if field['fldName'] not in have:
            await stroom.post('/index/v2/addField', {'indexDocRef': ref, 'indexField': field})
            added.append(field['fldName'])
    return {'index': doc.get('name'), 'added': added, 'already_present': sorted(have)}


async def find_elastic_clusters(
        ctx: Context, test: Annotated[bool, Field(description="Also run Stroom's connection test on each.")] = False,
) -> dict[str, Any]:
    """
    Elasticsearch: the Elastic Cluster docs in Stroom with their connection URLs (never credentials), the
    Elastic Index docs that use each, and those docs' settings. Pick the cluster that sibling sources use and
    confirm it with the user; this server never creates or changes cluster docs.
    """
    stroom = gateway_from(ctx)
    clusters = [v['docRef'] for v in (await stroom.find_documents('*', ['ElasticCluster'], 50)).get('values') or []
                if v['docRef'].get('type') == 'ElasticCluster']
    indexes = [v for v in (await stroom.find_documents('*', ['ElasticIndex'], 500)).get('values') or []
               if v['docRef'].get('type') == 'ElasticIndex']
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for value in indexes:
        doc = await stroom.get_doc('ElasticIndex', value['docRef']['uuid'])
        cluster = (doc.get('clusterRef') or {}).get('uuid')
        by_cluster.setdefault(cluster, []).append({'name': doc.get('name'), 'uuid': doc.get('uuid'),
                                                   'index_name': doc.get('indexName'), 'time_field': doc.get('timeField'),
                                                   'path': (value.get('path') or '').replace(' / ', '/')})
    out = []
    for ref in clusters:
        doc = _redact(await stroom.get_doc('ElasticCluster', ref['uuid']))
        entry = {'name': doc.get('name'), 'uuid': doc.get('uuid'),
                 'urls': (doc.get('connection') or {}).get('connectionUrls'),
                 'index_docs': by_cluster.get(ref['uuid'], [])}
        if test:
            entry['test'] = await stroom.post('/elasticCluster/v1/testCluster', await stroom.get_doc('ElasticCluster', ref['uuid']))
        out.append(entry)
    return {'clusters': out}


async def create_index_doc(
        ctx: Context,
        build: Build,
        backend: Backend,
        name: Annotated[str, Field(description="Index doc name, following the environment's convention.")],
        time_field: Annotated[str, Field(description="The plan's time field.")],
        index_name: Annotated[str | None, Field(description="Elasticsearch: the index or data stream name.")] = None,
        cluster_uuid: Annotated[str | None, Field(description="Elasticsearch: an existing Elastic Cluster doc.")] = None,
        volume_group: Annotated[str, Field(description="Lucene: the index volume group.")] = 'Default Volume Group',
        plan: Annotated[FieldPlan | None, Field(description="Lucene: the field plan from draft_index_mapping; its fields "
                                                           "are added to the index doc (keywords as TEXT with the KEYWORD "
                                                           "analyzer). Elasticsearch fields come from the index template.")] = None,
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Create the build's index doc: a Lucene Index in a volume group with the plan's fields, or an Elastic
    Index doc on an existing Elastic Cluster doc pointing at the index or data stream, tested against the
    cluster. The user confirms the backend, name and target.
    """
    stroom = gateway_from(ctx)
    if backend == 'elasticsearch':
        if not (index_name and cluster_uuid):
            raise ToolError("Elasticsearch needs index_name and cluster_uuid (find_elastic_clusters)")
        cluster = await stroom.get_doc('ElasticCluster', cluster_uuid)
        target = {'cluster': cluster.get('name'), 'index name': index_name}
    else:
        target = {'volume group': volume_group}
    details = {'build': build, 'backend': backend, 'index doc': name, 'time field': time_field, **target}
    gate = await consent_from(ctx).require(ctx, 'confirmation', 'create_index_doc', f"Create {backend} index doc '{name}'",
                                           details, confirmation_id)
    if gate:
        return gate
    doc_type = INDEX_TYPE[backend]
    ref = await guard_from(ctx).create(doc_type, name, build)
    doc = await stroom.get_doc(doc_type, ref['uuid'])
    if backend == 'lucene':
        doc.update(volumeGroupName=volume_group, timeField=time_field, partitionBy='MONTH', partitionSize=1,
                   shardsPerPartition=1)
    else:
        doc.update(clusterRef={'type': 'ElasticCluster', 'uuid': cluster_uuid, 'name': cluster.get('name')},
                   indexName=index_name, timeField=time_field)
    doc = await stroom.put_doc(doc)
    extra: dict[str, Any] = {}
    if backend == 'lucene' and plan is not None:
        extra['fields'] = await set_index_fields(ctx, doc['uuid'], plan)
    elif backend == 'lucene':
        extra['fields'] = ("none yet: the index indexes nothing until it has the plan's fields. Give plan= here, or "
                           "create_indexing_pipeline adds them from the plan kept with the indexing XSLT (save_xslt "
                           "index_plan=...).")
    elif backend == 'elasticsearch':
        try:
            extra['test'] = await test_elastic_index(ctx, doc['uuid'])
        except ToolError as e:
            extra['test'] = {'error': str(e)}
    from tools.plan import with_next
    return await with_next(ctx, build, {'type': doc_type, 'uuid': doc['uuid'], 'name': doc['name'], **target, **extra})


async def _events_available(ctx: Context, build: str, events_stream_ids: list[int]) -> None:
    """An indexing pipeline reads Events, which raw data only has once an events pipeline has translated it.
    The build's own events pipeline counts; otherwise the Events streams it will index must already exist."""
    stroom = gateway_from(ctx)
    for doc in await guard_from(ctx).folder_contents(build):
        if doc['type'] == 'Pipeline' and (await _shape(stroom, doc['uuid']))['stage'] == 'translation':
            return
    if not events_stream_ids:
        raise ToolError(f"Build '{build}' has no events pipeline, and no events_stream_ids were given. An indexing "
                        f"pipeline reads Events streams, not raw data: build the events pipeline first (stage 1 of "
                        f"onboard_data_source: feed, translation XSLT, step, process), then index its Events. To "
                        f"index Events an existing pipeline already produces, pass their stream ids as "
                        f"events_stream_ids. Raw structured data indexed as it is, with no translation, is a "
                        f"discovery template (find_pipeline_templates stage=discovery).")
    meta = await _meta(stroom, events_stream_ids[0])
    if meta.get('typeName') != 'Events':
        raise ToolError(f"Stream {events_stream_ids[0]} is {meta.get('typeName')!r}, not Events. An indexing pipeline "
                        f"reads the Events streams an events pipeline produces; build that first (stage 1).")


async def _missing_index_fields(stroom, index_uuid: str, xslt_uuid: str) -> tuple[list[str], FieldPlan | None]:
    """The fields the indexing XSLT writes that the Lucene index lacks, and the field plan kept with the XSLT (None when
    it was written by hand). Stroom drops a value for a field the index lacks with only a warning ("Attempt to index
    unknown field"), so the pipeline runs and indexes nothing searchable."""
    index = await stroom.get_doc('Index', index_uuid)
    ref = {'type': 'Index', 'uuid': index_uuid, 'name': index.get('name')}
    found = await stroom.post('/dataSource/v1/findFields', {'dataSourceRef': ref, 'pageRequest': {'offset': 0, 'length': 500}})
    have = {f['fldName'] for f in found.get('values') or []}
    xslt = await stroom.get_doc('XSLT', xslt_uuid)
    kept = read_mapping(xslt.get('description'))
    plan = FieldPlan.model_validate(kept[1]) if kept and kept[0] == 'index' else None
    if plan:
        written = [f.name for f in plan.fields]
    else:   # written by hand: the records:2 data elements it writes
        written = list(dict.fromkeys(re.findall(r'<data\s+name="([^"{]+)"', xslt.get('data') or '')))
    missing = [name for name in written if name not in have]
    if missing and not plan:
        raise ToolError(f"Index '{index.get('name')}' has {'no fields' if not have else 'no field'} for "
                        f"{missing[:10]}{' ...' if len(missing) > 10 else ''}, which the indexing XSLT writes: Stroom "
                        f"would drop those values ('Attempt to index unknown field') and the searches would find "
                        f"nothing. Save the indexing XSLT from its plan (save_xslt index_plan=the plan from "
                        f"draft_index_mapping, no code): the plan is kept with it, and its fields are then added to the "
                        f"index here. Or create the index with them: create_index_doc plan=....")
    return missing, plan


async def create_indexing_pipeline(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Pipeline name, e.g. 'Acme - Indexing'.")],
        template_uuid: Annotated[str, Field(description="Indexing template (find_pipeline_templates stage=indexing).")],
        xslt_uuid: Annotated[str, Field(description="The indexing XSLT, e.g. created from draft_index_mapping's draft.")],
        index_uuid: Annotated[str | None, Field(description="Lucene: the Index doc.")] = None,
        index_name: Annotated[str | None, Field(description="Elasticsearch: the index or data stream name.")] = None,
        cluster_uuid: Annotated[str | None, Field(
            description="Elasticsearch: the cluster, if the template does not already set one.")] = None,
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(
            description="Events streams this pipeline will index (stage 1's output, or an existing Events feed's). "
                        "Needed when the build has no events pipeline of its own.")] = [],
        confirmation_id: Annotated[str | None, Field(description="From an earlier needs_confirmation reply.")] = None,
) -> dict[str, Any]:
    """
    Stage 2: create an indexing pipeline as a child of an indexing template, setting its XSLT and where it
    indexes: the Lucene Index doc, or the Elasticsearch index name (and cluster if the template leaves it
    open). It reads Events streams, so it comes after the events pipeline (stage 1) has produced them, or
    takes an existing Events feed's streams as events_stream_ids.
    """
    stroom = gateway_from(ctx)
    shape = await _shape(stroom, template_uuid)
    if shape['stage'] not in ('indexing', 'discovery'):
        raise ToolError(f"That template is a {shape['stage']} template, not an indexing one")
    if shape['stage'] == 'indexing':
        await _events_available(ctx, build, events_stream_ids)
    xslt_element = next((s['element'] for s in shape['child_must_supply'] if s['type'] == 'XSLTFilter'), 'xsltFilter')
    props = [PropertyValue(element=xslt_element, name='xslt', doc_uuid=xslt_uuid, doc_type='XSLT')]
    open_props = {(s['element'], s['property']) for s in shape['child_must_supply']}
    missing, plan = [], None
    if shape['backend'] == 'lucene':
        if not index_uuid:
            raise ToolError("This template indexes into Lucene: give index_uuid")
        missing, plan = await _missing_index_fields(stroom, index_uuid, xslt_uuid)
        element = next(e for e, p in open_props if p == 'index')
        props.append(PropertyValue(element=element, name='index', doc_uuid=index_uuid, doc_type='Index'))
    else:
        if not index_name:
            raise ToolError("This template indexes into Elasticsearch: give index_name")
        element = next((e for e, p in open_props if p == 'indexName'), 'elasticIndexingFilter')
        props.append(PropertyValue(element=element, name='indexName', value=index_name))
        if (element, 'cluster') in open_props:
            if not cluster_uuid:
                raise ToolError("The template leaves the cluster open: give cluster_uuid")
            props.append(PropertyValue(element=element, name='cluster', doc_uuid=cluster_uuid, doc_type='ElasticCluster'))
    result = await create_pipeline(ctx, name, template_uuid, props, build=build, confirmation_id=confirmation_id,
                                   accept_parser_mismatch=True)   # an indexing pipeline reads Events, not the raw sample
    if result.get('uuid'):
        result['backend'] = shape['backend']
        if shape['backend'] == 'lucene' and missing:
            # The index lacks fields the XSLT's plan writes (made without plan=): the agreed plan supplies them.
            result['index_fields_added'] = (await set_index_fields(ctx, index_uuid, plan))['added']
    return result


async def test_elastic_index(ctx: Context, index_uuid: Annotated[str, Field(description="Elastic Index doc.")]) -> dict[str, Any]:
    """Elasticsearch: Stroom's own connection and index test for an Elastic Index doc."""
    stroom = gateway_from(ctx)
    return {'result': await stroom.post('/elasticIndex/v1/testIndex', await stroom.get_doc('ElasticIndex', index_uuid))}


# Fields the search API's TableSettings accepts; a dashboard's table component carries more (e.g.
# selectionHandlers, pageSize) that the search request rejects with "Unable to process JSON".
_TABLE_SETTINGS = {'aggregateFilter', 'applyValueFilters', 'conditionalFormattingRules', 'extractValues',
                   'extractionPipeline', 'fields', 'maxResults', 'maxStringFieldLength', 'modelVersion',
                   'overrideMaxStringFieldLength', 'queryId', 'showDetail', 'valueFilter', 'visSettings', 'window'}


def _column(name: str) -> dict[str, Any]:
    return {'id': str(uuidlib.uuid4()), 'name': name, 'expression': '${' + name + '}', 'visible': True,
            'width': 150, 'format': {'type': 'GENERAL'}}


async def create_verification_dashboard(
        ctx: Context,
        build: Build,
        name: Annotated[str, Field(description="Dashboard name, e.g. the index name with a -VERIFY suffix.")],
        index_uuid: Annotated[str, Field(description="The index doc to query.")],
        backend: Backend,
        fields: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Minimal field set: StreamId, EventId, the time field and a "
                                                       "few key fields.")],
) -> dict[str, Any]:
    """A workspace dashboard with a query on the index doc and a table of the given fields, for verify_index."""
    stroom = gateway_from(ctx)
    index = await stroom.get_doc(INDEX_TYPE[backend], index_uuid)
    source = {'type': INDEX_TYPE[backend], 'uuid': index_uuid, 'name': index.get('name')}
    query_id, table_id = 'query-VERIFY', 'table-VERIFY'
    table = {'type': 'table', 'queryId': query_id, 'fields': [_column(f) for f in fields], 'extractValues': False,
             'maxResults': [1000], 'pageSize': 100}
    config = {'components': [
        {'type': 'query', 'id': query_id, 'name': 'Query', 'settings': {
            'type': 'query', 'dataSource': source, 'expression': {'type': 'operator', 'op': 'AND', 'children': []},
            'automate': {'open': False, 'refresh': False}}},
        {'type': 'table', 'id': table_id, 'name': 'Table', 'settings': table}],
        'layout': {'type': 'splitLayout', 'dimension': 1, 'children': [
            {'type': 'tabLayout', 'tabs': [{'id': query_id, 'visible': True}], 'selected': 0},
            {'type': 'tabLayout', 'tabs': [{'id': table_id, 'visible': True}], 'selected': 0}]}}
    ref = await guard_from(ctx).create('Dashboard', name, build)
    doc = await stroom.get_doc('Dashboard', ref['uuid'])
    doc['dashboardConfig'] = config
    doc = await stroom.put_doc(doc)
    return {'type': 'Dashboard', 'uuid': doc['uuid'], 'name': doc['name'], 'data_source': source, 'fields': fields}


async def _search(ctx: Context, dashboard: dict[str, Any], expression: dict[str, Any]) -> dict[str, Any]:
    stroom = gateway_from(ctx)
    components = dashboard['dashboardConfig']['components']
    query = next(c for c in components if c['type'] == 'query')
    table = next(c for c in components if c['type'] == 'table')
    settings = table['settings']
    request = {
        'searchRequestSource': {'sourceType': 'DASHBOARD_UI', 'componentId': query['id'],
                                'ownerDocRef': {'type': 'Dashboard', 'uuid': dashboard['uuid'], 'name': dashboard['name']}},
        'search': {'dataSourceRef': query['settings']['dataSource'], 'expression': expression, 'incremental': True,
                   'componentSettingsMap': {table['id']: settings}},
        'componentResultRequests': [{'type': 'table', 'componentId': table['id'], 'fetch': 'ALL',
                                     'requestedRange': {'offset': 0, 'length': 100}, 'tableName': table['name'],
                                     'tableSettings': {k: v for k, v in settings.items() if k in _TABLE_SETTINGS}}],
        'dateTimeSettings': {'localZoneId': 'UTC', 'referenceTime': int(time.time() * 1000)},
        'storeHistory': False, 'timeout': 5000}
    started = time.monotonic()
    while True:
        result = await stroom.post('/dashboard/v1/search', request)
        if result.get('complete') or time.monotonic() - started > 60:
            break
        request['queryKey'] = result.get('queryKey')
        await asyncio.sleep(0.5)
    table_result = next((r for r in result.get('results') or [] if r.get('componentId') == table['id']), {})
    columns = [f['name'] for f in settings['fields']]
    rows = [dict(zip(columns, row.get('values') or [])) for row in table_result.get('rows') or []]
    return {'rows': rows, 'errors': (result.get('errors') or []) + (table_result.get('errors') or [])}


async def run_test_searches(
        ctx: Context,
        dashboard_uuid: Annotated[str, Field(description="A verification dashboard.")],
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams that were indexed.")],
        expected_documents: Annotated[int, Field(description="Events records in those streams.")],
        exact: Annotated[list[dict[str, str]] | str, ONE_OR_MORE, Field(
            description="Exact-match checks, each {'field': ..., 'value': ...} using values from stepped documents; "
                        "each must return at least one row.")] = [],
        time_range: Annotated[dict[str, Any] | None, Field(
            description="{'field': ..., 'from': ISO, 'to': ISO, 'expected': n}")] = None,
        retries: Annotated[int, Field(ge=0, le=20, description="Retries while the index catches up.")] = 6,
) -> dict[str, Any]:
    """
    Run test searches through the verification dashboard, the way people will search: all documents for the
    indexed stream ids (count must match), an exact match per key field, and a time range. Each check passes
    or fails with the rows it returned. A failure points at the mapping or the indexing XSLT.
    """
    dashboard = await gateway_from(ctx).get_doc('Dashboard', dashboard_uuid)
    term = lambda f, c, v: {'type': 'term', 'field': f, 'condition': c, 'value': str(v)}
    by_stream = {'type': 'operator', 'op': 'OR', 'children': [term('StreamId', 'EQUALS', i) for i in stream_ids]}
    for attempt in range(retries + 1):
        found = await _search(ctx, dashboard, by_stream)
        if len(found['rows']) >= expected_documents or attempt == retries:
            break
        await asyncio.sleep(5)
    checks = [{'check': f'documents for streams {stream_ids}', 'expected': expected_documents,
               'returned': len(found['rows']), 'pass': len(found['rows']) == expected_documents,
               'errors': found['errors'], 'sample': found['rows'][:3]}]
    for item in exact:
        res = await _search(ctx, dashboard, {'type': 'operator', 'op': 'AND', 'children': [
            term(item['field'], 'EQUALS', item['value'])]})
        checks.append({'check': f"{item['field']} = {item['value']}", 'returned': len(res['rows']),
                       'pass': len(res['rows']) >= 1, 'errors': res['errors'], 'sample': res['rows'][:2]})
    if time_range:
        res = await _search(ctx, dashboard, {'type': 'operator', 'op': 'AND', 'children': [
            term(time_range['field'], 'BETWEEN', f"{time_range['from']},{time_range['to']}")]})
        checks.append({'check': f"{time_range['field']} between {time_range['from']} and {time_range['to']}",
                       'expected': time_range.get('expected'), 'returned': len(res['rows']),
                       'pass': len(res['rows']) == time_range.get('expected', len(res['rows'])), 'errors': res['errors']})
    return {'passed': all(c['pass'] for c in checks), 'checks': checks}


async def _destination(ctx: Context, pipeline_uuid: str) -> dict[str, Any]:
    destination = await elastic_destination(gateway_from(ctx), pipeline_uuid)
    if not destination or not destination.get('index name'):
        raise ToolError("That is not an Elasticsearch indexing pipeline with an indexName set")
    return destination


async def _documents(ctx: Context, pipeline_uuid: str, stream_ids: list[int], cap: int) -> list[dict[str, Any]]:
    """The documents the indexing pipeline would send to Elasticsearch, by stepping its XSLT over Events."""
    stroom = gateway_from(ctx)
    pipeline = await _Pipeline.load(stroom, pipeline_uuid)
    outputs = await _outputs(stroom, pipeline, stream_ids, pipeline.default_outputs()[-1], None, cap)
    docs = [d for xml in outputs.values() for d in json_xml_documents(xml)]
    if not docs:
        raise ToolError("Stepping the indexing pipeline gave no documents; step_sample it first and fix its XSLT")
    return docs


def _component_notes(body: dict[str, Any]) -> list[str]:
    """This server doesn't read Elasticsearch, so fields from component templates can't be checked."""
    names = body.get('composed_of') or []
    return [f"composed_of {names} not checked: fields from those component templates aren't seen here; ask the "
            f"user whether they map any field the pipeline writes"] if names else []


async def propose_index_template(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The candidate Elasticsearch indexing pipeline.")],
        plan: Annotated[FieldPlan, Field(description="The field plan from draft_index_mapping (backend elasticsearch).")],
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams to check the template against.")],
        template_name: Annotated[str | None, Field(description="Template name; defaults to the index name.")] = None,
        priority: Annotated[int, Field(ge=0)] = 200,
) -> dict[str, Any]:
    """
    Suggest the index template for the user to commit, when the indexing pipeline is ready: rendered from the
    field plan for the pipeline's own destination index, as JSON and as a Kibana Dev Tools request, already
    checked against the documents the pipeline writes. Show it to the user and ask them to commit it, or to
    send back their changed version (check that with check_index_template). Writes nothing.
    """
    if plan.backend != 'elasticsearch':
        raise ToolError("Index templates are for Elasticsearch; Lucene fields are set with create_index_doc (plan=...)")
    destination = await _destination(ctx, pipeline_uuid)
    index = destination['index name']
    name = template_name or index
    body = plan.model_copy(update={'index_name': index}).elastic_template(name, priority)['body']
    stroom = gateway_from(ctx)
    check = compare(body, await _documents(ctx, pipeline_uuid, events_stream_ids, 50), index)
    text = json.dumps(body, indent=2)
    return {'template_name': name, 'index': index, 'cluster': destination['cluster'], 'template': body,
            'dev_tools': f"PUT _index_template/{name}\n{text}", 'self_check': check,
            'pipeline_link': doc_link(stroom.settings, 'Pipeline', pipeline_uuid),
            'hint': ("Show the user dev_tools and ask them to commit it to Elasticsearch, or to send back their "
                     "changed template; check changes with check_index_template before going on.")}


async def check_index_template(
        ctx: Context,
        pipeline_uuid: Annotated[str, Field(description="The candidate Elasticsearch indexing pipeline.")],
        template: Annotated[str, Field(description="The template as the user gave it: a Dev Tools request "
                                                   "(PUT _index_template/name {...}), the JSON body, or GET output.")],
        events_stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams to step the pipeline over.")],
        max_records: Annotated[int, Field(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """
    Check a user's (possibly changed) index template against the candidate indexing pipeline: does it
    apply to the pipeline's index, and can it take every field the pipeline writes (types, date formats,
    dynamic setting, object clashes, renamed or dropped fields)? Returns whether it is compatible and each
    change needed, mostly to the indexing XSLT, to flag to the user before anything is changed.
    """
    try:
        name, body = parse_template(template)
    except ValueError as e:
        raise ToolError(str(e)) from e
    destination = await _destination(ctx, pipeline_uuid)
    result = compare(body, await _documents(ctx, pipeline_uuid, events_stream_ids, max_records),
                     destination['index name'])
    result['notes'] = _component_notes(body) + result['notes']
    result.update({'template_name': name, 'index': destination['index name'], 'cluster': destination['cluster'],
                   'pipeline_link': doc_link(gateway_from(ctx).settings, 'Pipeline', pipeline_uuid),
                   'hint': ("Compatible: ask the user to commit it, then create_processor_filter." if result['compatible']
                            else "Show the user pipeline_changes and ask whether to make them (update the indexing "
                                 "XSLT, step again) or to change the template instead. Nothing has been changed.")})
    return result


async def verify_index(
        ctx: Context,
        build: Build,
        index_uuid: Annotated[str, Field(description="The index doc that was indexed into.")],
        backend: Backend,
        stream_ids: Annotated[list[int] | int | str, ONE_OR_MORE, Field(description="Events streams that were indexed.")],
        expected_documents: Annotated[int, Field(description="Events records in those streams.")],
        fields: Annotated[list[str] | str, ONE_OR_MORE, Field(description="Minimal field set for the dashboard: StreamId, EventId, "
                                                       "the time field and a few key fields.")],
        exact: Annotated[list[dict[str, str]] | str, ONE_OR_MORE, Field(
            description="Exact-match checks, each {'field': ..., 'value': ...} using values from stepped documents; "
                        "each must return at least one row.")] = [],
        time_range: Annotated[dict[str, Any] | None, Field(
            description="{'field': ..., 'from': ISO, 'to': ISO, 'expected': n}")] = None,
        dashboard_name: Annotated[str | None, Field(description="Defaults to the index name with a -VERIFY suffix.")] = None,
        retries: Annotated[int, Field(ge=0, le=20, description="Retries while the index catches up.")] = 6,
) -> dict[str, Any]:
    """
    Verify indexed events through Stroom, not by querying the backend: a workspace dashboard on the index doc
    (created once per build, with a table of the given fields), then the test searches: the sample stream
    ids, an exact match on each key field, and a time range. Passes when every check returns what it should.
    """
    stroom = gateway_from(ctx)
    index = await stroom.get_doc(INDEX_TYPE[backend], index_uuid)
    name = dashboard_name or f"{index.get('name')}-VERIFY"
    existing = next((d for d in await guard_from(ctx).folder_contents(build) if d['type'] == 'Dashboard' and d['name'] == name), None)
    dashboard = existing or await create_verification_dashboard(ctx, build, name, index_uuid, backend, fields)
    searched = await run_test_searches(ctx, dashboard['uuid'], stream_ids, expected_documents, exact, time_range, retries)
    return {'dashboard': {'uuid': dashboard['uuid'], 'name': name}, **searched}


ALL_TOOLS = [get_field_conventions, draft_index_mapping, propose_index_template, check_index_template,
             find_elastic_clusters, create_index_doc, create_indexing_pipeline, verify_index]
