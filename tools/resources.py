"""MCP resources (reference guides, convention profiles) and prompts (packaged workflows)."""
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / 'knowledge' / 'guides'

_RULES = """Rules for every run:
- Start with get_instructions (with the folders, feeds or documents involved): standing instructions from AGENTS
  docs in Stroom. Follow them, the most specific last. The user's request takes precedence, and they never lift an
  approval.
- Build everything in one build (start_build, with the feeds when known: it returns the standing instructions
  too); nothing leaves the workspace until promote_build is approved. Before promoting, list_build shows what
  the build's pipelines still lack (a clean step of their current code, documentation): resolve it first.
- Confirm key details with the user when a tool returns needs_confirmation, and ask for approval when it returns
  needs_approval: show the summary, and only pass the id back once the user has agreed.
- Never assume a field convention, cluster, index name, template or feed name: propose one with where it came
  from, and let the user confirm or correct it.
- Step every sample record (step_sample) before processing; fix blocking groups and treat review groups as
  questions to resolve. Draft code is tried with draft_code before anything is saved.
- Document what you build (write_documentation) and use the user's source notes for field meanings.
- While developing a pipeline in the build, reprocess sample streams after a fix with reprocess_streams (at most
  10 per call, one task at a time) and wait_for_processing with its filter_id. Reprocessing production data, and
  switching readers from one index version to the next, are the user's.
- A translation pipeline only processes the build's own feeds: step production records in place (step_records) or
  copy them into a test feed. Promotion pre-creates each pipeline's filter for new data, disabled: give the user
  the pipeline link to review and enable it.
- Elasticsearch indexing runs only through the Stroom indexing pipeline, and only after the user confirms that
  the index template for the destination index has been written."""


# Sent to every client at connection (serverInfo.instructions), so the rules hold whichever client runs the model
# and whether or not it uses the prompts.
SERVER_INSTRUCTIONS = f"""Stroom MCP server: builds Stroom content (feeds, translation and indexing pipelines) from raw data, through tools
that enforce the rules; the prompts (onboard_data_source, onboard_existing_feed, update_events_pipeline, ...) are the
workflows, and stroom://guide/* the reference.

Order of work for a new source, always: stage 1, the events pipeline (profile_sample, template, feed, translation
XSLT, step every record, process, validate the Events), then stage 2, indexing, which reads the Events streams
stage 1 produced. Never start with an indexing pipeline for raw data; create_indexing_pipeline refuses until the
build has an events pipeline or is given existing Events streams.

Samples: ask for every sample file the user has and give them all to profile_sample (samples by file name): it
reports what differs between files. Upload each file as its own stream, step them all, and survey_feed with those
stream_ids shows every kind of event the sample holds; map variants with any_of.

Parsing: profile_sample names the parser. JSON (an array, or one object per line) is parsed by the Event Data
(JSON) template's JSONParser element with no text converter; a Data Splitter is for text (CSV, syslog, key=value).
XML fragments (several root elements, e.g. one <Event> per line, no root) take an XMLFragmentParser with an
XML_FRAGMENT wrapper converter: a template with one, else create_pipeline from Event Data (XML) with
replace_parser='XMLFragmentParser'; fragments without a namespace take the wrapper's, records:2. The XSLT reads
the parser's output in its namespace (records:2 for a Data Splitter; http://www.w3.org/2013/XSL/json for JSON, root
/map for JSON lines, /array for an array), so set xpath-default-namespace to it.

Text formats: write the Data Splitter with build_data_splitter from a spec (delimited, regex, key=value, syslog with a
parsed body), which runs it on the sample and shows the records and field names; never by hand first.

Translation: write the XSLT with build_translation_xslt from a mapping, not by hand, and pass it the sample (and the
splitter spec) so fields no record has and time formats the values do not fit are caught before stepping. Sources:
field, any_of (first of several names), value, xpath, lookup (reference data); modifiers: transform (lower, upper,
trim, strip_domain, domain, digits), dictionary, map, default, time_format. Values the record does not carry come
from reference data (find_reference_data; build_reference_xslt and a Reference Data pipeline for new tables; the
events pipeline names the feed in references) or from a Dictionary doc (create_dictionary). Text fields holding several
values (a message string with a time, user, action and description) are parsed with the mapping's extract (a
regular expression whose groups become fields), not with substring-before/after chains; JSON held in a string is
read with an xpath using json-to-xml(). Only stroom: functions that exist may be used (format-date, lookup, meta,
dictionary, log, json-to-xml, ...; there is no stroom:json-parse); check_xslt refuses unknown ones, elements the
event-logging schema has no place for (e.g. EventDetail/ServerEvent: EventDetail holds TypeId, Description and one
action element such as Authenticate, Process, View, Alert, Unknown), and match/select expressions that would
select nothing for want of a namespace.

{_RULES}"""


