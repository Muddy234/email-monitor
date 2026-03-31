"""Data classes representing the pipeline documentation model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepTag:
    type: str  # writes, reads, gate, fatal, nonfatal, new
    label: str  # e.g. "writes · emails table"


@dataclass
class PipelineStep:
    num: str  # e.g. "01", "13"
    name: str
    description: str
    detail: str = ""
    tags: list[StepTag] = field(default_factory=list)
    source_file: str = ""
    source_line: int = 0


@dataclass
class ParallelBox:
    title: str
    description: str
    color: str = "blue"  # CSS variable name
    dashed: bool = False
    tags: list[StepTag] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ParallelBlock:
    label: str
    boxes: list[ParallelBox] = field(default_factory=list)


@dataclass
class GateBox:
    title: str
    conditions: list[str] = field(default_factory=list)


@dataclass
class FailureBox:
    title: str
    description: str
    fatal: bool = True


@dataclass
class HardenCallout:
    title: str
    body: str


@dataclass
class SectionDivider:
    label: str


@dataclass
class Connector:
    label: str


# Union type for phase body elements
PhaseBodyElement = (
    PipelineStep
    | ParallelBlock
    | GateBox
    | FailureBox
    | HardenCallout
    | SectionDivider
)


@dataclass
class Phase:
    id: str  # "phase-a", "phase-b", etc.
    badge: str  # "PHASE A"
    title: str  # "Extension sync loop"
    system: str  # "Chrome ext. · 45s cycle"
    color: str  # "blue", "coral", "purple", "teal"
    body: list[PhaseBodyElement] = field(default_factory=list)


@dataclass
class PipelineModel:
    title: str = "Clarion AI — Pipeline flow"
    subtitle: str = ""
    phases: list[Phase] = field(default_factory=list)
    connectors: list[Connector] = field(default_factory=list)
    constants: dict[str, Any] = field(default_factory=dict)
    extraction_timestamp: str = ""
