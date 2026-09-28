"""The tool modules the server registers, in one place for main.py and the dev runner."""
from tools import (builds, explorer, feeds, indexing, pipeline_writes, pipelines, processing, processing_writes,
                   stepping, streams, templates, translation, validation)

TOOL_MODULES = (explorer, feeds, pipelines, pipeline_writes, templates, translation, streams, stepping, processing,
                processing_writes, validation, indexing, builds)
