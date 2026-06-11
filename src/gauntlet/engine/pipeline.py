"""Pipeline model + loader (FR-5).

A pipeline is a YAML document: ``name``, ``version``, and ordered ``stages``,
each with ordered ``steps``. Steps carry first-class ``when:``/``foreach:``/
``on_fail:`` attributes and per-step overrides (FR-5.4). Unknown keys are
preserved (``extra="allow"``) so custom step types (FR-5.5) and type-specific
fields need no model change. Versioning is ``version:`` + a content hash of the
exact bytes loaded (FR-5.6), both recorded in the manifest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class OnFail(BaseModel):
    """Failure routing for a step (FR-5.4)."""

    route_to: str
    max_retries: int = 0


class Step(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str

    # first-class control attributes (FR-5.4)
    agent: str | None = None
    when: str | None = None
    foreach: str | None = None
    on_fail: OnFail | None = None

    # per-step overrides (FR-5.4) — engine-level budget guards (FR-3.3)
    max_turns: int | None = None
    budget_usd: float | None = None
    timeout_s: float | None = None

    def get(self, key: str, default: Any = None) -> Any:
        """Read a type-specific field — declared or extra.

        Deliberately consults only model fields and ``extra``; using ``hasattr``
        would collide with pydantic's own methods (``schema``, ``dict``, ``copy``)
        and return a bound method for, e.g., a step's ``schema:`` key.
        """
        if key in type(self).model_fields:
            return getattr(self, key)
        extra = self.__pydantic_extra__ or {}
        return extra.get(key, default)


class Stage(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    foreach: str | None = None
    when: str | None = None
    steps: list[Step]


class Pipeline(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    version: int
    stages: list[Stage]

    def all_steps(self) -> list[Step]:
        return [step for stage in self.stages for step in stage.steps]


def content_hash(text: str) -> str:
    """Stable content hash of the pipeline source (FR-5.6)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_pipeline(path: Path) -> tuple[Pipeline, str]:
    """Load and parse a pipeline file; return ``(pipeline, content_hash)``.

    Parsing only — semantic load-time validation (dangling artifacts, adapter
    capabilities, banned flags) lives in :mod:`gauntlet.engine.validate` so the
    model stays free of cross-module imports.
    """
    if not path.exists():
        raise FileNotFoundError(f"pipeline not found at {path}")
    text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping, got {type(data).__name__}")
    pipeline = Pipeline.model_validate(data)
    _assert_unique_ids(pipeline)
    return pipeline, content_hash(text)


def _assert_unique_ids(pipeline: Pipeline) -> None:
    seen: set[str] = set()
    for step in pipeline.all_steps():
        if step.id in seen:
            raise ValueError(f"duplicate step id {step.id!r} in pipeline")
        seen.add(step.id)
    stage_ids: set[str] = set()
    for stage in pipeline.stages:
        if stage.id in stage_ids:
            raise ValueError(f"duplicate stage id {stage.id!r} in pipeline")
        stage_ids.add(stage.id)
