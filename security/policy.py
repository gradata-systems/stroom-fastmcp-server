"""Server-side policy: where template pipelines live (and, later, where the agent may write)."""
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

Stage = Literal['translation', 'indexing', 'discovery']


class TemplateSource(BaseModel):
    folders: list[str] = Field(default_factory=list, description="Explorer paths, e.g. 'System/Template Pipelines'.")
    names: list[str] = Field(default_factory=list, description="Glob patterns on pipeline names.")


class AccessPolicy(BaseModel):
    pipeline_templates: dict[Stage, TemplateSource] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> 'AccessPolicy':
        with path.open(encoding='utf-8') as f:
            return cls.model_validate(yaml.safe_load(f) or {})
