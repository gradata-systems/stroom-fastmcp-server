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
  too); nothing leaves the workspace until promote_build is approved. Before promoting, build_status shows what
  the build's pipelines still lack (a clean step of their current code, documentation): resolve it first.
- Confirm key details with the user when a tool returns needs_confirmation, and ask for approval when it returns
  needs_approval: show the summary, and only pass the id back once the user has agreed.
- Never assume a field convention, cluster, index name, template or feed name: propose one with where it came
  from, and let the user confirm or correct it.
- Step every sample record (step_sample) before processing; fix blocking groups and treat review groups as
  questions to resolve. A generated XSLT is saved by build_translation_xslt (build, name) and stepped as saved;
  code written by hand is tried with draft_code before it is saved.
- Document what you build (write_documentation) and use the user's source notes for field meanings.
- While developing a pipeline in the build, reprocess sample streams after a fix with reprocess_streams (at most
  10 per call, one task at a time) and wait_for_processing with its filter_id. Reprocessing production data, and
  switching readers from one index version to the next, are the user's.
- A translation pipeline only processes the build's own feeds: step production records in place (step_records) or
  copy them into a test feed. Promotion pre-creates each pipeline's filter for new data, disabled: give the user
  the pipeline link to review and enable it.
- Elasticsearch indexing runs only through the Stroom indexing pipeline, with an index template the user agreed
  (built by propose_index_template from their example, or their correction), and only once they confirm the cluster
  admin has committed it to the cluster: the approval to start it asks them."""


# Sent to every client at connection (serverInfo.instructions), so the rules hold whichever client runs the model
# and whether or not it uses the prompts.
SERVER_INSTRUCTIONS = f"""Stroom MCP server: builds Stroom content (feeds, translation and indexing pipelines) from raw data, through tools
that enforce the rules; the prompts (onboard_data_source, onboard_existing_feed, update_events_pipeline, ...) are the
workflows, and stroom://guide/* the reference.

Order of work for a new source, always: start_onboarding (profiles every sample file, creates the build, returns
the plan), then stage 1, the events pipeline (feed, samples uploaded, template, translation XSLT saved with its
mapping, step every record, process, validate the Events, document), then stage 2, indexing, which reads the Events
streams stage 1 produced, then promotion. One tool call is never the whole job: every write tool's result carries
`next`, the plan's next step, and `done: false` until promotion; build_status shows every step's state. Keep going
until `next` says promote. Never start with an indexing pipeline for raw data; create_indexing_pipeline refuses
until the build has an events pipeline or is given existing Events streams; create_processor_filter refuses a
pipeline with no clean step recorded; create_pipeline refuses another source's XSLT and a template whose parser
cannot read the sample. Templates are inherited (create_pipeline), never copied: copy_pipeline refuses one, and is for a
new version or working copy of a source's own pipeline.

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

Translation: start from draft_translation_mapping (a valid mapping drafted from the sample: decide the action element
per kind of event, the system name and environment), then build_translation_xslt with the edited mapping, the sample
(and the splitter spec) so fields no record has and time formats the values do not fit are caught before stepping;
never write XSLT by hand, and never send the field inventory as the mapping. Sources:
field, any_of (first of several names), value, xpath, lookup (reference data); modifiers: transform (lower, upper,
trim, strip_domain, domain, digits), dictionary, map, default, time_format. Values the record does not carry come
from reference data (find_reference_data; build_reference_xslt and a Reference Data pipeline for new tables; the
events pipeline names the feed in references) or from a Dictionary doc (save_dictionary). Text fields holding several
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
1. Ask whether there are more sample files than the one below (other appliances, versions or days) and get them all.
   start_onboarding with every file by name: it profiles them (fields and timestamp shapes that differ between files,
   the parser and template, whether a text converter is needed), creates the build and returns the plan. Follow `next`
   in each result until it says promote; build_status shows what remains. A file's text is sent once more, to
   upload_sample; after that every tool takes the sample streams (stream_ids) and reads them itself.