def _docs(source_docs: str) -> str:
    return (f"\n\nThe user supplied this source documentation. Record it with record_source_notes and use it for "
            f"field meanings and event types:\n{source_docs}") if source_docs else ''


def register(mcp: FastMCP, conventions_dir: Path = ROOT / 'conventions') -> None:
    @mcp.resource('stroom://guide/{name}', mime_type='text/markdown',
                  description="Working guides: event-logging, xslt, data-splitter, json-input, reference-data, indexing, "
                              "documentation, agent-instructions.")
    def guide(name: str) -> str:
        path = GUIDES / f'{name}.md'
        if not path.is_file() or path.parent != GUIDES:
            raise ResourceError(f"No guide '{name}'. Guides: {', '.join(sorted(p.stem for p in GUIDES.glob('*.md')))}")
        return path.read_text(encoding='utf-8')

    @mcp.resource('stroom://guides', mime_type='text/markdown', description="Index of the working guides.")
    def guides() -> str:
        lines = ['# Guides', '']
        for path in sorted(GUIDES.glob('*.md')):
            title = path.read_text(encoding='utf-8').splitlines()[0].lstrip('# ')
            lines.append(f'- `stroom://guide/{path.stem}`: {title}')
        return '\n'.join(lines)

    @mcp.resource('stroom://conventions/{name}', mime_type='text/yaml', description="A field convention profile.")
    def convention(name: str) -> str:
        path = conventions_dir / f'{name}.yaml'
        if not path.is_file() or path.parent != conventions_dir:
            raise ResourceError(f"No convention '{name}'")
        return path.read_text(encoding='utf-8')

    @mcp.prompt(description="Onboard a new data source: sample to events, then indexing, then promotion.")
    def onboard_data_source(sample: str, source_name: str, vendor: str = '', source_docs: str = '') -> str:
        return f"""Onboard "{source_name}"{f' from {vendor}' if vendor else ''} into Stroom.

Stage 1, events:
1. Ask whether there are more sample files than the one below (other appliances, versions or days) and get them all:
   profile_sample with samples by file name reports the fields and timestamp shapes that differ between them.
   start_build with a build name for this source (and the feed, once named).
2. find_pipeline_templates stage=translation; list_template_children and describe_template_contract on the best
   candidate to see how this environment specialises it. find_similar_translations for existing XSLTs to reuse.
3. Propose the feed name (following sibling feeds' naming) and create_feed; upload_sample once per file, so each is a
   stream. survey_feed with those stream_ids (and the build) lists the kinds of event the sample holds and where, so
   every kind gets a rule and step_records can check each. Values the records do not carry (a user's department, a
   host's site): find_reference_data for maps the environment loads, or build the reference data (reference-data guide).
4. Text formats need a Data Splitter (DSParser.textConverter): build_data_splitter from a spec, with the sample, until
   every line parses; XML fragments need the wrapper (xmlFragmentParser.textConverter). JSON and single-document XML
   need none: the JSONParser or XMLParser reads them. Build the XSLT with build_translation_xslt (feeds=[the feed],
   sample=all the files, splitter=the spec) from a mapping: which input field or constant goes to which event-logging
   path, one rule per kind of event, time patterns from profile_sample, any_of where files name a field differently,
   extract for text fields holding several values, lookup or dictionary for values from reference data,
   json_layout from profile_sample for JSON. Fix reported problems in the mapping and regenerate; hand-edit only
   what a mapping cannot express. step_sample with draft_code over every sample stream until the verdict is clean;
   step_pipeline on single records to debug.
5. create_text_converter (if any) / create_xslt, create_pipeline from the template (with the pipeline_properties
   build_translation_xslt returned, e.g. jsonParser.addRootObject, and references for any lookup maps), step_sample
   again over every sample stream.
6. create_processor_filter on all the sample stream ids, wait_for_processing (gate: one Events stream per raw stream),
   validate_events and check_event_quality on the output.

Stage 2, indexing:
7. find_pipeline_templates stage=indexing gives the backend (Lucene or Elasticsearch). get_field_conventions;
   ask the user which convention to follow. For Elasticsearch, find_elastic_clusters.
8. Propose, in one message, the backend, cluster or volume group, convention, indexing template and index name
   (following the environment's versioned naming); create_index_doc once confirmed.
9. draft_index_mapping; set_index_fields (Lucene); create_xslt with the drafted indexing XSLT;
   create_indexing_pipeline; step_sample on the Events streams.
10. Elasticsearch: propose_index_template and show the user its dev_tools request. If they send back a changed
    template, check_index_template; if it is not compatible, show the pipeline changes it needs and ask whether to
    make them (update the indexing XSLT, step again, check again) or to change the template instead.
11. create_processor_filter on the Events stream ids with source_pipeline_uuid = the events pipeline from stage 1
    (the filter then only selects Events from that pipeline). Elasticsearch: the user confirms they have committed
    the template, the filter is created disabled, and you give them pipeline_link and say it is ready to enable;
    once they have enabled it, continue. Then wait_for_processing expect_events=false, create_verification_dashboard
    and run_test_searches.

Finish: write_documentation for both pipelines, then promote_build to the folders sibling sources use.

{_RULES}{_docs(source_docs)}

Sample:
{sample}"""

    @mcp.prompt(description="Fix or extend an existing events pipeline (new version or in place).")
    def update_events_pipeline(pipeline: str, samples: str = '', issue: str = '', source_docs: str = '') -> str:
        return f"""Update the events pipeline "{pipeline}".{f' Reported issue: {issue}' if issue else ''}

1. Find it and describe_pipeline; get_document its XSLT; describe_translation.
2. Ask the user whether this is a new version (e.g. V1.2 to V1.3) or an in-place change, and confirm the names.
   New version: copy_pipeline with rename (e.g. {{'V1.2': 'V1.3'}}). In place: copy_pipeline working_copy=true.
3. Test records: new samples go to a test feed in the build (create_feed, upload_sample), never the production feed;
   for a reported issue, find example records in the production feed (find_streams, read_stream, step_pipeline).
4. Draft the change and prove it with compare_outputs (draft_code) on the test records and recent production
   records: only the targeted fields may change. step_sample must stay clean.
5. update_xslt on the copy; write_documentation noting the change; promote_build (approval).
   Reprocessing historical data is the user's decision: propose it, do not do it.

{_RULES}{_docs(source_docs)}{f'''

Samples:
{samples}''' if samples else ''}"""

    @mcp.prompt(description="Create the next version of an indexing pipeline with field changes.")
    def update_indexing_pipeline(indexing_pipeline: str, changes: str) -> str:
        return f"""Create the next version of the indexing pipeline "{indexing_pipeline}" with these changes: {changes}

1. describe_pipeline to find its XSLT, index doc and index name; work out the next version from the naming
   convention (e.g. -v1 to -v2) and confirm it and the cluster or volume group with the user.
2. draft_index_mapping with the requested fields as extra_fields; create_index_doc for v2; set_index_fields, or
   propose_index_template for v2 (check the user's changes with check_index_template); create_xslt with the new
   indexing XSLT.
3. copy_pipeline with set_properties for the new XSLT and index; compare_outputs against v1 on recent Events
   streams: only the requested fields may differ.
4. Process sample Events, wait_for_processing expect_events=false, verification dashboard and run_test_searches.
5. write_documentation, promote_build. v1 stays running; switching readers to v2, and retiring v1, is the user's.

{_RULES}"""

    @mcp.prompt(description="Index an existing Events feed (stage 2 only).")
    def index_event_data(events_feed: str, index_pattern: str = '') -> str:
        return f"""Index the events in feed "{events_feed}"{f' into {index_pattern}' if index_pattern else ''}.
Find recent Events streams (find_streams feed={events_feed} stream_type=Events) and run stage 2 of
onboard_data_source on them: backend and convention with the user, index doc, field plan, indexing pipeline,
stepping, processing, verification searches, documentation, promotion.

{_RULES}"""

    @mcp.prompt(description="Index raw structured data directly into a discovery index.")
    def create_discovery_index(feed: str = '', sample: str = '', timestamp_field: str = '') -> str:
        return f"""Create a discovery index for {'feed ' + feed if feed else 'the sample below'}: raw structured data
indexed as it is, for exploration, without an event-logging translation.

1. find_pipeline_templates stage=discovery. profile_sample (and get_stream_attributes on a raw stream) to propose
   enrichments: embedded JSON to unpack with json-to-xml(), stream meta to add with stroom:meta().
2. Confirm cluster, index name, template, timestamp field{f' ({timestamp_field})' if timestamp_field else ''} and
   enrichments with the user in one message.
3. The XSLT copies the parser's JSON XML (namespace http://www.w3.org/2013/XSL/json) into the xpath-functions
   namespace the indexing filter reads, adding StreamId, EventId and @timestamp and the enrichments.
4. Step every sample record, propose the index template (propose_index_template; check_index_template for the
   user's changes), pre-create the filter disabled once they have committed it and give them the pipeline link, then
   verify with a dashboard and test searches once they have enabled it; document, promote.

{_RULES}{f'''

Sample:
{sample}''' if sample else ''}"""

    @mcp.prompt(description="Evaluate and document an existing events pipeline.")
    def evaluate_events_pipeline(pipeline: str, sample_size: int = 50, source_docs: str = '') -> str:
        return f"""Evaluate the Stroom events pipeline "{pipeline}" and write a report. Change nothing except the report.

1. Describe the pipeline: find it (find_documents), then describe_pipeline for its template chain,
   elements, what it overrides or removes, and its reference data. get_document its XSLT and text converter.
   processing_status shows which feeds its processor filters cover.
2. Sample the data: find_streams for recent Raw Events on each of those feeds, and the Events and Error
   streams the pipeline produced from them (get_stream_children). Step up to {sample_size} raw records
   with step_sample, and look at one or two records in detail with step_pipeline.
3. Map the translation: describe_translation on its XSLT. Compare input_fields with the fields in the
   raw data (read_stream, profile_sample) to find inputs that are never used.
4. Inventory the events: summarise_events on its Events streams.
5. Measure conformance: validate_events and check_event_quality on sample Events records;
   summarise_errors on recent raw streams it processed. Give rates, e.g. "97% valid".
6. Suggest improvements, prioritised, each with the rule or field it fixes, the share of events affected,
   and a draft XSLT change.

Report sections: Purpose and data; Processing; Field mapping; Event types; Schema conformance; Suggestions.
Return it in the chat and save it with write_documentation in a build, then promote_build beside the
pipeline once the user approves.{_docs(source_docs)}"""

    @mcp.prompt(description="Build an events pipeline for a feed that already holds data.")
    def onboard_existing_feed(feed: str, source_docs: str = '') -> str:
        return f"""Build an events pipeline for the existing feed "{feed}" from the data it already holds, by stepping only.

1. start_build feeds=[{feed}], then survey_feed feed={feed} build=<the build>: it samples streams spread over the feed's lifetime, reading only the head of each (big
   streams are fine), and groups records into kinds of event (shapes), with counts, examples and where each example
   is, and keeps them in the build's '{feed} - Survey' doc. Not every kind appears in every stream.
2. Draft the text converter if needed and the translation with build_translation_xslt (feeds=[{feed}]), one rule per shape, then
   create_pipeline and step_records over the survey's locations until clean: the feed's own records are stepped
   where they are. Create no feed, upload nothing and process nothing.
3. survey_feed again with the same build: it carries on from the survey doc (streams read, shapes known). New
   shapes: add rules, regenerate, and step_records over every location so far. Repeat until a survey's coverage
   says covered, or every stream has been read. Until then, say plainly that the feed is not covered yet.
   Kinds that may not be worth translating (housekeeping, debug, noise): show them with their share and ask the
   user. Never drop records on your own judgement. If the user agrees, set_shape_handling handling=drop with their
   reason (they confirm it), add a drop rule for those kinds to the mapping (drop=true, conditions that match them),
   and step_records: their locations expect no Event and count as clean when none is written.
4. Broad check: step_sample over three of the surveyed streams (newest, oldest, middle) with records_per_stream=200,
   to catch variants the examples did not show; fix and step again until clean. Then tell the user which kinds of
   event the pipeline covers and their share of the data.
5. write_documentation and promote_build. Processing the source feed with the promoted pipeline is the user's to
   start; once its Events exist, index_event_data builds the indexing.

{_RULES}{_docs(source_docs)}"""

    @mcp.prompt(description="Diagnose a reported problem in an events pipeline and propose a fix.")
    def fix_pipeline_issue(stream_id: int, issue: str, event_id: int | None = None) -> str:
        where = f"event {event_id} of stream {stream_id}" if event_id else f"stream {stream_id}"
        return f"""The user reports a problem with {where}: {issue}

1. Locate it: locate_event(stream_id={stream_id}{f', event_id={event_id}' if event_id else ''}) gives the raw
   stream, part and record, the pipeline, and its XSLTs and text converters (some may be inherited from a template).
   Without an event id, use summarise_events on the Events stream to find the event type the user means,
   then step_pipeline on raw records until you find an example.
2. Plan the validation, and say it to the user in two or three lines: what output the record should give
   (from the user's words, the event-logging schema and any source docs), which field paths are wrong now, and
   which other records to check (recent raw streams on the same feed, find_streams; the same event type).
3. Confirm the issue: step_pipeline on the located record (with part), validate_events and check_event_quality
   on the output. If you cannot reproduce it, say what you found and ask the user; do not guess a fix.
4. Draft the fix in the pipeline's own XSLT or text converter and try it with step_pipeline draft_code. Then
   summarise_fix with the reported raw stream plus a few recent ones and expected_paths set to the fields the fix
   should change. Revise until ready is true.
5. Present the fix: the diff, the fields that change and how many records, and any template warning. Ask whether
   to apply it to the pipeline, or to give them the manual steps.
   - Apply: follow update_events_pipeline (ask new version or in place, confirm names, copy_pipeline, update_xslt
     or update_text_converter with the draft, compare_outputs, write_documentation, promote_build).
   - Manual: give summarise_fix's manual_steps and diff.
   Either way, reprocessing production data is the user's to do.

{_RULES}"""
