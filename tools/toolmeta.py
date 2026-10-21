"""What each tool is, for clients that decide from it whether to ask the user first and which tools to offer together
(asked for by the user, for Stroom's Ask Stroom AI and clients like it).

MCP annotations, as the spec has them:
- readOnlyHint: the tool changes nothing in Stroom.
- destructiveHint: it can change or start something outside the build workspace: processing (a filter that runs, or
  reprocesses), or production documents (promotion). A tool that writes only the build's own drafts and records is
  not destructive: the write guard refuses it anything else.
- openWorldHint: false for every tool: they reach this Stroom (and its Elasticsearch through it), nothing else.

And in the tool's _meta:
- group: the group a client offers the tool in, with groupSummary to describe the group;
- confirms: the tool asks the user in its own form before its main change (and any processing it starts), so a client
  needn't ask as well.
"""
from typing import Any

GROUPS = {
    'explore': "Find and read what is in Stroom: documents, feeds, streams, processing, templates, reference data, "
               "standing instructions",
    'onboarding': "Start and follow an onboarding or build: its plan and status, the feed, sample uploads, source "
                  "notes, surveying a feed",
    'translation': "Write and check the translation: Data Splitter, mapping, XSLT, dictionaries, reference XSLT",
    'pipelines': "Create, copy and change pipelines, and step them over sample data until clean",
    'processing': "Processor filters: process samples, reprocess after a change, wait for processing",
    'indexing': "Index fields, index docs and pipelines, Elasticsearch templates, verifying and searching an index",
    'cef': "CEF output for ArcSight",
    'diagnosis': "Find why an event is wrong, and what a feed's data the pipeline doesn't cover yet",
    'builds': "Documentation and promotion",
}

# tool: (group, read_only, destructive, confirms)
TOOLS: dict[str, tuple[str, bool, bool, bool]] = {
    # explore
    'find_documents': ('explore', True, False, False),
    'describe_document': ('explore', True, False, False),
    'describe_feed': ('explore', True, False, False),
    'find_streams': ('explore', True, False, False),
    'describe_stream': ('explore', True, False, False),
    'read_stream': ('explore', True, False, False),
    'summarise_streams': ('explore', True, False, False),
    'processing_status': ('explore', True, False, False),
    'get_instructions': ('explore', True, False, False),
    'find_reference_data': ('explore', True, False, False),
    'find_pipeline_templates': ('explore', True, False, False),
    'describe_template': ('explore', True, False, False),
    'find_elastic_clusters': ('explore', True, False, False),
    # onboarding
    'start_onboarding': ('onboarding', False, False, False),        # makes the build's folder
    'build_status': ('onboarding', True, False, False),
    'start_build': ('onboarding', False, False, False),
    'profile_sample': ('onboarding', True, False, False),
    'create_feed': ('onboarding', False, False, True),
    'upload_sample': ('onboarding', False, False, False),           # a stream in the build's feed
    'record_source_notes': ('onboarding', False, False, False),
    'survey_feed': ('onboarding', False, False, False),             # keeps its survey doc in the build
    'set_shape_handling': ('onboarding', False, False, True),
    # translation
    'build_data_splitter': ('translation', False, False, False),    # save_as saves the converter
    'save_text_converter': ('translation', False, False, False),
    'draft_translation_mapping': ('translation', True, False, False),
    'build_translation_xslt': ('translation', False, False, False),
    'save_xslt': ('translation', False, False, False),
    'save_dictionary': ('translation', False, False, False),
    'build_reference_xslt': ('translation', False, False, False),
    'check_xslt': ('translation', True, False, False),
    'rebuild_mapping': ('translation', False, False, True),
    # pipelines
    'create_pipeline': ('pipelines', False, False, True),
    'copy_pipeline': ('pipelines', False, False, True),
    'update_pipeline': ('pipelines', False, False, False),
    'step_pipeline': ('pipelines', True, False, False),
    'step_sample': ('pipelines', False, False, False),              # records a clean step in the build
    'step_records': ('pipelines', False, False, False),             # likewise
    'compare_outputs': ('pipelines', True, False, False),
    'check_events': ('pipelines', False, False, False),             # records the Events as validated
    # processing
    'create_processor_filter': ('processing', False, True, True),   # its form covers anything that processes
    'set_processor_filter_enabled': ('processing', False, False, False),    # only ever disables
    'reprocess_streams': ('processing', False, True, True),
    'wait_for_processing': ('processing', False, False, False),     # restores the build pipeline's batch size
    # indexing
    'get_field_conventions': ('indexing', True, False, False),
    'draft_index_mapping': ('indexing', False, False, False),       # keeps the example in the build
    'propose_index_template': ('indexing', False, False, True),
    'check_index_template': ('indexing', False, False, True),
    'create_index_doc': ('indexing', False, False, True),
    'create_indexing_pipeline': ('indexing', False, False, False),
    'rename_index': ('indexing', False, False, True),
    'verify_index': ('indexing', False, False, False),              # its dashboard, and the verified record
    'find_events': ('indexing', True, False, False),
    # cef
    'draft_cef_mapping': ('cef', False, False, False),
    # diagnosis
    'locate_event': ('diagnosis', True, False, False),
    'summarise_fix': ('diagnosis', True, False, False),
    'review_coverage': ('diagnosis', True, False, False),
    # builds
    'write_documentation': ('builds', False, False, False),
    'promote_build': ('builds', False, True, True),
}


def registration(name: str) -> dict[str, Any]:
    """FastMCP's tool() keywords for a tool: its annotations and _meta."""
    from mcp.types import ToolAnnotations
    group, read_only, destructive, confirms = TOOLS[name]
    return {'annotations': ToolAnnotations(readOnlyHint=read_only, destructiveHint=destructive if not read_only else None,
                                           openWorldHint=False),
            'meta': {'group': group, 'groupSummary': GROUPS[group], **({'confirms': True} if confirms else {})}}
