"""Pipeline engine: state machine, step types, manifest, run lifecycle (P3).

The orchestrator is thin and explicit by design (plan §2): a write-ahead
manifest checkpoints every step so a ``kill -9`` resumes correctly.
"""

from __future__ import annotations

from gauntlet.engine.config import AgentProfile, RunConfig
from gauntlet.engine.manifest import Manifest
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline, load_pipeline
from gauntlet.engine.run import RunManager
from gauntlet.engine.validate import PipelineValidationError, validate_pipeline

__all__ = [
    "AgentProfile",
    "RunConfig",
    "Manifest",
    "Orchestrator",
    "Pipeline",
    "load_pipeline",
    "RunManager",
    "PipelineValidationError",
    "validate_pipeline",
]
