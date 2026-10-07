"""Server-side policy: where template pipelines live (and, later, where the agent may write)."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

Stage = Literal['translation', 'indexing', 'discovery', 'reference', 'records']


class TemplateSource(BaseModel):
    folders: list[str] = Field(default_factory=list, description="Explorer paths, e.g. 'System/Template Pipelines'.")
    names: list[str] = Field(default_factory=list, description="Glob patterns on pipeline names.")


class StageMarkers(BaseModel):
    """What marks a pipeline as a stage, when the environment's templates differ from Stroom's defaults."""
    schema_groups: list[str] = Field(default_factory=list, description="SchemaFilter schemaGroup values.")
    stream_types: list[str] = Field(default_factory=list, description="StreamAppender streamType values.")


DEFAULT_MARKERS = {'translation': StageMarkers(schema_groups=['EVENTS'], stream_types=['Events']),
                   'reference': StageMarkers(schema_groups=['REFERENCE_DATA'], stream_types=['Reference']),
                   # A pipeline that writes records:2 records as a stream of their own (e.g. a source's parsed text).
                   'records': StageMarkers(schema_groups=['RECORDS'], stream_types=['Records'])}


class AccessPolicy(BaseModel):
    pipeline_templates: dict[Stage, TemplateSource] = Field(default_factory=dict)
    stage_markers: dict[str, StageMarkers] = Field(default_factory=lambda: dict(DEFAULT_MARKERS))

    def markers(self) -> dict[str, StageMarkers]:
        return {**DEFAULT_MARKERS, **self.stage_markers}

    @classmethod
    def load(cls, path: Path) -> 'AccessPolicy':
        with path.open(encoding='utf-8') as f:
            return cls.model_validate(yaml.safe_load(f) or {})
