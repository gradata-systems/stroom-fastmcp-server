"""MCP resources (reference guides, convention profiles) and prompts (packaged workflows)."""
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / 'knowledge' / 'guides'

_RULES = """Rules for every run:
- Start with get_instructions (docs= the folders, feeds or documents involved): standing instructions from AGENTS
  docs in Stroom. Follow them, the most specific last. The user's request takes precedence, and they never lift an
  approval.
- Build everything in one build (start_build, with the feeds when known: it returns the standing instructions
  too); nothing leaves the workspace until promote_build is approved. Before promoting, build_status shows what
  the build's pipelines still lack (a clean step of their current code, documentation): resolve it first.
- Confirm key details with the user when a tool returns needs_confirmation, and ask for approval when it returns
  needs_approval: show the summary, and only pass the id back once the user has agreed. needs_review (promote_build's
  layout, propose_index_template's template): show what it returns in the chat, then call again with reviewed=true,
  and the user approves it in a short form.
- Never assume a field convention, cluster, index name, template or feed name: propose one with where it came
  from, and let the user confirm or correct it. For a name a tool confirms (create_feed, create_pipeline,
  create_index_doc), propose it by calling the tool, not by asking in the chat first: the user confirms or corrects
  it there (a form their client shows, the name editable in it; otherwise the needs_confirmation reply you relay).
- Never weaken validation: do not change, remove or bypass a schema filter, or any check a template applies. When
  validation fails, the output is what to fix.
- Step every sample record (step_sample) before processing. Errors, when stepping or processing: first resolve
  those your own content causes (the XSLT, text converter, mapping or field plan), stepping again after each fix.
  Only what you cannot resolve there (an inherited template element, reference data, the source data itself) goes
  to the user, with what you tried and an example. If the user says an error is benign and can be ignored, record it
  with write_documentation accept_errors=[{element, example, reason in their words, matches: the kind it covers,
  with * for what varies, e.g. 'No HR record for user svc-*'}]; they confirm it, and it is then reported as benign
  with their reason and not raised again. Events written as Unknown are not such an error: they are agreed per rule
  in build_translation_xslt (allow_unknown). A generated XSLT is saved by build_translation_xslt (build, name) and
  stepped as saved; code written by hand is tried with draft_code before it is saved.
- Document what you build (write_documentation; stream_ids default to the streams its current code last stepped
  clean on) and use the user's source notes for field meanings.
- A sample's processor filter (create_processor_filter stream_ids=, an events or indexing pipeline) runs once the user
  approves it in a form: Elasticsearch, only once the agreed index template is committed, and an index the pipeline
  wrote to before dropped (the form says how). Then wait_for_processing with its filter_id. Any other filter (a whole
  feed, a CEF output, reference data) is created disabled, as promotion's are: give the user its pipeline_link, what
  enabling does and anything before_enabling says; ask them to enable it on the pipeline's Processors tab and to tell
  you when they have. Then wait_for_processing with its filter_id: never before.
- Reprocessing in a build, after a fix (reprocess_streams, at most 10 streams a call, one task at a time): for an
  events or indexing pipeline the user approves it in a form and it runs (an index is dropped first, so it starts
  fresh: give every Events stream it should hold); any other pipeline's (CEF output, reference data) is created
  disabled, for the user to enable. Then wait_for_processing with its filter_id. Reprocessing
  production data, and switching readers from one index version to the next, are the user's.
- A build on data already in Stroom (an existing feed, a production pipeline's new version or working copy) starts
  with start_build feeds=[the feed it works from], creates no feed and uploads nothing: sample files in the workspace
  aren't used. Its records are stepped where they are (step_sample with their stream ids, step_records), and
  processed where they are (create_processor_filter with their stream ids): the server writes their Events to a test
  feed it makes in the build, never into the production feed. Only sample files the user chooses instead of the
  feed's data go to a test feed of their own (create_feed user_samples=[the files they named]). Test feeds stay in
  the workspace, and promotion's filters name the feed the build works from. Promotion creates each pipeline's processor filter, disabled, with
  no minimum creation time: give the user the pipeline link to review and enable it, and say that enabled it
  processes the feed's existing data too, the build's streams included. An indexing pipeline's says before_enabling:
  its index is dropped (or emptied) first, so it starts fresh, and, with its events pipeline promoted too, it is
  enabled only once that pipeline's filter has processed the build's streams again; ask the user to do it in that
  order. A filter marked existing is one the pipeline already had, enabled: no second one is made.
- Elasticsearch indexing in the workspace runs in batches of 10 (create_processor_filter and reprocess_streams set
  it), so a document Elasticsearch rejects is reported whole, with its reason; wait_for_processing restores the
  template's batch size once indexing completes without errors.
- Elasticsearch indexing runs only through the Stroom indexing pipeline, with an index template the user agreed
  (built by propose_index_template from their example, or their correction), and only once the cluster admin has
  committed it to the cluster: the user enables the filter then, or confirms it when approving a reprocessing."""