2. Propose the feed name (following sibling feeds' naming) and create_feed; upload_sample once per file, so each is a
   stream. survey_feed with those stream_ids (and the build) lists the kinds of event the sample holds and where, so
   every kind gets a rule and step_records can check each. Values the records do not carry (a user's department, a
   host's site): find_reference_data for maps the environment loads, or build the reference data (reference-data guide).
3. Text formats need a Data Splitter (DSParser.textConverter): build_data_splitter with the sample streams infers
   the spec and runs it until every line parses; save_text_converter saves it. XML fragments need the wrapper
   (xmlFragmentParser.textConverter). JSON and single-document XML need none: the JSONParser or XMLParser reads them.
4. find_pipeline_templates stage=translation; describe_template on the best candidate. Its shared_xslt lists the
   shared XSLTs (xsl:import, found by name) its other pipelines' XSLTs use: each named template they call, where (at,
   e.g. EventSource/Device or Meta), what it writes and reads (stroom:meta) and the parameters they pass. Use the same
   ones: the mapping's shared entries (href, template, at, with_params), and map nothing below those elements, as
   the shared template writes them (twice fails validation). find_documents (content=...) for other XSLTs to reuse.
   draft_translation_mapping with the sample streams gives a
   mapping to edit (its notes say what to decide: the action element per kind of event, System Name, Environment, a
   time zone); then build_translation_xslt (feeds=[the feed], stream_ids=the sample streams, splitter=the spec) with
   it: which input field or constant goes to which event-logging path, one rule per kind of event, any_of where files
   name a field differently, extract for text fields holding several values, lookup or dictionary for values from
   reference data. Give it build and name: it saves the XSLT with the mapping (kept with it, so the documentation is
   generated from it) and returns the document, not the code. Fix reported problems in the mapping and call again
   with uuid= the saved XSLT; hand-edit only what a mapping cannot express.
5. create_pipeline from that template (with the pipeline_properties build_translation_xslt returned, e.g.
   jsonParser.addRootObject, and references for any lookup maps), then step_sample over every sample stream until the
   verdict is clean, fixing the mapping and saving again (uuid=) in between; step_pipeline on single records to debug.
6. create_processor_filter on all the sample stream ids, wait_for_processing (gate: one Events stream per raw stream),
   check_events on the output. Then write_documentation for the events pipeline with stream_ids = the sample streams:
   its Field mapping section is generated from the kept mapping.

Stage 2, indexing:
Two kinds of template, not to be confused: a Stroom pipeline template is a pipeline the indexing pipeline inherits
from (find_pipeline_templates, describe_template); an Elasticsearch index template defines the destination index's
mappings and settings on the cluster (propose_index_template, check_index_template).

7. find_pipeline_templates stage=indexing gives the backend (Lucene or Elasticsearch) and the Stroom pipeline
   template. get_field_conventions; ask the user which convention to follow. For Elasticsearch, find_elastic_clusters,
   and ask the user for an example: the Elasticsearch index template a sibling source's index uses (GET
   _index_template/<name>), or an existing index's mapping (GET <index>/_mapping), and, only if it lists any in
   composed_of, those component templates (GET _component_template/<name>). Many templates have none. The new
   index's template is built from them.
