"""The tool modules the server registers, in one place for main.py and the dev runner."""
from tools import (builds, cef, coverage, describe, diagnosis, rebuild, explorer, feeds, generation, instructions, plan, reference, sampling, indexing, pipeline_writes, pipelines, processing, processing_writes,
                   stepping, streams, templates, translation, validation)

TOOL_MODULES = (explorer, feeds, pipelines, pipeline_writes, templates, translation, streams, stepping, processing,
                processing_writes, validation, generation, indexing, builds, diagnosis, sampling, instructions, reference, cef, coverage, describe, rebuild, plan)
