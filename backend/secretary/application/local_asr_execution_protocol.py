"""Server-owned protocol between the local executor and a pinned runtime adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol

from secretary.domain.local_asr import ImportLocalAsrRun, SourceSegment
from secretary.application.local_asr_lifecycle import ExecutionPlan, RuntimeQualification


@dataclass(frozen=True)
class ExecutionContext:
    meeting_id: str
    transcript_version: int
    input_path: Path
    input_sha256: str
    source_segments: tuple[SourceSegment, ...]
    operation_id: str
    source_text_sha256: str | None
    duration_ms: int
    canonical_audio_path: Path | None = None
    canonical_audio_sha256: str | None = None


@dataclass(frozen=True)
class StageSpec:
    stage: str
    command: tuple[str, ...]
    outputs: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 1000
    binary_outputs: tuple[str, ...] = ()
    # Only installed adapters may declare verified source directories. These
    # extend the executor's guarded path; environment cannot replace it.
    python_paths: tuple[Path, ...] = ()


class LocalAsrRuntime(Protocol):
    def plan_factory(self, meeting_id: str, transcript_version: int,
                     input_sha256: str, source_text_sha256: str | None) -> ExecutionPlan: ...
    def qualification(self, plan: ExecutionPlan) -> RuntimeQualification: ...
    def build_attempt(self, plan: ExecutionPlan, context: ExecutionContext,
                      workspace: Path) -> tuple[StageSpec, ...]: ...
    def read_result(self, plan: ExecutionPlan, context: ExecutionContext,
                    workspace: Path) -> ImportLocalAsrRun: ...
