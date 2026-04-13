"""Static HTML template fragments for pipeline visualization."""

FONT_LINK = '<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">'

CSS = """\
:root {
  --bg: #0f1117;
  --bg-card: #181b24;
  --bg-card-hover: #1e2230;
  --bg-raised: #232736;
  --border: rgba(255,255,255,0.06);
  --border-hover: rgba(255,255,255,0.12);
  --text: #e2e4ea;
  --text-muted: #8b8fa4;
  --text-dim: #555a6e;
  --blue: #4d9ef6;
  --blue-bg: rgba(77,158,246,0.08);
  --blue-border: rgba(77,158,246,0.2);
  --coral: #e8693c;
  --coral-bg: rgba(232,105,60,0.08);
  --coral-border: rgba(232,105,60,0.2);
  --purple: #8b7bdf;
  --purple-bg: rgba(139,123,223,0.08);
  --purple-border: rgba(139,123,223,0.2);
  --teal: #34c891;
  --teal-bg: rgba(52,200,145,0.08);
  --teal-border: rgba(52,200,145,0.2);
  --amber: #e8a838;
  --amber-bg: rgba(232,168,56,0.08);
  --amber-border: rgba(232,168,56,0.2);
  --red: #e05252;
  --red-bg: rgba(224,82,82,0.08);
  --red-border: rgba(224,82,82,0.2);
  --green: #5cb85c;
  --mono: 'JetBrains Mono', monospace;
  --sans: 'DM Sans', -apple-system, sans-serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:var(--sans); line-height:1.6; min-height:100vh; }
.container { max-width:1080px; margin:0 auto; padding:40px 24px 80px; }

header { margin-bottom:48px; }
header h1 { font-size:28px; font-weight:700; letter-spacing:-0.5px; margin-bottom:6px; }
header p { color:var(--text-muted); font-size:14px; }
.header-row { display:flex; align-items:center; justify-content:space-between; gap:16px; }
.expand-btn { font-family:var(--mono); font-size:12px; color:var(--text-muted); background:var(--bg-card); border:1px solid var(--border); border-radius:6px; padding:6px 14px; cursor:pointer; white-space:nowrap; transition:all 0.15s; }
.expand-btn:hover { background:var(--bg-card-hover); border-color:var(--border-hover); color:var(--text); }

.legend { display:flex; gap:20px; flex-wrap:wrap; margin-bottom:32px; padding:14px 18px; background:var(--bg-card); border:1px solid var(--border); border-radius:10px; }
.legend-item { display:flex; align-items:center; gap:7px; font-size:12px; color:var(--text-muted); font-family:var(--mono); }
.legend-dot { width:10px; height:10px; border-radius:3px; }

.connector { display:flex; justify-content:center; padding:8px 0; }
.connector-line { width:2px; height:32px; background:linear-gradient(to bottom, var(--border-hover), transparent); position:relative; }
.connector-line::after { content:''; position:absolute; bottom:-3px; left:-3px; width:8px; height:8px; border-right:2px solid var(--text-dim); border-bottom:2px solid var(--text-dim); transform:rotate(45deg); }
.connector-label { font-size:11px; font-family:var(--mono); color:var(--text-dim); text-align:center; padding-top:2px; }

.phase { border:1px solid var(--border); border-radius:12px; background:var(--bg-card); overflow:hidden; transition:border-color 0.2s; }
.phase:hover { border-color:var(--border-hover); }
.phase-header { padding:18px 22px; cursor:pointer; display:flex; align-items:center; gap:14px; user-select:none; }
.phase-header:hover { background:var(--bg-card-hover); }
.phase-badge { font-family:var(--mono); font-size:11px; font-weight:600; padding:3px 10px; border-radius:6px; white-space:nowrap; letter-spacing:0.3px; }
.phase-title { font-size:16px; font-weight:600; flex:1; }
.phase-system { font-family:var(--mono); font-size:11px; color:var(--text-dim); padding:3px 8px; border:1px solid var(--border); border-radius:5px; }
.phase-chevron { color:var(--text-dim); transition:transform 0.25s; font-size:18px; }
.phase.open .phase-chevron { transform:rotate(90deg); }

.phase-body { display:none; border-top:1px solid var(--border); }
.phase.open .phase-body { display:block; }

.step-list { padding:16px 22px; }
.step { padding:14px 16px; border-radius:8px; margin-bottom:8px; border:1px solid transparent; transition:all 0.15s; cursor:pointer; }
.step:hover { background:var(--bg-raised); border-color:var(--border); }
.step:last-child { margin-bottom:0; }
.step-head { display:flex; align-items:baseline; gap:10px; margin-bottom:4px; }
.step-num { font-family:var(--mono); font-size:11px; color:var(--text-dim); min-width:28px; }
.step-name { font-weight:500; font-size:14px; }
.step-desc { font-size:13px; color:var(--text-muted); padding-left:38px; }
.step-meta { display:flex; gap:8px; padding-left:38px; margin-top:8px; flex-wrap:wrap; }
.step-tag { font-family:var(--mono); font-size:10px; padding:2px 8px; border-radius:4px; }
.tag-writes { background:var(--blue-bg); color:var(--blue); border:1px solid var(--blue-border); }
.tag-reads { background:var(--teal-bg); color:var(--teal); border:1px solid var(--teal-border); }
.tag-gate { background:var(--coral-bg); color:var(--coral); border:1px solid var(--coral-border); }
.tag-fatal { background:var(--red-bg); color:var(--red); border:1px solid rgba(224,82,82,0.25); }
.tag-nonfatal { background:var(--amber-bg); color:var(--amber); border:1px solid var(--amber-border); }
.tag-new { background:var(--purple-bg); color:var(--purple); border:1px solid var(--purple-border); }

.parallel-block { margin:16px 22px; padding:18px; background:var(--bg-raised); border-radius:10px; border:1px solid var(--border); }
.parallel-label { font-family:var(--mono); font-size:11px; color:var(--text-dim); margin-bottom:12px; letter-spacing:0.5px; text-transform:uppercase; }
.parallel-row { display:flex; gap:10px; flex-wrap:wrap; }
.parallel-box { flex:1; min-width:140px; padding:12px 14px; border-radius:8px; border:1px solid var(--border); background:var(--bg-card); }
.parallel-box h4 { font-size:13px; font-weight:600; margin-bottom:4px; }
.parallel-box p { font-size:11px; color:var(--text-muted); line-height:1.5; }
.parallel-box.dashed { border-style:dashed; opacity:0.7; }
.parallel-box .skip-note { font-family:var(--mono); font-size:10px; color:var(--amber); margin-top:6px; }
.parallel-box .new-note { font-family:var(--mono); font-size:10px; color:var(--purple); margin-top:6px; }

.parallel-connector { display:flex; justify-content:center; padding:6px 0; }
.parallel-connector-line { width:1px; height:20px; background:var(--border-hover); }

.gate-box { margin:0 22px 16px; padding:16px 18px; background:var(--coral-bg); border:1px solid var(--coral-border); border-radius:10px; }
.gate-box h4 { font-size:13px; font-weight:600; color:var(--coral); margin-bottom:8px; font-family:var(--mono); }
.gate-box ul { list-style:none; padding:0; }
.gate-box li { font-size:12px; color:var(--text-muted); padding:3px 0; padding-left:16px; position:relative; }
.gate-box li::before { content:'\\2192'; position:absolute; left:0; color:var(--coral); }

.failure-box { margin:0 22px 16px; padding:14px 18px; border-radius:10px; }
.failure-box.fatal { background:var(--red-bg); border:1px solid rgba(224,82,82,0.2); }
.failure-box.nonfatal { background:var(--amber-bg); border:1px solid var(--amber-border); }
.failure-box h4 { font-size:12px; font-weight:600; font-family:var(--mono); margin-bottom:4px; }
.failure-box.fatal h4 { color:var(--red); }
.failure-box.nonfatal h4 { color:var(--amber); }
.failure-box p { font-size:12px; color:var(--text-muted); }

.section-divider { padding:0 22px 16px; }
.section-divider span { font-family:var(--mono); font-size:11px; color:var(--text-dim); letter-spacing:0.5px; text-transform:uppercase; display:flex; align-items:center; gap:10px; }
.section-divider span::after { content:''; flex:1; height:1px; background:var(--border); }

.harden-callout { margin:16px 22px; padding:14px 18px; background:rgba(139,123,223,0.06); border:1px solid var(--purple-border); border-radius:10px; }
.harden-callout h4 { font-size:12px; font-weight:600; color:var(--purple); font-family:var(--mono); margin-bottom:6px; }
.harden-callout p { font-size:12px; color:var(--text-muted); line-height:1.6; }

.step-detail { display:none; margin-top:10px; padding:12px 14px; padding-left:38px; background:var(--bg-raised); border-radius:8px; border:1px solid var(--border); }
.step.open .step-detail { display:block; }
.step-detail p { font-size:12px; color:var(--text-muted); line-height:1.7; margin-bottom:6px; }
.step-detail code { font-family:var(--mono); font-size:11px; background:rgba(255,255,255,0.05); padding:1px 5px; border-radius:3px; color:var(--text); }

.gen-timestamp { text-align:center; padding:24px 0 0; font-family:var(--mono); font-size:11px; color:var(--text-dim); }

@media (max-width:700px) {
  .parallel-row { flex-direction:column; }
  .parallel-box { min-width:unset; }
  .legend { gap:12px; }
  .phase-header { padding:14px 16px; }
  .step-list { padding:12px 16px; }
}"""