# Sent to every client at connection (serverInfo.instructions), so the rules hold whichever client runs the model
# and whether or not it uses the prompts.
SERVER_INSTRUCTIONS = f"""Stroom MCP server: builds Stroom content (feeds, translation and indexing pipelines) from raw data, through tools
that enforce the rules; the prompts (onboard_data_source, onboard_existing_feed, update_events_pipeline, ...) are the
workflows, and stroom://guide/* the reference.

Order of work for a new source, always: start_onboarding (creates the build; given the sample streams, profiles
them and returns the plan), then stage 1, the events pipeline (feed, samples uploaded, template, translation XSLT saved with its
mapping, step every record, process, validate the Events, document), then stage 2, indexing, which reads the Events
streams stage 1 produced, then promotion. One tool call is never the whole job: every write tool's result carries
`next`, the plan's next step, and `done: false` until promotion; build_status shows every step's state. Keep going
until `next` says promote. A task list you keep follows `next.progress`: the steps done, and the one now. A result's
`user_actions` are the user's steps (enabling a filter, an Elasticsearch request for the cluster admin, a document to
open), which a client may show as buttons: still say what each is for and wait for the user, and never take one as
done; the next call reads Stroom to see. Never start with an indexing pipeline for raw data; create_indexing_pipeline refuses
until the build has an events pipeline or is given existing Events streams; create_processor_filter refuses a
pipeline with no clean step recorded; create_pipeline refuses another source's XSLT and a template whose parser
cannot read the sample. Templates are inherited (create_pipeline), never copied: copy_pipeline refuses one, and is for a
new version or working copy of a source's own pipeline.

Tools: a tool this server names (in `next`, a hint or a refusal) may not be in your tool list yet: some clients
hide part of a server's tools behind tools that enable a group of them (VS Code: activate_*). Call the one in your
tool list whose description names it, then the named tool. Only names your list shows: the client makes them up per
session, and a guessed one only fails. If none names it, ask the user to enable it. Never work around a hidden tool
with others. A result your client saved to a file ("Large tool result ... written to file", VS Code: over 8 KB): read
it with your client's file-reading tool (read_file), which needs no approval, never with a terminal command
(Get-Content, cat), which asks the user every time. Never write, edit or re-encode text for this server in a terminal
(seen: a doc body round-tripped through PowerShell came back mangled, and fixing it stalled the terminal): pass it to
the tool as it is; the server repairs text mangled by a Windows code page itself.

Questions for the user: when a result, hint or note leaves decisions to the user (the event-logging version, an
action element, System Name and Environment, which records to keep, whether to reindex) and your client gives you a
tool for asking the user questions (Stroom AI: ask_user), ask them with it, all in one call, rather than listing
questions in your answer: one question per decision, its options the ones the result lists or that fit, the one you
recommend first and marked as recommended, and the user's own answer allowed unless only the options make sense.
Without such a tool, ask in your answer: one numbered question per decision, each with what you recommend. Either
way, where a result gives the options, their order or labels, or says to recommend none, keep to it. Ask only what
you can't decide from the result, the standing instructions or what the user has said, and not what a form from this
server will ask; then carry on with their answers. A request to paste something (an index template, a sample) is
not such a question: ask for it in your answer and end your turn.

Existing feed: when the user asks to onboard data a feed already holds, work from that feed (onboard_existing_feed):
start_build feeds=[it], then survey_feed. No feed is created and nothing uploaded, whatever sample files the
workspace holds: its streams are stepped and processed where they are (create_processor_filter with their stream
ids), the server writing their Events to a test feed it makes in the build, and promotion gives the pipeline its
filter on the feed.

Samples (a new source): ask for every sample file the user has. Files on their disk: start_onboarding files=[their paths], then
create_feed, upload_sample files=[the same paths], and start_onboarding again with the build and stream_ids, which
profiles them; never read a file's text into the conversation to send it. Only pasted samples go as text
(start_onboarding samples=), about 10 records each: you write that text out token by token. start_onboarding profiles
them itself: don't call profile_sample as well. It reports what differs between files. Each file is its own stream:
step them all, and survey_feed with those stream_ids shows every kind of event the sample holds; map variants with
any_of. To fix a translation mapping, build_translation_xslt changes= only the rules or entries that change (with the
uuid it saved, or the same build and name), not the whole mapping again.

Whole feed: a sample can miss rare kinds of event. Once a feed has been processed (or indexed) whole, review_coverage
finds the records no rule matched and the events kept as Unknown, with exactly which raw streams hold them; tell the
user, add rules, and only those streams need processing again. A change to an events pipeline reaches the pipelines
that read its Events (indexing, CEF): review_coverage reviews them too, and they and their documentation are changed
with it. Each save records its change (change=) for the XSLT's version history and the doc's version control, as one
entry when the build is promoted.

Questions about the data (what a feed holds, what a field means, how to search it): describe_feed, read only,
from the generated documentation and the indexes' fields. Answer in plain words and give the documentation links.

Parsing: profile_sample names the parser. JSON (an array, or one object per line) is parsed by the Event Data
(JSON) template's JSONParser element with no text converter; a Data Splitter is for text (CSV, syslog, key=value).
XML fragments (several root elements, e.g. one <Event> per line, no root) take an XMLFragmentParser with an
XML_FRAGMENT wrapper converter: a template with one, else create_pipeline from a template whose parser is the XMLParser, with
replace_parser='XMLFragmentParser'; fragments without a namespace take the wrapper root's (records:2, or event-logging:3
for an <Events> wrapper): build_data_splitter gives the environment's own wrapper and saves it. The XSLT reads
the parser's output in its namespace (records:2 for a Data Splitter; http://www.w3.org/2013/XSL/json for JSON, root
/map for JSON lines, /array for an array), so set xpath-default-namespace to it.

Text formats: write the Data Splitter with build_data_splitter from a spec (delimited, regex, key=value, syslog with a
parsed body), which runs it on the sample and shows the records and field names; never by hand first.

Translation: start from draft_translation_mapping (the user first chooses the event-logging version it writes, from
those this Stroom holds, the newest recommended, 4.x as much as 3.x (namespace event-logging:3 is every version's):
a form, or needs_guidance for you to ask; then a valid mapping drafted
from the sample: decide the action element per kind of event, the system name and environment; describe_document
type=XMLSchema element= says what an element takes: EventDetail for the action elements, EventDetail/<Action> for its
children and values), then build_translation_xslt with the edited mapping and the draft's schema_version, the sample
(and the splitter spec) so fields no record has and time formats the values do not fit are caught before stepping;
never write XSLT by hand, and never send the field inventory as the mapping. A field that no element of the schema
means (a rule id, a byte count, a vendor code) is carried as Data of the action element (path
EventDetail/<Action>/Data with data_name): never invent an element for it, and never leave the event Unknown because
of it. Sources:
field, any_of (first of several names), value, xpath, lookup (reference data); modifiers: transform (lower, upper,
trim, strip_domain, domain, digits), dictionary, map, default, time_format. Values the record does not carry come
from reference data (find_reference_data; build_reference_xslt and a reference-data pipeline for new tables; the
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
    return (f"\n\nThe user supplied this source documentation. Keep it in Stroom with record_source_notes: the documents "
            f"themselves (documents=[{{title, text}}], verbatim) and, condensed from them, the field dictionary (each "
            f"field's meaning, its codes as values, the event-logging path it belongs in) and the event catalogue (each "
            f"event with the field and value that show it in a record, a value of its own for each event, its action "
            f"element (e.g. Authenticate; for a connection allowed or denied, Network/Permit or Network/Deny), TypeId, "
            f"Action and outcome). Then draft_translation_mapping with build= drafts from the notes, and "
            f"build_translation_xslt with build= checks the mapping against the catalogue: resolve what it reports, or "
            f"tell the user where the documentation and the data disagree:\n{source_docs}") if source_docs else ''


def _samples(samples: str) -> str:
    return f"\n\nSample:\n{samples}" if samples else ''


def _indexing_steps(first: int, events: str, source: str) -> str:
    """Stage 2, the same wherever the Events come from: numbered from `first`."""
    n = iter(range(first, first + 20))
    return f"""Two kinds of template, not to be confused: a Stroom pipeline template is a pipeline the indexing pipeline
