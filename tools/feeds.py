"""Tools for understanding sample data before a feed is created."""
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from utils.profile import profile


async def profile_sample(
        ctx: Context,
        sample: Annotated[str, Field(description="A representative sample of the raw data, several records long.")],
) -> dict[str, Any]:
    """
    Profile a raw data sample locally (nothing is sent to Stroom): its format (XML, JSON array or lines,
    delimited with or without a header, RFC 3164/5424 syslog, key=value), record structure, and each
    field's fill rate, inferred type and examples. Timestamps get a suggested stroom:format-date pattern;
    string fields holding JSON are flagged as embedded JSON. Also suggests the parser and template to use.
    """
    return profile(sample)


ALL_TOOLS = [profile_sample]