8. Propose, in one message, the backend, cluster or volume group, convention, Stroom pipeline template and index
   name (following the environment's versioned naming); create_index_doc once confirmed.
9. describe_template on the indexing template: if its shared_xslt shows sibling indexing XSLTs calling shared
   templates (a guid field, say), pass them to draft_index_mapping as shared; the XSLT calls them, not writing those
   fields itself.
   draft_index_mapping (Elasticsearch: with the user's example and any component templates, so field names follow
   theirs, e.g. User.Id for the user, TypeId for the event type; show its from_example notes); create_index_doc
   (plan=...) (Lucene); save_xslt with index_plan=plan and no code (it is generated from the plan);
   create_indexing_pipeline; step_sample on the Events streams.
10. Elasticsearch: propose_index_template with the user's example and any component templates builds the index
    template for the new index, following their naming, field type and structure conventions (its from_example
    notes say what came from where; names unlike the example's are renamed in the field plan, then build again).
    When it fits the documents, the user confirms it as shown. If they correct it instead, check_index_template
    with their version (and any component templates): when it fits, they confirm it there; when it does not, show
    the pipeline changes it needs and ask whether to make them (update the indexing XSLT, step again, check again)
    or to change the index template. The confirmed template is kept with the pipeline.
11. Elasticsearch: give the user the agreed template's dev_tools request for the cluster admin to commit to the
    cluster, and wait until they say it is committed; indexing must not start before, or the index is created
    without it. Then create_processor_filter on the Events stream ids with source_pipeline_uuid = the events
    pipeline from stage 1 (the filter then only selects Events from that pipeline): its approval asks the user to
    confirm the agreed template is committed, and processing starts. Lucene: create_processor_filter likewise.
    Then wait_for_processing expect_events=false, and verify_index. Then write_documentation for the indexing
    pipeline with stream_ids = its Events streams.

Finish: promote_build to the folders sibling sources use.

{_RULES}{_docs(source_docs)}

Sample:
{sample}"""

    @mcp.prompt(description="Fix or extend an existing events pipeline (new version or in place).")
    def update_events_pipeline(pipeline: str, samples: str = '', issue: str = '', source_docs: str = '') -> str:
        return f"""Update the events pipeline "{pipeline}".{f' Reported issue: {issue}' if issue else ''}

1. Find it (find_documents) and describe_document the pipeline and its XSLT.
2. Ask the user whether this is a new version (e.g. V1.2 to V1.3) or an in-place change, and confirm the names.
   New version: copy_pipeline with rename (e.g. {{'V1.2': 'V1.3'}}). In place: copy_pipeline working_copy=true.
3. Test records: new samples go to a test feed in the build (create_feed, upload_sample), never the production feed;
   for a reported issue, find example records in the production feed (find_streams, read_stream, step_pipeline).
4. Draft the change and prove it with compare_outputs (draft_code) on the test records and recent production
   records: only the targeted fields may change. step_sample must stay clean.
5. Save the change on the copy: build_translation_xslt (uuid=...) from the changed mapping, or save_xslt (uuid=...)
   for code a mapping cannot express; write_documentation noting the change; promote_build (approval).
   Reprocessing historical data is the user's decision: propose it, do not do it.

{_RULES}{_docs(source_docs)}{f'''

Samples:
{samples}''' if samples else ''}"""

    @mcp.prompt(description="Create the next version of an indexing pipeline with field changes.")
    def update_indexing_pipeline(indexing_pipeline: str, changes: str) -> str:
        return f"""Create the next version of the indexing pipeline "{indexing_pipeline}" with these changes: {changes}

1. describe_document to find its XSLT, index doc and index name; work out the next version from the naming
   convention (e.g. -v1 to -v2) and confirm it and the cluster or volume group with the user.
2. draft_index_mapping with the requested fields as extra_fields; create_index_doc for v2 (plan=... adds the Lucene fields), or
   propose_index_template for v2 (check the user's changes with check_index_template); save_xslt with the new
   indexing XSLT.
3. copy_pipeline with set_properties for the new XSLT and index; compare_outputs against v1 on recent Events
   streams: only the requested fields may differ.
4. Process sample Events, wait_for_processing expect_events=false, verification dashboard and verify_index.
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
        return f"""Create a discovery index for {'feed ' + feed if feed else 'the sample below'}: raw structured data (JSON)
indexed as it is into Elasticsearch, for exploration, without an event-logging translation.

Elasticsearch maps the source's fields dynamically as documents arrive, so do not survey the data first: no
profile_sample, no reading through the streams. Only StreamId, EventId and @timestamp are mapped explicitly.

1. The source: the feed's Raw Events streams, or the sample uploaded to a new workspace feed (create_feed,
   upload_sample). find_pipeline_templates stage=discovery for the Stroom pipeline template; find_elastic_clusters.
2. Ask the user, in one message: the cluster, the index name (e.g. stroom-discovery-<source>-v1), the Stroom
   pipeline template, the field holding the event time{f' ({timestamp_field})' if timestamp_field else ''} (and its
   format, if it is not ISO 8601 or epoch milliseconds), any stream meta to add (describe_stream lists a stream's
   attributes), any fields to leave out (secrets, tokens), and an example Elasticsearch index template from a
   sibling index, for its settings (and component templates, if it has any). If they don't know the time field, read one record
   (read_stream), no more.
3. draft_index_mapping backend=elasticsearch discovery={{timestamp_field, timestamp_format?, meta?, drop?}}: its XSLT
   copies every record as it is, adding StreamId, EventId (the record number) and @timestamp, and parses a string
   holding a JSON object into a sibling <field>_json. save_xslt index_plan=plan with no code;
   create_indexing_pipeline from the discovery template; step_sample on the raw streams: the documents show what
   the source holds.
4. propose_index_template with the plan, the raw streams and the user's example index template, and its component
   templates if it is composed of any (as GET _index_template/<name> and GET _component_template/<name> return
   them): a permissive template
   (dynamic mapping, strings as keywords, a total-fields limit, malformed values ignored) with the example's
   settings and components. Without an example it is not offered to agree: ask for one first. The user confirms it, or corrects it (check_index_template). Give them its dev_tools
   for the cluster admin; once they say it is committed, create_processor_filter on the raw streams (its approval
   asks them to confirm that) starts indexing. wait_for_processing expect_events=false; create_index_doc for the
   index; verify_index; write_documentation with stream_ids = the raw streams; promote.

{_RULES}{f'''

Sample:
{sample}''' if sample else ''}"""

    @mcp.prompt(description="Evaluate and document an existing events pipeline.")
    def evaluate_events_pipeline(pipeline: str, sample_size: int = 50, source_docs: str = '') -> str:
        return f"""Evaluate the Stroom events pipeline "{pipeline}" and write a report. Change nothing except the report.

1. Describe the pipeline: find it (find_documents), then describe_document for its template chain,
   elements, what it overrides or removes, and its reference data. describe_document its XSLT and text converter.
   processing_status shows which feeds its processor filters cover.
2. Sample the data: find_streams for recent Raw Events on each of those feeds, and the Events and Error
   streams the pipeline produced from them (describe_stream). Step up to {sample_size} raw records
   with step_sample, and look at one or two records in detail with step_pipeline.
3. Map the translation: describe_document on its XSLT. Compare input_fields with the fields in the
   raw data (read_stream, profile_sample) to find inputs that are never used.
4. Inventory the events: summarise_streams (kind=events) on its Events streams.
5. Measure conformance: check_events on sample Events records;
   summarise_streams (kind=errors) on recent raw streams it processed. Give rates, e.g. "97% valid".
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
   Without an event id, use summarise_streams (kind=events) on the Events stream to find the event type the user means,
   then step_pipeline on raw records until you find an example.
2. Plan the validation, and say it to the user in two or three lines: what output the record should give
   (from the user's words, the event-logging schema and any source docs), which field paths are wrong now, and
   which other records to check (recent raw streams on the same feed, find_streams; the same event type).
3. Confirm the issue: step_pipeline on the located record (with part), check_events
   on the output. If you cannot reproduce it, say what you found and ask the user; do not guess a fix.
4. Draft the fix in the pipeline's own XSLT or text converter and try it with step_pipeline draft_code. Then
   summarise_fix with the reported raw stream plus a few recent ones and expected_paths set to the fields the fix
   should change. Revise until ready is true.
5. Present the fix: the diff, the fields that change and how many records, and any template warning. Ask whether
   to apply it to the pipeline, or to give them the manual steps.
   - Apply: follow update_events_pipeline (ask new version or in place, confirm names, copy_pipeline, build_translation_xslt
     (uuid=...) from the changed mapping, or save_xslt (uuid=...) or save_text_converter with the draft, compare_outputs, write_documentation, promote_build).
   - Manual: give summarise_fix's manual_steps and diff.
   Either way, reprocessing production data is the user's to do.

{_RULES}"""