inherits from (find_pipeline_templates, describe_template); an Elasticsearch index template defines the destination
index's mappings and settings on the cluster (propose_index_template, check_index_template).

{next(n)}. Backend and field naming: find_pipeline_templates stage=indexing gives the backend (Lucene or
   Elasticsearch) and the Stroom pipeline template. Elasticsearch: find_elastic_clusters, then get_field_conventions
   backend=elasticsearch, without asking first: it asks the user in a form, and its reply gives the next call. Only
   where it returns the choices instead (needs_guidance: a client without forms), offer them exactly as labelled, in
   order, and recommend none of them:
   - From an index template: the user pastes GET _index_template/<name> (or an index's GET <index>/_mapping) and, only
     if it lists any in composed_of, those component templates (GET _component_template/<name>). Ask them to paste it
     and end your turn (a choice form can't carry it); draft nothing until it arrives.
   - Follow an existing index in Stroom (its existing_indexes): draft_index_mapping like_index= reads its field names
     and types through Stroom; nothing to paste.
   - One per convention profile, only when they have no example: draft_index_mapping convention=...
     without_example=true, which the user confirms.
   With an example or an index there is no convention question: it names the fields. Lucene: get_field_conventions
   (it asks the user which convention to follow, in a form where the client has one).
{next(n)}. Propose, in one message, the backend, cluster or volume group, convention, Stroom pipeline template and index
   name (following the environment's versioned naming).
{next(n)}. The field plan: describe_template on the indexing template; if its shared_xslt shows sibling indexing XSLTs calling
   shared templates (a guid field, say), pass them to draft_index_mapping as shared. draft_index_mapping
   events_stream_ids={events} (Elasticsearch: with the user's example_template and any component templates, or
   like_index, so field names follow theirs; show its from_example notes); create_index_doc with plan= (the user
   confirms the name in its form); save_xslt index_plan=plan, with no code (it is generated from the plan);
   create_indexing_pipeline with that XSLT and the index (events_stream_ids={events} when the build has no events
   pipeline); step_sample over {events} until clean.
{next(n)}. Elasticsearch, the index template: propose_index_template with the user's example (exactly as they pasted it, the
   text given to draft_index_mapping) and any component templates builds the template for the new index, following
   their naming, field type and structure conventions (its from_example notes say what came from where). When it
   fits the documents it comes back as needs_review: show the user its dev_tools in the chat as a json code block,
   then call again with reviewed=true, and they confirm it in a short form. If they correct it instead,
   check_index_template with their version: when it fits, they confirm it there; when it doesn't, show the pipeline
   changes it needs and ask whether to make them (update the index plan, save_xslt, step again) or change the
   template. Without an example, propose_index_template builds nothing: ask the user for one; only if they say they
   have none, without_example=true. A template put on the cluster without being agreed here doesn't count.
{next(n)}. Elasticsearch, committing it: give the user the agreed template's dev_tools request for the cluster admin to
   commit to the cluster, and wait until they say it is committed: indexing before that creates the index without it.
{next(n)}. Processing: create_processor_filter stream_ids={events} source_pipeline_uuid={source} (Events from a pipeline this
   server didn't build: the user confirms it in a form). The user approves it in a form (Elasticsearch: once the
   template is committed, and the index dropped if this pipeline wrote to it before) and it runs; then
   wait_for_processing expect_events=false. If a call is refused, do what its message
   says: never go on to another stage or to promotion instead.
{next(n)}. Verification: suggest the dashboard's columns (the time field and key fields: user, host, address, event type,
   outcome; never StreamId or EventId) and confirm them with the user. verify_index with those fields and
   pipeline_uuid = the indexing pipeline (each hit traced back to its event), and searches as people will make them,
   from stepped values: an exact match, a value in another case (keywords on Elasticsearch: expected 0), IN, a
   wildcard, a numeric or IP range.
{next(n)}. write_documentation for the indexing pipeline (stream_ids = {events})."""


_PROMOTE = ("promote_build (it returns the layout first: show it in the chat; once the user confirms it, call again\n"
            "   with reviewed=true, and they approve it in a short form).")

_HANDOVER = """Reprocessing production data is the user's: tell them which raw streams to reprocess (exactly those: from
find_events all_streams with the conditions that pick out the events, or review_coverage's affected streams), and for
Elasticsearch the delete request their cluster admin runs just before, so the corrected events aren't indexed twice.
A CEF pipeline sends the reprocessed streams' events again, whole. New data takes the change once the promoted
pipeline processes it."""


def _change_through_a_copy(first: int, what: str) -> str:
    """Changing a production events pipeline: a copy in a build, the change saved and proven on it, the pipelines
    that read its Events reviewed, then promotion. Numbered from `first`."""
    n = iter(range(first, first + 10))
    return f"""{next(n)}. The build: start_build feeds=[the pipeline's feed] (find it from the pipeline's processor filters:
   processing_status). The build works from the feed: it creates no feed and uploads nothing.
{next(n)}. Ask the user whether this is a new version (e.g. V1.2 to V1.3) or an in-place change, and confirm the names. New
   version: copy_pipeline with rename (e.g. {{'V1.2': 'V1.3'}}). In place: copy_pipeline working_copy=true.
{next(n)}. Save {what} on the copy: build_translation_xslt uuid=<the copy's XSLT> changes= only the rules or entries that
   change, change=<what and why>. Code a mapping can't express: save_xslt uuid=<the copy's XSLT> change=..., then
   rebuild_mapping uuid=<it> reads the mapping back, so the documentation can be generated. A text converter:
   save_text_converter uuid=<the copy's converter>.
{next(n)}. Prove it: step_sample on the copy over the test records (step_records for located records) until clean; then
   summarise_fix pipeline_uuid=<the original> fixed_pipeline_uuid=<the copy>, with stream_ids= a few recent raw
   streams and events= the indexed examples where there are any, and expected_paths= the fields the change is to:
   only those may change, and no new errors. Revise until ready is true.
{next(n)}. The pipelines that read its Events (indexing, CEF output): review_coverage pipeline_uuid=<the original>
   changed_pipeline_uuid=<the copy> raw_stream_ids=<the raw streams it was proven on> lists them and what each lacks.
   Change each in the same build (copy_pipeline working_copy=true; an index: draft_index_mapping extra_fields and
   save_xslt index_plan; CEF: draft_cef_mapping uuid=<its XSLT> overrides), step it clean, and document it.
   Elasticsearch: a template change goes through check_index_template (the user confirms; the cluster admin commits).
{next(n)}. write_documentation for the copy, noting the change (change=...): each XSLT and doc gains one version entry for
   the build. Then {_PROMOTE} In place, promotion writes the copy back over the production pipeline after a backup."""


def register(mcp: FastMCP, conventions_dir: Path = ROOT / 'conventions') -> None:
    @mcp.resource('stroom://guide/{name}', mime_type='text/markdown',
                  description="Working guides: event-logging, xslt, data-splitter, json-input, reference-data, indexing, "
                              "cef, documentation, agent-instructions.")
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

    # Each prompt is complete in itself: the agent only has the text of the prompt the user started, so none refers to
    # another (seen in the review of the prompts: "run stage 2 of onboard_data_source" named text it never had).

    @mcp.prompt(description="Onboard a new data source: sample to events, then indexing, then promotion.")
    def onboard_data_source(source_name: str, samples: str = '', vendor: str = '', source_docs: str = '') -> str:
        return f"""Onboard "{source_name}"{f' from {vendor}' if vendor else ''} into Stroom.

A feed that already holds this data: follow onboard_existing_feed's steps instead (start_build feeds=[the feed],
survey_feed), with no feed created and nothing uploaded, even with sample files in the workspace.

