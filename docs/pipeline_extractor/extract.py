"""
Pipeline Flow Extractor — CLI entry point.

Scans the codebase for @pipeline markers and auto-extracted metadata,
then generates clarion_pipeline_flow.html.

Usage:
    python -m docs.pipeline_extractor.extract [--output FILE] [--validate-only] [--no-open]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from docs.pipeline_extractor import html_generator, js_extractor, python_extractor
from docs.pipeline_extractor.markers import MarkerRecord, parse_directory
from docs.pipeline_extractor.model import (
    Connector,
    FailureBox,
    GateBox,
    HardenCallout,
    ParallelBlock,
    ParallelBox,
    Phase,
    PipelineModel,
    PipelineStep,
    SectionDivider,
    StepTag,
)


def _find_project_root() -> Path:
    """Walk up from this file to find the project root (contains worker/ and extension/)."""
    return _PROJECT_ROOT


def _collect_markers(root: Path) -> list[MarkerRecord]:
    """Collect @pipeline markers from worker/ and extension/."""
    markers: list[MarkerRecord] = []
    for subdir in ["worker", "extension"]:
        d = root / subdir
        if d.is_dir():
            markers.extend(parse_directory(d))
    return markers


def _collect_constants(root: Path) -> dict[str, str]:
    """Auto-extract constants from Python and JS source files."""
    constants: dict[str, str] = {}

    worker_dir = root / "worker"
    if worker_dir.is_dir():
        py_info = python_extractor.extract_directory(worker_dir)
        for file_info in py_info.values():
            for c in file_info.constants:
                constants[c.name] = c.value

    ext_dir = root / "extension"
    if ext_dir.is_dir():
        js_info = js_extractor.extract_directory(ext_dir)
        for file_info in js_info.values():
            for c in file_info.constants:
                constants[c.name] = c.value

    return constants


def _group_markers(markers: list[MarkerRecord]) -> dict[str, list[MarkerRecord]]:
    """Group markers by phase — inferred from nearest preceding phase marker per file.

    Non-phase markers inherit the phase of the most recent phase marker
    in the same file (sorted by line number). Markers before any phase
    marker in a file are assigned to the first phase in that file.
    """
    # Sort all markers by (file, line) so we process in source order
    sorted_markers = sorted(markers, key=lambda m: (m.file, m.line))

    # First pass: find phase IDs per file, in order
    file_phases: dict[str, list[tuple[int, str]]] = {}
    for m in sorted_markers:
        if m.marker_type == "phase":
            phase_id = m.attrs.get("phase", "unknown")
            file_phases.setdefault(m.file, []).append((m.line, phase_id))

    # Second pass: assign each marker to the nearest preceding phase in its file
    groups: dict[str, list[MarkerRecord]] = {}
    for m in sorted_markers:
        if m.marker_type == "phase":
            phase_id = m.attrs.get("phase", "unknown")
        else:
            # Find the last phase marker at or before this line in this file
            phase_id = "unknown"
            for line_no, pid in reversed(file_phases.get(m.file, [])):
                if line_no <= m.line:
                    phase_id = pid
                    break
            else:
                # Before any phase in this file — use first phase if available
                phases_in_file = file_phases.get(m.file, [])
                if phases_in_file:
                    phase_id = phases_in_file[0][1]

        groups.setdefault(phase_id, []).append(m)
    return groups


def _build_step(m: MarkerRecord) -> PipelineStep:
    """Build a PipelineStep from a step marker."""
    tags: list[StepTag] = []

    for tag_attr in ["writes", "reads", "gate", "fatal", "nonfatal", "new"]:
        val = m.attrs.get(tag_attr)
        if val:
            tag_type = tag_attr
            if tag_attr in ("writes", "reads", "gate"):
                label = f"{tag_attr} \u00b7 {val}"
            else:
                label = val
            tags.append(StepTag(type=tag_type, label=label))

    return PipelineStep(
        num=m.attrs.get("num", "??"),
        name=m.attrs.get("step", "Unnamed step"),
        description=m.attrs.get("desc", ""),
        detail=m.attrs.get("detail", ""),
        tags=tags,
        source_file=m.file,
        source_line=m.line,
    )


def _build_gate(m: MarkerRecord) -> GateBox:
    """Build a GateBox from a gate marker with conditions."""
    conditions = []
    # Conditions are stored as condition-1, condition-2, etc. or as a single condition
    single = m.attrs.get("condition")
    if single:
        conditions.append(single)
    for i in range(1, 10):
        c = m.attrs.get(f"condition-{i}")
        if c:
            conditions.append(c)
    return GateBox(
        title=m.attrs.get("gate", "Gate"),
        conditions=conditions,
    )


def _build_parallel_box(m: MarkerRecord) -> ParallelBox:
    """Build a ParallelBox from a parallel-box marker."""
    tags: list[StepTag] = []
    for tag_attr in ["fatal", "nonfatal"]:
        val = m.attrs.get(tag_attr)
        if val:
            tags.append(StepTag(type=tag_attr, label=val))

    notes: list[str] = []
    skip_note = m.attrs.get("skip-note")
    if skip_note:
        notes.append(skip_note)
    new_note = m.attrs.get("new-note")
    if new_note:
        notes.append(new_note)

    return ParallelBox(
        title=m.attrs.get("title", ""),
        description=m.attrs.get("desc", ""),
        color=m.attrs.get("color", "blue"),
        dashed=m.attrs.get("dashed", "false").lower() == "true",
        tags=tags,
        notes=notes,
    )


def _build_parallel_block(group_name: str, boxes: list[ParallelBox], label: str = "") -> ParallelBlock:
    return ParallelBlock(label=label or group_name, boxes=boxes)


def _build_harden(m: MarkerRecord) -> HardenCallout:
    return HardenCallout(
        title=m.attrs.get("harden", "Hardening note"),
        body=m.attrs.get("body", ""),
    )


def _build_divider(m: MarkerRecord) -> SectionDivider:
    return SectionDivider(label=m.attrs.get("divider", ""))


def _build_connector(m: MarkerRecord) -> Connector:
    return Connector(label=m.attrs.get("label", m.attrs.get("connector", "")))


def _build_phase_from_markers(
    phase_id: str, phase_markers: list[MarkerRecord]
) -> tuple[Phase | None, Connector | None]:
    """Build a Phase and optional trailing Connector from a group of markers."""
    # Find the phase definition marker (marker_type == "phase")
    phase_def = None
    for m in phase_markers:
        if m.marker_type == "phase":
            phase_def = m
            break

    if not phase_def:
        return None, None

    colors = COLOR_MAP = {
        "a": "blue", "b": "coral", "c": "purple", "d": "teal",
    }
    letter = phase_id[-1].lower() if phase_id else "a"

    phase = Phase(
        id=f"phase-{letter}",
        badge=phase_def.attrs.get("badge", f"PHASE {letter.upper()}"),
        title=phase_def.attrs.get("title", ""),
        system=phase_def.attrs.get("system", ""),
        color=colors.get(letter, "blue"),
        body=[],
    )

    # Sort remaining markers by line number to preserve source order
    body_markers = sorted(
        [m for m in phase_markers if m is not phase_def],
        key=lambda m: (m.file, m.line),
    )

    # Collect parallel boxes into groups
    parallel_groups: dict[str, list[tuple[int, ParallelBox]]] = {}
    parallel_labels: dict[str, str] = {}
    connector = None

    # First pass: identify parallel groups and their labels
    for m in body_markers:
        if m.marker_type == "parallel":
            group = m.attrs.get("parallel", "")
            parallel_labels[group] = m.attrs.get("label", group)

    # Second pass: build body elements in order
    seen_parallel_groups: set[str] = set()

    for idx, m in enumerate(body_markers):
        if m.marker_type == "step":
            phase.body.append(_build_step(m))

        elif m.marker_type == "gate":
            standalone = m.attrs.get("standalone", "false").lower() == "true"
            if standalone:
                phase.body.append(_build_gate(m))
            else:
                # Non-standalone gates are attached to the nearest step — skip here
                pass

        elif m.marker_type == "parallel":
            group = m.attrs.get("parallel", "")
            if group not in seen_parallel_groups:
                seen_parallel_groups.add(group)
                # Collect all parallel-box markers for this group
                boxes: list[ParallelBox] = []
                for m2 in body_markers:
                    if m2.marker_type == "parallel-box" and m2.attrs.get("group") == group:
                        boxes.append(_build_parallel_box(m2))
                if boxes:
                    phase.body.append(_build_parallel_block(
                        group, boxes, parallel_labels.get(group, group)
                    ))

        elif m.marker_type == "parallel-box":
            pass  # Handled by parallel group collection above

        elif m.marker_type == "harden":
            phase.body.append(_build_harden(m))

        elif m.marker_type == "divider":
            phase.body.append(_build_divider(m))

        elif m.marker_type == "connector":
            connector = _build_connector(m)

    return phase, connector


def _load_step_details() -> dict[str, str]:
    """Load step detail text from the sidecar JSON file."""
    details_path = Path(__file__).resolve().parent / "step_details.json"
    if details_path.exists():
        return json.loads(details_path.read_text(encoding="utf-8"))
    return {}


def _apply_step_details(model: PipelineModel, details: dict[str, str]) -> None:
    """Merge step details into the model. Marker detail= takes precedence."""
    if not details:
        return
    for phase in model.phases:
        for element in phase.body:
            if isinstance(element, PipelineStep) and not element.detail:
                element.detail = details.get(element.name, "")


def build_model(root: Path) -> PipelineModel:
    """Build the full PipelineModel from codebase markers and extracted data."""
    markers = _collect_markers(root)
    constants = _collect_constants(root)

    if not markers:
        print("WARNING: No @pipeline markers found. Generating empty pipeline.")
        return PipelineModel(
            extraction_timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            constants=constants,
        )

    grouped = _group_markers(markers)

    # Sort phase groups by their phase ID to maintain order
    # Phase IDs are expected to be like "ext-sync", "onboarding-gate", etc.
    # Or simply "a", "b", "c", "d"
    phase_order = sorted(grouped.keys())

    model = PipelineModel(
        extraction_timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        constants=constants,
    )

    for phase_id in phase_order:
        phase_markers = grouped[phase_id]
        phase, connector = _build_phase_from_markers(phase_id, phase_markers)
        if phase:
            model.phases.append(phase)
        if connector:
            model.connectors.append(connector)

    # Merge step details from sidecar JSON
    _apply_step_details(model, _load_step_details())

    return model


def validate(root: Path) -> list[str]:
    """Validate markers and return a list of warnings."""
    markers = _collect_markers(root)
    warnings: list[str] = []

    if not markers:
        warnings.append("No @pipeline markers found in codebase.")
        return warnings

    # Check that each step has required fields
    for m in markers:
        if m.marker_type == "step":
            if not m.attrs.get("num"):
                warnings.append(f"{m.file}:{m.line} — step missing 'num' attribute")
            if not m.attrs.get("desc"):
                warnings.append(f"{m.file}:{m.line} — step missing 'desc' attribute")

        if m.marker_type == "phase":
            if not m.attrs.get("title"):
                warnings.append(f"{m.file}:{m.line} — phase missing 'title' attribute")

    # Check for orphaned parallel-box markers (no parent parallel group)
    parallel_groups = {m.attrs.get("parallel") for m in markers if m.marker_type == "parallel"}
    for m in markers:
        if m.marker_type == "parallel-box":
            group = m.attrs.get("group")
            if group and group not in parallel_groups:
                warnings.append(
                    f"{m.file}:{m.line} — parallel-box references group '{group}' "
                    f"but no @pipeline parallel=\"{group}\" marker found"
                )

    return warnings


def main():
    parser = argparse.ArgumentParser(description="Generate pipeline flow documentation from codebase markers")
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output HTML file path (default: clarion_pipeline_flow.html in project root)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate markers, don't generate HTML",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't auto-open the HTML in the browser",
    )
    args = parser.parse_args()

    root = _find_project_root()
    print(f"Project root: {root}")

    if args.validate_only:
        warnings = validate(root)
        if warnings:
            print(f"\n{len(warnings)} warning(s) found:\n")
            for w in warnings:
                print(f"  - {w}")
            sys.exit(1)
        else:
            print("All markers valid.")
            sys.exit(0)

    # Build model and generate
    model = build_model(root)
    html = html_generator.generate(model)

    output_path = Path(args.output) if args.output else root / "clarion_pipeline_flow.html"
    output_path.write_text(html, encoding="utf-8")

    phase_count = len(model.phases)
    step_count = sum(
        1 for p in model.phases for el in p.body if isinstance(el, PipelineStep)
    )
    print(f"\nGenerated: {output_path}")
    print(f"  Phases: {phase_count}")
    print(f"  Steps:  {step_count}")
    print(f"  Constants extracted: {len(model.constants)}")

    if not args.no_open:
        _open_in_browser(output_path)


def _open_in_browser(path: Path):
    """Open the HTML file in the default browser."""
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        print("  Opened in browser.")
    except Exception as e:
        print(f"  Could not auto-open: {e}")


if __name__ == "__main__":
    main()
