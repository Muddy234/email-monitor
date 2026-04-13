"""Generates the pipeline flow HTML from a PipelineModel."""

from __future__ import annotations

from html import escape

from .model import (
    Connector,
    FailureBox,
    GateBox,
    HardenCallout,
    ParallelBlock,
    Phase,
    PipelineModel,
    PipelineStep,
    SectionDivider,
    StepTag,
)
from .template import COLOR_MAP, CSS, FONT_LINK, JS, LEGEND_ITEMS


def generate(model: PipelineModel) -> str:
    """Generate the complete HTML document from a PipelineModel."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(model.title)}</title>
{FONT_LINK}
<style>
{CSS}
</style>
</head>
<body>
<div class="container">
{_render_header(model)}
{_render_legend()}
{_render_phases_and_connectors(model)}
</div>
<script>
{JS}
</script>
</body>
</html>"""


def _render_header(model: PipelineModel) -> str:
    subtitle = model.subtitle or (
        "End-to-end data flow from extension sync through onboarding "
        "to normal processing. Click any phase or step to expand."
    )
    ts = ""
    if model.extraction_timestamp:
        ts = f'\n  <p style="margin-top:4px;font-family:var(--mono);font-size:11px;color:var(--text-dim)">Generated: {escape(model.extraction_timestamp)}</p>'
    return f"""<header>
  <div class="header-row">
    <h1>{escape(model.title)}</h1>
    <button id="expand-btn" class="expand-btn" data-state="collapsed" onclick="toggleAll()">Expand all</button>
  </div>
  <p>{escape(subtitle)}</p>{ts}
</header>"""


def _render_legend() -> str:
    items = "\n".join(
        f'  <div class="legend-item">'
        f'<div class="legend-dot" style="background:var(--{color})"></div>'
        f'{escape(label)}</div>'
        for color, label in LEGEND_ITEMS
    )
    return f'<div class="legend">\n{items}\n</div>'


def _render_phases_and_connectors(model: PipelineModel) -> str:
    parts: list[str] = []
    for i, phase in enumerate(model.phases):
        parts.append(_render_phase(phase))
        # Add connector after this phase if one exists
        if i < len(model.connectors):
            parts.append(_render_connector(model.connectors[i]))
    return "\n\n".join(parts)


def _render_connector(conn: Connector) -> str:
    return (
        f'<div class="connector"><div>'
        f'<div class="connector-line"></div>'
        f'<div class="connector-label">{escape(conn.label)}</div>'
        f'</div></div>'
    )


def _render_phase(phase: Phase) -> str:
    colors = COLOR_MAP.get(phase.color, COLOR_MAP["blue"])
    body_html = _render_phase_body(phase.body)
    return f"""<div class="phase" id="{escape(phase.id)}">
  <div class="phase-header" onclick="togglePhase('{escape(phase.id)}')">
    <span class="phase-badge" style="background:{colors['bg']};color:{colors['fg']};border:1px solid {colors['border']}">{escape(phase.badge)}</span>
    <span class="phase-title">{escape(phase.title)}</span>
    <span class="phase-system">{escape(phase.system)}</span>
    <span class="phase-chevron">&rsaquo;</span>
  </div>
  <div class="phase-body">
{body_html}
  </div>
</div>"""


def _render_phase_body(elements: list) -> str:
    """Render a list of phase body elements, grouping consecutive steps into step-lists."""
    parts: list[str] = []
    step_buffer: list[str] = []

    def flush_steps():
        if step_buffer:
            parts.append(f'    <div class="step-list">\n' + "\n".join(step_buffer) + "\n    </div>")
            step_buffer.clear()

    for el in elements:
        if isinstance(el, PipelineStep):
            step_buffer.append(_render_step(el))
        else:
            flush_steps()
            if isinstance(el, ParallelBlock):
                parts.append(_render_parallel_block(el))
            elif isinstance(el, GateBox):
                parts.append(_render_gate_box(el))
            elif isinstance(el, FailureBox):
                parts.append(_render_failure_box(el))
            elif isinstance(el, HardenCallout):
                parts.append(_render_harden_callout(el))
            elif isinstance(el, SectionDivider):
                parts.append(_render_section_divider(el))

    flush_steps()
    return "\n".join(parts)


def _render_tag(tag: StepTag) -> str:
    return f'<span class="step-tag tag-{escape(tag.type)}">{escape(tag.label)}</span>'


def _render_step(step: PipelineStep) -> str:
    tags_html = ""
    if step.tags:
        tags_html = (
            '\n        <div class="step-meta">'
            + "".join(_render_tag(t) for t in step.tags)
            + "</div>"
        )
    detail_html = ""
    if step.detail:
        detail_html = f'\n        <div class="step-detail"><p>{step.detail}</p></div>'
    return f"""      <div class="step" onclick="toggleStep(this)">
        <div class="step-head"><span class="step-num">{escape(step.num)}</span><span class="step-name">{escape(step.name)}</span></div>
        <div class="step-desc">{escape(step.description)}</div>{tags_html}{detail_html}
      </div>"""


def _render_parallel_block(block: ParallelBlock) -> str:
    boxes = "\n".join(_render_parallel_box(b) for b in block.boxes)
    return f"""    <div class="parallel-block">
      <div class="parallel-label">{escape(block.label)}</div>
      <div class="parallel-row">
{boxes}
      </div>
    </div>"""


def _render_parallel_box(box) -> str:
    colors = COLOR_MAP.get(box.color, COLOR_MAP["blue"])
    dashed = " dashed" if box.dashed else ""
    border_style = f' style="border-color:{colors["border"]}"' if not box.dashed else f' style="border-color:{COLOR_MAP["amber"]["border"]}"'

    tags_html = ""
    if box.tags:
        tags_html = "\n" + "\n".join(
            f'          <div style="margin-top:6px">{_render_tag(t)}</div>'
            for t in box.tags
        )

    notes_html = ""
    for note in box.notes:
        if "skip" in note.lower():
            notes_html += f'\n          <div class="skip-note">{escape(note)}</div>'
        elif "new" in note.lower():
            notes_html += f'\n          <div class="new-note">{escape(note)}</div>'
        else:
            notes_html += f'\n          <div class="skip-note">{escape(note)}</div>'

    title_color = colors["fg"] if not box.dashed else COLOR_MAP["amber"]["fg"]

    return f"""        <div class="parallel-box{dashed}"{border_style}>
          <h4 style="color:{title_color}">{escape(box.title)}</h4>
          <p>{escape(box.description)}</p>{tags_html}{notes_html}
        </div>"""


def _render_gate_box(gate: GateBox) -> str:
    conditions = "\n".join(
        f"        <li><code>{escape(c)}</code></li>" for c in gate.conditions
    )
    return f"""    <div class="gate-box">
      <h4>{escape(gate.title)}</h4>
      <ul>
{conditions}
      </ul>
    </div>"""


def _render_failure_box(fb: FailureBox) -> str:
    cls = "fatal" if fb.fatal else "nonfatal"
    return f"""    <div class="failure-box {cls}">
      <h4>{escape(fb.title)}</h4>
      <p>{escape(fb.description)}</p>
    </div>"""


def _render_harden_callout(hc: HardenCallout) -> str:
    return f"""    <div class="harden-callout">
      <h4>{escape(hc.title)}</h4>
      <p>{hc.body}</p>
    </div>"""


def _render_section_divider(sd: SectionDivider) -> str:
    return f'    <div class="section-divider"><span>{escape(sd.label)}</span></div>'