Stage 1, events:
1. Samples. Ask whether there are more sample files than {'the one below' if samples else 'they have given'} (other
   appliances, versions or days) and get them all. Files on the user's disk: start_onboarding files=[their paths]
   creates the build; create_feed with the feed name you propose (following sibling feeds' naming: the user confirms
   or corrects it in its form); upload_sample files=[the same paths], so each file is its own stream; then
   start_onboarding again with the build and stream_ids profiles them (fields and timestamp shapes that differ
   between files, the parser and template, whether a text converter is needed) and returns the plan. Never read a
   file into the conversation to send its text. Pasted samples only: start_onboarding samples= by name, about 10
   records each, then create_feed. Follow `next` in each result until it says promote_build; build_status shows what
   remains. From then on every tool takes the sample streams (stream_ids) and reads them itself.
2. The kinds of event: survey_feed with those stream_ids and the build lists the kinds of event the sample holds and
   where, so every kind gets a rule and step_records can check each. Values the records don't carry (a user's
   department, a host's site): find_reference_data for maps the environment loads, or build the reference data
   (reference-data guide).
3. Text formats need a Data Splitter (DSParser.textConverter): build_data_splitter with the sample streams infers the
   spec and runs it until every line parses; save_text_converter saves it (or build_data_splitter save_as=). XML
   fragments need the wrapper (xmlFragmentParser.textConverter). JSON and single-document XML need none: the
   JSONParser or XMLParser reads them.
4. The template: find_pipeline_templates stage=translation; describe_template on the best candidate. Its shared_xslt
   lists the shared XSLTs (xsl:import, found by name) its other pipelines' XSLTs use: each named template they call,
   where (at, e.g. EventSource/Device or Meta), what it writes and reads (stroom:meta) and the parameters they pass.
   Use the same ones: the mapping's shared entries (href, template, at, with_params), and map nothing below those
   elements, as the shared template writes them (twice fails validation). It lists their xsl:functions too
   (namespace, parameters, calls): call them in xpaths, with a functions entry (href, prefix, namespace) per shared
   XSLT. find_documents (content=...) for other XSLTs to reuse. An XSLT style in the standing instructions, or one the
   user asks for, goes in the mapping's style (layout, naming, variables, how Data values and Names are written).
5. The mapping and XSLT: draft_translation_mapping with the sample streams gives a mapping to edit (its notes say what
   to decide: the action element per kind of event, System Name, Environment, a time zone). Then
   build_translation_xslt (feeds=[the feed], stream_ids=the sample streams, splitter=the spec) with it: which input
   field or constant goes to which event-logging path, one rule per kind of event, any_of where files name a field
   differently, extract for text fields holding several values, lookup or dictionary for values from reference data.
   Give it build and name: it saves the XSLT with the mapping (kept with it, so the documentation is generated from
   it) and returns the document, not the code. Fix reported problems with changes= only the rules or entries that
   change (uuid= the saved XSLT, or the same build and name), not the whole mapping again; hand-edit only what a
   mapping can't express. To regenerate a saved XSLT as the generator now writes it, give uuid= alone: never send its
   kept mapping back. An XSLT changed by hand, or whose kept mapping was lost: rebuild_mapping reads the mapping back
   from it, proven on the sample.
6. The pipeline: create_pipeline from that template, with the set_properties build_translation_xslt returned (JSON)
   and references for any lookup maps (update_pipeline sets one left unset). step_sample over every sample stream
   until the verdict is clean, fixing the mapping and saving again (uuid=) in between; step_pipeline on single
   records to debug.
7. Processing: create_processor_filter on all the sample stream ids. The user approves it in a form and it runs; then
   wait_for_processing (the gate: one Events stream per raw stream, from code that stepped clean). After changing the XSLT, step again and
   reprocess_streams (the user approves; it runs). check_events stream_ids=<the Events streams> (the server reads
   them).
8. write_documentation for the events pipeline (stream_ids = the sample streams): its Field mapping section is
   generated from the kept mapping.

Stage 2, indexing the Events stage 1 produced:
{_indexing_steps(9, '<the Events streams of step 7>', '<the events pipeline from stage 1>')}

Finish: {_PROMOTE} The documents go to the folders sibling sources use.