JS = """\
function togglePhase(id) {
  document.getElementById(id).classList.toggle('open');
}
function toggleStep(el) {
  el.classList.toggle('open');
  event.stopPropagation();
}
function toggleAll() {
  var btn = document.getElementById('expand-btn');
  var phases = document.querySelectorAll('.phase');
  var steps = document.querySelectorAll('.step');
  var expanding = btn.dataset.state !== 'expanded';
  phases.forEach(function(p) { expanding ? p.classList.add('open') : p.classList.remove('open'); });
  steps.forEach(function(s) { expanding ? s.classList.add('open') : s.classList.remove('open'); });
  btn.textContent = expanding ? 'Collapse all' : 'Expand all';
  btn.dataset.state = expanding ? 'expanded' : 'collapsed';
}"""

LEGEND_ITEMS = [
    ("blue", "writes"),
    ("teal", "reads"),
    ("coral", "gate"),
    ("red", "fatal failure"),
    ("amber", "non-fatal"),
    ("purple", "new (preference)"),
]

# Map color names to CSS variable patterns
COLOR_MAP = {
    "blue": {"bg": "var(--blue-bg)", "fg": "var(--blue)", "border": "var(--blue-border)"},
    "coral": {"bg": "var(--coral-bg)", "fg": "var(--coral)", "border": "var(--coral-border)"},
    "purple": {"bg": "var(--purple-bg)", "fg": "var(--purple)", "border": "var(--purple-border)"},
    "teal": {"bg": "var(--teal-bg)", "fg": "var(--teal)", "border": "var(--teal-border)"},
    "amber": {"bg": "var(--amber-bg)", "fg": "var(--amber)", "border": "var(--amber-border)"},
    "red": {"bg": "var(--red-bg)", "fg": "var(--red)", "border": "var(--red-border)"},
}
