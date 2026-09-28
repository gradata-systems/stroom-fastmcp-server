"""Generating translation code from a mapping, so the model does not have to write XSLT by hand."""
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from utils.eventschema import EventSchema
from utils.schemas import SchemaCache, event_logging_system_id
from utils.stroom import gateway_from
from utils.xsltgen import TranslationMapping, generate


async def event_schema(ctx: Context, version: str) -> EventSchema:
    schemas = ctx.lifespan_context.setdefault('event_schemas', {})
    if version not in schemas:
        cache = ctx.lifespan_context.setdefault('schemas', SchemaCache(gateway_from(ctx)))
        schemas[version] = EventSchema.parse(await cache.source(event_logging_system_id(version)))
    return schemas[version]


async def build_translation_xslt(
        ctx: Context,
        mapping: Annotated[TranslationMapping, Field(description="Which input field or constant goes to which "
                                                                 "event-logging path, per kind of event.")],
        schema_version: Annotated[str | None, Field(
            description="Event-logging version, e.g. '3.5.2'. Defaults to the configured version.")] = None,
) -> dict[str, Any]:
    """
    Write the event-logging translation XSLT from a field mapping instead of by hand. Give the input kind
    (data_splitter, json or xml), fields every event shares (time, System, Device...), and one rule per
    kind of event with its conditions and fields. Paths are checked against the schema: unknown paths come
    back with suggestions, constants are checked against allowed values, and elements are written in schema
    order with empty inputs left out. Fix any problems in the mapping and call again; then step_sample with
    draft_code={'<xslt element>': xslt}. Saves nothing.
    """
    version = schema_version or gateway_from(ctx).settings.event_logging_version
    result = generate(mapping, await event_schema(ctx, version), version)
    result['schema_version'] = version
    result['hint'] = ("Fix the problems in the mapping (not the XSLT) and call again." if not result['ok'] else
                      "Step it: step_sample(pipeline, streams, draft_code={'translationFilter': xslt}) (use the "
                      "pipeline's XSLT element id). Fix issues in the mapping and regenerate; save with create_xslt.")
    return result


ALL_TOOLS = [build_translation_xslt]