{_RULES}{_docs(source_docs)}{_samples(samples)}"""

    @mcp.prompt(description="Fix or extend an existing events pipeline (new version or in place).")
    def update_events_pipeline(pipeline: str, issue: str = '', samples: str = '', source_docs: str = '') -> str:
        return f"""Update the events pipeline "{pipeline}".{f' Reported issue: {issue}' if issue else ''}

1. Find it (find_documents) and describe_document the pipeline and its XSLT.
2. Test records: ask the user which to use, and keep to their answer. Production records (recent streams on the
   feed, or a reported issue's examples: find_streams, read_stream, step_pipeline) are stepped where they are
   (step_sample with their stream ids, step_records): no feed is created and nothing uploaded. Only new sample files
   the user chooses go to a test feed in the build (create_feed user_samples=[the files they named], upload_sample),
   never the production feed: files in the workspace aren't their choice until they say so. A test feed
   stays in the workspace at promotion, and its samples are processed and checked there (create_processor_filter,
   which the user enables; wait_for_processing; check_events).
3. Draft the change on the test records with step_pipeline draft_code and compare_outputs (draft_code): only the
   targeted fields may change, and step_sample must stay clean.
{_change_through_a_copy(4, 'the change')}

{_HANDOVER}

{_RULES}{_docs(source_docs)}{_samples(samples)}"""

    @mcp.prompt(description="Create the next version of an indexing pipeline with field changes.")
    def update_indexing_pipeline(pipeline: str, changes: str) -> str:
        return f"""Create the next version of the indexing pipeline "{pipeline}" with these changes: {changes}

1. describe_document to find its XSLT, index doc and index name (and, for Elasticsearch, the index template agreed
   for it, kept in the pipeline's description). Work out the next version from the naming convention (e.g. -v1 to
   -v2) and confirm it and the cluster or volume group with the user. start_build feeds=[the Events feed it reads]:
   the build works from the Events already in Stroom, so it creates no feed and uploads nothing. find_streams
   feed=<that feed> stream_type=Events for recent Events streams.
2. copy_pipeline as a new version (not a working copy), with rename (e.g. {{'-v1': '-v2'}}) and, for Elasticsearch,
   set_properties for v2's indexName. draft_index_mapping events_stream_ids=<the recent Events streams> with the
   requested fields as extra_fields (Elasticsearch: with v1's agreed template as example_template, so v2 follows it).
   Lucene: create_index_doc for v2 with plan=, then update_pipeline to point the copy at it. save_xslt
   index_plan=plan uuid=<the copied XSLT>.
3. step_sample v2 on the recent Events streams; compare_outputs v1 against v2 (other_pipeline_uuid): only the
   requested fields may differ.
4. Elasticsearch: propose_index_template for v2 with v1's agreed template as the example (v2's pattern, the added
   fields, everything else as v1's). It comes back as needs_review: show its dev_tools in the chat, call again with
   reviewed=true, and the user confirms it (or corrects it: check_index_template). Give them its dev_tools for the
   cluster admin to commit, and wait until they say it is.
5. create_processor_filter on v2 with stream_ids=<the recent Events streams> and source_pipeline_uuid=<the events
   pipeline that wrote them> (the user confirms one this server didn't build). The user approves it in a form
   (Elasticsearch: once the template is committed) and it runs; then wait_for_processing expect_events=false. Elasticsearch: create_index_doc for v2 (verify_index searches through
   it). verify_index with pipeline_uuid = v2 and searches on the new fields.
6. write_documentation for v2, saying what changed from v1. {_PROMOTE} v2 goes in a version subfolder beside v1, with its
   index doc, documentation and dashboard. Promotion gives v2 a filter, disabled, with no minimum creation time:
   enabled, it indexes the feed's existing Events too (a backfill), those indexed in the build included, so its
   index is dropped first and starts fresh (before_enabling says how). Say so. v1
   stays running; enabling v2, switching readers to it, and retiring v1 are the user's.

{_RULES}"""

    @mcp.prompt(description="Find kinds of event the sample missed, once the whole feed is processed, and cover them.")
    def check_feed_coverage(pipeline: str, source_docs: str = '') -> str:
        return f"""Check the events pipeline "{pipeline}" covers its whole feed, now that the feed has been processed (and indexed).

1. Find it (find_documents), and start_build feeds=[its feed] (processing_status names the feed its filters read).
   review_coverage pipeline_uuid=<it>: the records no rule matched across the whole feed (from its Error streams) and
   the events kept as Unknown, grouped by kind with examples, and exactly which raw streams hold them. Tell the user
   what it found (its tell_user): untranslated events, or none.
2. For each kind found: step_records on an example, and agree with the user what it is.
3. A pipeline in production is changed through a copy: copy_pipeline working_copy=true into the build first. Add a
   rule for each kind on the copy: build_translation_xslt uuid=<the copy's XSLT> changes={{events: [...]}}
   change=<what was found and added>. (A pipeline still in a build: change it there.)
4. step_sample over the sample streams, and step_records over the affected records (review_coverage's examples), on
   the copy, until clean.
5. The pipelines that follow it: review_coverage pipeline_uuid=<the original> changed_pipeline_uuid=<the copy>
   raw_stream_ids=<the affected raw streams> lists each indexing or CEF pipeline and what it lacks. Change each in
   the same build as it says (a working copy; draft_index_mapping extra_fields and save_xslt index_plan;
   draft_cef_mapping uuid=... overrides), step it clean, and document it. Elasticsearch: a template change goes
   through check_index_template, which the user confirms and the cluster admin commits.
6. write_documentation for the copy, with stream_ids = the sample streams plus some affected ones and change = what
   was found and added (one version row when the build is promoted). Then {_PROMOTE}
7. Processing the affected raw streams again: in a build, reprocess_streams (at most 10 a call; the user approves); in
   production it is the user's, exactly the streams review_coverage named, after the cluster admin's delete for
   Elasticsearch (review_coverage gives the request). Warn them that a CEF pipeline sends the reprocessed streams'
   events again, whole.

{_RULES}{_docs(source_docs)}"""

    @mcp.prompt(description="Send a source's Events to ArcSight as CEF (flattened text, usually through Kafka).")
    def forward_events_as_cef(feed: str, topic: str = '') -> str:
        return f"""Send the events in feed "{feed}" to ArcSight as CEF: one flattened CEF line per Event{f', on Kafka topic {topic}' if topic else ''}.

1. start_build feeds=[{feed}] (the build works from its Events: no feed to create, nothing to upload). Its standing
   instructions (get_instructions docs=[{feed}]) may give CEF mappings, the topic, whether keys outside the CEF
   dictionary are allowed, and the pipeline template to use; they take precedence. find_streams feed={feed}
   stream_type=Events for recent Events streams.
2. draft_cef_mapping stream_ids=<the Events streams> feeds=[{feed}]: it asks the user first whether keys outside
   ArcSight's CEF dictionary are allowed (unless the instructions say), then drafts the header, the fields every
   event shares and, per kind of event, the rest. Show the user its tables (documentation) and its not_sent list;
   their changes go back as overrides. {'' if topic else 'Ask them for the topic, and '}ask for the KafkaConfig (its
   kafka_configs lists them).
3. Save it: draft_cef_mapping with the same arguments plus build and name.
4. The pipeline, from the template the instructions name, else a forwarding template or the one existing CEF
   pipelines inherit from (draft_cef_mapping's templates), else create_pipeline standalone='kafka': set its XSLT and
   standardKafkaProducer.kafkaConfig (update_pipeline sets one left unset).
5. step_sample over the Events streams (stepping sends nothing); draft_cef_mapping pipeline_uuid=<it>
   stream_ids=... reviews what it writes; write_documentation (stream_ids = the Events streams).
6. {_PROMOTE} Promotion gives the pipeline a filter on the feed's Events, disabled: enabled, it sends the feed's
   Events to Kafka, the existing ones too. When to enable it is the user's.

Guide: stroom://guide/cef

{_RULES}"""

    @mcp.prompt(description="Review, or change, an existing CEF output pipeline.")
    def review_cef_pipeline(pipeline: str, change: str = '') -> str:
        return f"""Review the CEF pipeline "{pipeline}"{f' and change it: {change}' if change else ''}.

1. Find it (find_documents) and the Events streams it reads (find_streams on its input feed, stream_type=Events).
2. draft_cef_mapping pipeline_uuid=<it> stream_ids=<a few Events streams>: its lines checked against ArcSight's CEF
   dictionary (header, severity, keys outside it, unlabelled custom slots, types and lengths), what each Event sends
   and what it leaves out, and its plan (or the mapping its lines imply). Report the findings to the user.
3. To change it: start_build feeds=[its Events feed], then copy_pipeline working_copy=true. An XSLT that keeps its CEF
   plan: draft_cef_mapping uuid=<the copy's XSLT> with the change as overrides. One written by hand (no plan):
   draft_cef_mapping pipeline_uuid=<the copy> stream_ids=... with the change as overrides, plus build and name, saves
   a new XSLT from the mapping its lines imply; update_pipeline sets the copy's XSLT to it.
4. step_sample on the copy (sends nothing), review it again (draft_cef_mapping pipeline_uuid=<the copy>), and
   write_documentation noting the change. Then {_PROMOTE} It writes the copy back over the pipeline after a backup.

Guide: stroom://guide/cef

{_RULES}"""

    @mcp.prompt(description="Index an existing Events feed (stage 2 only).")
    def index_event_data(feed: str, index: str = '') -> str:
        return f"""Index the events in feed "{feed}"{f' into {index}' if index else ''}.

start_build feeds=[{feed}]: the build works from its Events, so there is no feed to create and nothing to upload.
find_streams feed={feed} stream_type=Events for recent Events streams, and processing_status on the feed for the
events pipeline that writes them.

{_indexing_steps(1, '<the recent Events streams>', '<the events pipeline that writes them>')}

Finish: {_PROMOTE}

{_RULES}"""

    @mcp.prompt(description="Index raw structured data directly into a discovery index.")
    def create_discovery_index(feed: str = '', samples: str = '', timestamp_field: str = '') -> str:
        return f"""Create a discovery index for {'feed ' + feed if feed else 'the sample below'}: raw data (JSON, delimited
text such as CSV, or XML) indexed as it is into Elasticsearch, for exploration, without an event-logging translation.

Elasticsearch maps the source's fields dynamically as documents arrive, so don't survey the data first: no
profile_sample, no reading through the streams. Only StreamId, EventId and @timestamp are mapped explicitly.

1. The source: {f'start_build feeds=[{feed}], and its Raw Events streams' if feed else 'the sample uploaded to a new feed in the build (start_build, create_feed, upload_sample)'};
   what it is (JSON, delimited text, XML) from a glance at one record. find_pipeline_templates stage=discovery for
   the Stroom pipeline template whose parser fits it (JSONParser, DSParser, XMLParser or XMLFragmentParser; for XML,
   an XMLParser template listed under stage=indexing serves too, as the two look alike); find_elastic_clusters.
   Delimited text needs a Data Splitter in the build first (build_data_splitter with the streams, which infers the
   header; save_text_converter): it names each column.
2. Ask the user, in one message: the cluster, the index name (e.g. stroom-discovery-<source>-v1), the Stroom pipeline
   template, the field holding the event time{f' ({timestamp_field})' if timestamp_field else ''} (and its format, if
   it is not ISO 8601 or epoch milliseconds), any stream meta to add (describe_stream lists a stream's attributes), any
   fields to leave out (secrets, tokens), and an example Elasticsearch index template from a sibling index, for its
   settings (and its component templates, if it has any). If they don't know the time field, read one record
   (read_stream), no more.
3. draft_index_mapping backend=elasticsearch discovery={{input (json, delimited, xml), timestamp_field, record (xml:
   the record element), timestamp_format?, meta?, drop?}} events_stream_ids=the raw streams. When the records hold
   times in several fields, the user picks which is @timestamp (a form, or ask them with the candidates it lists);
   every record having its time makes the index a data stream. Its XSLT copies every record as it is (a JSON object;
   each column, named by the header; an XML element as nested objects, repeated elements as arrays, attributes as
   fields), adding StreamId, EventId (the record number) and @timestamp, and parses a value holding a JSON object
   into a sibling <field>_json. save_xslt index_plan=plan with no code; create_indexing_pipeline from the discovery
   template; step_sample on the raw streams: the documents show what the source holds.
4. propose_index_template with the plan, the raw streams and the user's example index template, and its component
   templates if it is composed of any (as GET _index_template/<name> and GET _component_template/<name> return them):
   a permissive template (dynamic mapping, strings as keywords, a total-fields limit, malformed values ignored) with
   the example's settings and components. Without an example it is not offered to agree: ask for one first. It comes
   back as needs_review: show its dev_tools, call again with reviewed=true, and the user confirms it (or corrects it:
   check_index_template). Give them its dev_tools for the cluster admin to commit.
5. create_processor_filter on the raw streams. The user approves it in a form, once the template is committed, and it
   runs. Then wait_for_processing expect_events=false; create_index_doc for the index; verify_index (pipeline_uuid = the discovery pipeline, so hits
   are traced to their records, and searches on a few of the fields the documents showed); write_documentation
   (stream_ids = the raw streams). Then {_PROMOTE}

{_RULES}{_samples(samples)}"""

    @mcp.prompt(description="Evaluate and document an existing events pipeline.")
    def evaluate_events_pipeline(pipeline: str, sample_size: int = 50, source_docs: str = '') -> str:
        return f"""Evaluate the Stroom events pipeline "{pipeline}": a health check. The question to answer first is whether
anything is wrong with it that is worth worrying about, schema compliance above all. Change nothing except the
report.

1. Describe the pipeline: find it (find_documents), then describe_document for its template chain, elements, what it
   overrides or removes, and its reference data. describe_document its XSLT and text converter. processing_status
   shows which feeds its processor filters cover.
2. Errors and schema compliance, first: find_streams for recent Raw Events on each of those feeds, and the Events and
   Error streams the pipeline produced from them (describe_stream). summarise_streams (kind=errors) on those raw
   streams: its error groups, triaged as blocking, review or benign. check_events stream_ids=<the Events streams>
   (the server reads them): the share that are valid against the schema and pass the quality rules. step_sample over
   up to {sample_size} raw records shows the errors the current code gives now, and step_pipeline one record in
   detail.
3. Say plainly whether there are errors worth worrying about: blocking ones (schema failures, fatal errors, records not
   translated) with how many records and events each affects and an example; review ones; benign ones only as a
   count. Compare records stepped with events stored: Stroom doesn't store an event that fails the schema, so the
   stored events can all be valid while records are lost, e.g. "6 records, 5 events: one record's event fails on
   EventSource/Device/IPAddress ('n/a') and is not stored".
4. Map the translation: describe_document on its XSLT. Compare input_fields with the fields in the raw data
   (read_stream, profile_sample) to find inputs that are never used.
5. Inventory the events: summarise_streams (kind=events) on its Events streams: types, TypeIds, actions, and how
   often each path is populated.
6. Suggest fixes, prioritised (blocking errors first), each with what it fixes, the share of records or events
   affected, and a draft change to the pipeline's own XSLT or text converter. When the user asks for one, prove it
   with summarise_fix (element, draft = the complete fixed code, expected_paths = the fields it should change):
   ready means it changes only those fields and adds no errors (errors the saved code has too are reported apart,
   as not the fix's doing). Applying it is a change through a copy of the pipeline in a build (copy_pipeline, the
   change saved and stepped on the copy, promote_build), as the user decides.

Report sections: Purpose and data; Processing; Errors and schema conformance; Field mapping; Event types;
Suggestions. Return it in the chat. To keep it in Stroom: start_build, then write_documentation
pipeline_uuid=<the pipeline> evaluation=true, which keeps it as its own doc, "<pipeline> - Evaluation" (the
pipeline's documentation is left as it is); once the user approves, {_PROMOTE} It goes beside the pipeline.

{_RULES}{_docs(source_docs)}"""

    @mcp.prompt(description="Document an existing index (Elastic Index or Lucene Index doc).")
    def document_index(index: str, purpose: str = '') -> str:
        return f"""Document the existing index "{index}" in Stroom, as an indexing pipeline's documentation would be:
every field, what it holds and the values it has. Change nothing except the documentation.

1. Locate the index doc: find_documents with types=['ElasticIndex', 'Index'] (the name, or part of it). Show the
   user each match with its folder and confirm which one; when none or several match, ask.
2. Survey it through Stroom: describe_document on the index doc. Its survey has the fields Stroom has for it, the
   newest documents read through a dashboard that is not saved (how often each field is populated, sample values),
   the time range they cover, and the pipelines that feed it (with whether each keeps an index plan, which says
   where each field comes from in the events). describe_document a feeding pipeline for its source feeds and what it
   does. A field with no values in the survey: say so, rather than guessing what it holds.
3. Ask the user, and wait for the answer before drafting: what is the index for, what system or team produces its
   data, and who searches it and why? The survey shows what the index holds, not why it exists; don't infer its
   purpose when the user can say it.{' (They have said: ' + purpose + '; ask only what that leaves open.)' if purpose else ''}
4. Draft the documentation in a build (start_build), with write_documentation index_uuid=the index doc: the user
   confirms the index doc there. Write Purpose and data from the user's answer and what you have seen of the source,
   in a few paragraphs: what the source system is and what its data records (the feeds the survey's profile names,
   their descriptions, and the feeding and events pipelines: describe_document, and their own Documentation docs
   where they have one); what kinds of events or records the index holds (from the sample values, e.g. the event
   codes and actions); who and what it serves, and how it is searched (dashboards on the index doc: find_documents
   types=['Dashboard'] with the index's name). Lead with the purpose as the user gave it; say what you don't know
   rather than guess. The tool adds a "Data surveyed" summary under it (documents, time span, source feeds and
   pipelines) and generates Field mapping: each field with a description, type, source path when a plan records it,
   how often it is populated and sample values. Fields the feeding pipelines write that the index lacks are refused
   by name: tell the user, and ask whether to reindex or check again (document_missing_fields=true documents them as
   not in the index, if they choose that).
5. Give the user the doc's link (the reply's link) and the field table in the chat. Ask where it should live: beside
   the index doc (the default) or a folder they choose.
6. Once the user agrees, {_PROMOTE} The doc goes beside the index doc, or with destinations=
   {{'Documentation': '<their folder>'}}.

{_RULES}"""

    @mcp.prompt(description="Build an events pipeline for a feed that already holds data.")
    def onboard_existing_feed(feed: str, source_docs: str = '') -> str:
        return f"""Build an events pipeline for the existing feed "{feed}" from the data it already holds, by stepping only.

1. start_build feeds=[{feed}], then survey_feed feed={feed} build=<the build>: it samples streams spread over the
   feed's lifetime, reading only the head of each (big streams are fine), and groups records into kinds of event,
   with counts, examples and where each example is, and keeps them in the build's '{feed} - Survey' doc. Not every
   kind appears in every stream. The sample size is the user's: max_streams (streams a call, 10 by default; up to
   100 as they ask, fewer only when they give the number, which they confirm in a form), max_records_per_stream
   (1,000, up to 10,000) and max_chars_per_stream (250,000, up to 5,000,000), on every survey_feed call.
2. Draft the text converter if the format needs one (build_data_splitter with the surveyed streams) and the
   translation with build_translation_xslt (feeds=[{feed}], build and name), one rule per kind of event, then
   create_pipeline and step_records over the survey's locations until clean: the feed's own records are stepped
   where they are. Create no feed and upload nothing, whatever sample files the workspace holds.
3. survey_feed again with the same build: it carries on from the survey doc (streams read, kinds known). New kinds of
   event: add rules (build_translation_xslt uuid= changes=), and step_records over every location so far. Repeat
   until a survey's coverage says covered, or every stream has been read; until then, say plainly that the feed is
   not covered yet. Kinds that may not be worth translating (housekeeping, debug, noise): show them with their share
   and ask the user; never drop records on your own judgement. If the user agrees, set_shape_handling handling=drop
   with their reason (they confirm it), add a drop rule for those kinds to the mapping (drop=true, conditions that
   match them), and step_records: their locations expect no Event and count as clean when none is written.
4. Broad check: step_sample over three of the surveyed streams (newest, oldest, middle) with records_per_stream=150,
   to catch variants the examples didn't show; fix and step again until clean. Then tell the user which kinds of
   event the pipeline covers and their share of the data.
5. Process the surveyed streams where they are: create_processor_filter stream_ids=[them] (up to 10). Their Events
   go to a test feed the server makes in the build (output_feed), not into {feed}. The user approves it in a form and
   it runs; then wait_for_processing, then check_events on the Events.
6. write_documentation for the pipeline. Then {_PROMOTE} Promotion gives it a filter on {feed}, disabled: enabled, it
   processes the feed's existing data too; when is the user's. Once its Events exist, indexing them is a build of its
   own (start_build feeds=[{feed}], then the index).

{_RULES}{_docs(source_docs)}"""

    @mcp.prompt(description="Change how a kind of event is translated, from events found by searching an index.")
    def change_event_translation(index: str, events: str, change: str, source_docs: str = '') -> str:
        return f"""The user wants the events "{events}" in the index "{index}" translated differently: {change}

The events are the user's examples of a kind of event: the change is a rule on what the raw records hold, so it
applies to every event of that kind, not to these events alone.

1. Find them: find_documents types=['ElasticIndex', 'Index'] for the index doc, then find_events with the conditions
   the user's description gives (as they would set them on a dashboard). Show them the hits (newest first, each with
   its StreamId and EventId), the total and the streams holding them, and confirm these are the events they mean;
   narrow or widen the conditions with them until they are.
2. Trace a few back, from different streams if the hits span several: locate_event stream_id=<StreamId>
   event_id=<EventId> for each gives the raw record, the pipeline that produced it, its feed, and its translation.
   Read the records' input (step_pipeline) beside the stored events.
3. Generalise, and say it to the user before changing anything: describe_feed feed=<the feed locate_event gave>
   field=<each searched index field> traces it to its event-logging path and the source field it comes from;
   describe_document on the translation XSLT shows its kept mapping and the rule that writes these events. State the
   rule condition, on the raw fields, that picks out exactly this kind (every hit must meet it; events of other kinds
   must not), and the change, as one example's value before and after. Say how many events it reaches: find_events
   all_streams with the same conditions (N events in M Events streams). A field the change reads that the rule's
   records may lack takes a default.
{_change_through_a_copy(4, 'the change to the rule')}

{_HANDOVER}

{_RULES}{_docs(source_docs)}"""

    @mcp.prompt(description="Diagnose a reported problem in an events pipeline and propose a fix.")
    def fix_pipeline_issue(issue: str, stream_id: int | None = None, event_id: int | None = None,
                           source_docs: str = '') -> str:
        where = (f"event {event_id} of Events stream {stream_id}" if stream_id and event_id else
                 f"Events stream {stream_id}" if stream_id else "an event")
        return f"""The user reports a problem with {where}: {issue}

1. The event: every indexed event carries the Events stream's StreamId and its EventId, in the dashboard row where
   the user saw it (show the StreamId and EventId columns, or open the row in the text pane).{'' if stream_id and event_id else ''' Ask the user for
   both first. If they can only describe it, find_events searches the index for it as they would, and gives each
   hit's StreamId and EventId: confirm with them which hit they mean. Only failing both, summarise_streams
   (kind=events) on the Events stream finds the kind of event they mean, then step_pipeline on raw records for an
   example.'''}
   locate_event(stream_id=<the StreamId>, event_id=<the EventId>) gives the raw stream, part and record, the pipeline,
   its feed, and its XSLTs and text converters (some may be inherited from a template).
2. Plan the validation, and say it to the user in two or three lines: what output the record should give (from the
   user's words, the event-logging schema and any source docs), which field paths are wrong now, and which other
   records to check: other events of the same kind wherever they are (find_events with the conditions that pick them
   out, e.g. the TypeId and the wrong value: its hits are what the fix is proven on), and recent raw streams on the
   same feed (find_streams).
3. Confirm the issue: step_pipeline on the located record (with part), check_events on the output. If you can't
   reproduce it, say what you found and ask the user; don't guess a fix.
4. Draft the fix in the pipeline's own XSLT or text converter and try it with step_pipeline draft_code. Then
   summarise_fix with element, draft = the complete fixed code, events = the hits of the same kind (from several
   streams if it has them), stream_ids = a few recent raw streams, and expected_paths = the fields the fix should
   change: the hits must change as intended, and the recent streams only there. Revise until ready is true.
5. Present the fix: the diff, the fields that change and how many records, and any template warning. Ask whether to
   apply it to the pipeline, or to give them the manual steps (summarise_fix's manual_steps and diff). To apply it:
{_change_through_a_copy(6, 'the fix')}

{_HANDOVER}

{_RULES}{_docs(source_docs)}"""
