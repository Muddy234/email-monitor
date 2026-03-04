# Email Monitor: Web UI + Dynamic Config + Scheduler Implementation Plan

## Overview

Convert the single-user, single-file email pipeline into a Flask web application with:
- Browser-based settings UI for all pipeline configuration
- SQLite database replacing config.json and processed_emails.json
- APScheduler (in-process) replacing Windows Task Scheduler
- Dynamic pipeline that reads all config from DB at runtime
- Pluggable output adapters (Obsidian, briefing email, etc.)
- Proper logging infrastructure throughout the pipeline

**Stack**: Python + Flask + SQLite + APScheduler + Bootstrap 5 (CDN)

---

## Phase 1: Database + Migration Script

Create the SQLite database schema and a one-time migration script to import existing JSON data. Includes a schema versioning system for future migrations.

### New files
- `email_monitor/database.py` — schema definitions, connection helpers, schema migration runner
- `email_monitor/models.py` — all data access functions (CRUD for config, filter rules, prompts, tracking, run history, schedule)
- `email_monitor/migrate_json.py` — one-time import of config.json + processed_emails.json into SQLite
- `email_monitor/migrations/` — directory for sequential SQL migration scripts

### Database schema (8 tables)

**`schema_version`** — tracks current database schema version for forward migrations
```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
    description TEXT
);
```

**WAL mode**: All connection helpers in `database.py` must explicitly enable WAL (Write-Ahead Logging) mode. SQLite defaults to rollback journal, which causes "database is locked" errors when the Flask web UI reads while the pipeline thread writes. WAL allows concurrent readers and one writer without blocking.

```python
# In every connection helper (get_db, get_db_standalone, init_db):
conn.execute("PRAGMA journal_mode=WAL")
```

WAL mode is persistent — once set on a database file, it sticks across connections. But executing the pragma on every connection is cheap (no-op if already WAL) and guarantees correctness even if someone recreates the file.

The initial migration (version 1) creates all tables below. Future schema changes are written as numbered SQL files in `migrations/` (e.g., `002_add_column.sql`) and applied automatically by a migration runner in `database.py`:

```python
def run_migrations(db: sqlite3.Connection, migrations_dir: str) -> None:
    """Apply any unapplied migration scripts in order."""
    current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    for migration_file in sorted(Path(migrations_dir).glob("*.sql")):
        version = int(migration_file.stem.split("_")[0])  # e.g., "002" -> 2
        if version > current:
            db.executescript(migration_file.read_text())
            db.execute("INSERT INTO schema_version (version, description) VALUES (?, ?)",
                       (version, migration_file.stem))
            db.commit()
```

**`config`** — key-value store for pipeline settings (replaces config.json scalars)
```sql
CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    value_type  TEXT NOT NULL DEFAULT 'string',  -- string, integer, boolean, float, json
    category    TEXT NOT NULL DEFAULT 'general',  -- general, outlook, analysis, drafts
    label       TEXT,
    description TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**`filter_rules`** — replaces the 4 filter arrays in config.json
```sql
CREATE TABLE IF NOT EXISTS filter_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type   TEXT NOT NULL,  -- blacklist_sender, blacklist_subject, whitelist_sender, whitelist_domain, project_keyword
    value       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(rule_type, value)
);
```

**`system_prompts`** — user-editable prompt templates
```sql
CREATE TABLE IF NOT EXISTS system_prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_key  TEXT NOT NULL UNIQUE,  -- analysis_system_prompt, draft_system_prompt
    content     TEXT NOT NULL,
    is_default  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**`processed_emails`** — replaces processed_ids array in JSON
```sql
CREATE TABLE IF NOT EXISTS processed_emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id        TEXT NOT NULL UNIQUE,
    run_id          INTEGER,  -- which pipeline run processed this email
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES run_history(id)
);
```

**`processed_conversations`** — replaces processed_conversations dict in JSON
```sql
CREATE TABLE IF NOT EXISTS processed_conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id     TEXT NOT NULL UNIQUE,
    topic               TEXT,
    latest_email_id     TEXT,
    email_count         INTEGER DEFAULT 1,
    last_seen           TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**`run_history`** — tracks each pipeline execution, including last run's action items for dashboard display
```sql
CREATE TABLE IF NOT EXISTS run_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',  -- running, completed, failed
    trigger_type        TEXT NOT NULL DEFAULT 'manual',   -- manual, scheduled
    emails_extracted    INTEGER DEFAULT 0,
    conversations_found INTEGER DEFAULT 0,
    important_count     INTEGER DEFAULT 0,
    skipped_count       INTEGER DEFAULT 0,
    action_items_count  INTEGER DEFAULT 0,
    drafts_saved        INTEGER DEFAULT 0,
    draft_errors        INTEGER DEFAULT 0,
    error_message       TEXT,
    log_output          TEXT,
    action_items_json   TEXT  -- JSON snapshot of action items produced (for dashboard preview)
);
```

**`schedule_config`** — single-row table for scheduler settings
```sql
CREATE TABLE IF NOT EXISTS schedule_config (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    enabled             INTEGER NOT NULL DEFAULT 0,
    schedule_type       TEXT NOT NULL DEFAULT 'interval',  -- interval, cron
    interval_minutes    INTEGER DEFAULT 60,
    cron_expression     TEXT,
    last_run_at         TEXT,
    next_run_at         TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
```

### Migration script logic (migrate_json.py)
1. Create all tables via `run_migrations()` (applies `001_initial_schema.sql`)
2. Read config.json, insert each scalar key into `config` table
3. Extract filter arrays from config.json, insert into `filter_rules` (one row per value)
4. Insert default system prompts from hardcoded constants
5. Read processed_emails.json, insert each ID into `processed_emails`
6. Insert each conversation into `processed_conversations`
7. Insert default `schedule_config` row (enabled=0, interval=60min)
8. Insert `schema_version` row (version=1)
9. All inserts use INSERT OR IGNORE for idempotency

### Verify
- Run migrate_json.py, query DB to confirm all data migrated
- Existing pipeline still works via config.json (no changes to email_monitor.py yet)

---

## Phase 2: Break email_monitor.py into Modules

Extract classes from the 1,250-line monolith into a `pipeline/` package. Introduce a pluggable output adapter system and proper logging. Keep `email_monitor.py` as a thin backward-compatible entry point.

### Target structure
```
email_monitor/
    pipeline/
        __init__.py
        logging_setup.py    # Centralized logging config with dual handler (console + buffer)
        outlook.py          # Outlook COM: connect, get_folder_by_name, get_emails
        filter.py           # EmailFilter class (classify method unchanged)
        analyzer.py         # ClaudeAnalyzer class + CLAUDE_JSON_SCHEMA
        drafts.py           # DraftGenerator class + save_draft_to_outlook
        prompts.py          # Default prompt constants + DB-loading helpers
        outputs/
            __init__.py     # OutputAdapter base class + registry
            obsidian.py     # ObsidianAdapter: format entries, save markdown, update action log
        runner.py           # PipelineRunner: orchestrates all phases (replaces EmailMonitor.run)
    email_monitor.py        # Thin wrapper: instantiates PipelineRunner, calls run()
```

### What moves where

| Source (email_monitor.py) | Destination | Content |
|---|---|---|
| Lines 27-47 (CLAUDE_SYSTEM_PROMPT) | pipeline/prompts.py | Default analysis prompt constant |
| Lines 50-102 (CLAUDE_JSON_SCHEMA) | pipeline/analyzer.py | JSON schema dict |
| Lines 105-153 (EmailFilter) | pipeline/filter.py | Class, unchanged logic |
| Lines 156-299 (ClaudeAnalyzer) | pipeline/analyzer.py | Class, accepts prompt as param |
| Lines 302-304 (_strip_internal_keys) | pipeline/runner.py | Helper near usage |
| Lines 307-322 (DRAFT_SYSTEM_PROMPT_TEMPLATE) | pipeline/prompts.py | Default draft template constant |
| Lines 325-451 (DraftGenerator) | pipeline/drafts.py | Class, accepts prompt as param |
| Lines 454-528 (EmailMonitor init/load/save/id) | pipeline/runner.py + models.py | Config loading becomes DB reads |
| Lines 530-579 (group_by_conversation) | pipeline/runner.py | Method on PipelineRunner |
| Lines 581-735 (Outlook COM methods) | pipeline/outlook.py | OutlookConnection class |
| Lines 737-915 (Obsidian methods) | pipeline/outputs/obsidian.py | ObsidianAdapter class |
| Lines 917-1232 (run method) | pipeline/runner.py | PipelineRunner.run() |
| Lines 1235-1249 (main) | email_monitor.py | Backward-compat entry point |

### Logging infrastructure (pipeline/logging_setup.py)

Replace all `print()` calls with Python's `logging` module. Use a dual-handler approach that captures everything — including third-party library output, warnings, and stderr — for storage in run_history.

```python
# pipeline/logging_setup.py

import logging
import io

class BufferHandler(logging.Handler):
    """Handler that writes to a StringIO buffer for later retrieval."""
    def __init__(self):
        super().__init__()
        self.buffer = io.StringIO()

    def emit(self, record):
        self.buffer.write(self.format(record) + "\n")

    def get_output(self) -> str:
        return self.buffer.getvalue()

    def clear(self):
        self.buffer = io.StringIO()

def setup_pipeline_logger(name: str = "email_monitor") -> tuple[logging.Logger, BufferHandler]:
    """Configure logger with console + buffer handlers. Returns (logger, buffer_handler)."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Console handler — same output the user sees today
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))

    # Buffer handler — captures everything for run_history.log_output
    buffer_handler = BufferHandler()
    buffer_handler.setLevel(logging.DEBUG)
    buffer_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    logger.addHandler(console)
    logger.addHandler(buffer_handler)

    return logger, buffer_handler
```

All pipeline modules accept a `logger` parameter. During Phase 2, convert `print()` calls to `logger.info()` / `logger.warning()` / `logger.error()` equivalents. The buffer handler captures everything — including subprocess stderr from Claude CLI calls, COM errors, and third-party warnings — which gets saved to `run_history.log_output`.

### Pluggable output adapters (pipeline/outputs/)

Instead of hardwiring Obsidian output, introduce an adapter interface. The runner calls all enabled adapters after each phase.

```python
# pipeline/outputs/__init__.py

from abc import ABC, abstractmethod

class OutputAdapter(ABC):
    """Base class for pipeline output adapters.

    Two patterns are supported:
    1. Incremental adapters (like Obsidian) override the phase-level hooks to
       write output as each phase completes.
    2. Batch adapters (like a future briefing email) override on_run_complete
       to compose a single coherent output from all results at once.

    All methods have default no-ops, so adapters only implement what they need.
    """

    def on_emails_extracted(self, emails: list[dict], config: dict) -> None:
        """Called after Phase 1 with raw extracted emails. Override for incremental output."""
        pass

    def on_analysis_complete(self, action_items: list[dict], config: dict) -> None:
        """Called after Phase 3 with analyzed action items. Override for incremental output."""
        pass

    def on_drafts_complete(self, draft_results: list[dict], config: dict) -> None:
        """Called after Phase 4 with draft generation results. Override for incremental output."""
        pass

    def on_run_complete(self, results: dict) -> None:
        """Called once at the end of the pipeline with all results combined.
        Override for adapters that need the full picture (e.g., briefing email).

        results dict contains:
            emails: list[dict]         - all extracted emails
            action_items: list[dict]   - all analyzed action items
            draft_results: list[dict]  - all draft generation outcomes
            config: dict               - pipeline config snapshot
            run_summary: dict          - counts, timing, status
        """
        pass
```

```python
# pipeline/outputs/obsidian.py — incremental adapter (uses phase hooks)

from pipeline.outputs import OutputAdapter

class ObsidianAdapter(OutputAdapter):
    """Writes raw emails and action logs to Obsidian vault as markdown."""

    def __init__(self, vault_path: str, raw_dir: str, action_dir: str):
        # same as current ObsidianWriter init

    def on_emails_extracted(self, emails, config):
        # current save_to_obsidian() logic — write raw markdown files

    def on_analysis_complete(self, action_items, config):
        # current update_action_log() logic — write action log markdown

    def on_drafts_complete(self, draft_results, config):
        # update action log entries with [DRAFT SAVED] tags
```

```python
# Example future adapter — batch adapter (uses on_run_complete)
# pipeline/outputs/briefing_email.py

class BriefingEmailAdapter(OutputAdapter):
    """Composes and sends a single morning briefing email from all results."""

    def on_run_complete(self, results):
        # Build one coherent email from results['action_items'],
        # results['draft_results'], and results['run_summary'].
        # Phase-level hooks are not overridden — this adapter doesn't
        # need incremental writes.
        pass
```

The runner loads output adapters from a config list and calls both patterns:

```python
# In config table, key='output_adapters', value='["obsidian"]', value_type='json'

# In PipelineRunner.run():
# Phase-level hooks called inline as each phase completes:
for adapter in self.output_adapters:
    adapter.on_emails_extracted(emails, self.config)

# ...after all phases finish:
for adapter in self.output_adapters:
    adapter.on_run_complete({
        "emails": all_emails,
        "action_items": all_action_items,
        "draft_results": all_draft_results,
        "config": self.config,
        "run_summary": self.get_summary_dict()
    })
```

When the commercial version adds a briefing-email output, it becomes a second adapter (`BriefingEmailAdapter`) added to the list — no changes to the runner or existing Obsidian code.

### Key design change
`PipelineRunner.__init__` accepts either `db_path` (new) or `config` dict (legacy fallback):
```python
class PipelineRunner:
    def __init__(self, db_path=None, config=None, run_id=None):
        if db_path:
            db = get_db_standalone(db_path)
            self.config = get_config(db)
            # load filter rules, prompts, tracking from DB
            db.close()
        elif config:
            self.config = config  # legacy JSON mode

        # Initialize output adapters from config
        self.output_adapters = self._load_output_adapters()
```

### Verify
- Run `run_email_workflow.bat` after refactor
- Output should be identical to before (same log lines, same files created)
- Obsidian files produced in the same format

---

## Phase 3: Flask Web Application

### New files
```
email_monitor/
    app.py              # Flask factory + APScheduler init + pipeline lock
    config.py           # Flask app config (SECRET_KEY, DATABASE path)
    routes/
        __init__.py     # Blueprint registration
        dashboard.py    # GET /
        settings.py     # GET/POST /settings
        filters.py      # GET/POST /filters, POST /filters/add, POST /filters/delete/<id>
        prompts.py      # GET/POST /prompts/analysis, POST /prompts/drafts, POST /prompts/reset/<key>
        schedule.py     # GET/POST /schedule, POST /schedule/run-now
        history.py      # GET /history, GET /history/<run_id>
    templates/
        base.html       # Bootstrap 5 layout, nav bar, flash messages
        dashboard.html  # Status overview, last run, next run, last run preview, quick links
        settings.html   # General settings form (grouped sections)
        filters.html    # Tabbed filter management (5 tabs)
        prompts.html    # Two large textareas + Save/Reset buttons
        schedule.html   # Enable toggle, interval/cron config, status, Run Now
        history.html    # Paginated run history table
        history_detail.html  # Single run detail view with log output
    static/
        style.css       # Minimal overrides
```

### Routes detail

**Dashboard** (`GET /`)
- Shows: scheduler status (on/off, next run time), last run summary (timestamp, counts, status), links to all settings pages
- **Last run preview**: Displays the action items from the most recent completed run (loaded from `run_history.action_items_json`). Shows a table with: action, from, project, priority, needs_response, draft status. This closes the loop so you can see what the pipeline actually did without checking your Obsidian vault.
- "Run Now" button (POST to /schedule/run-now)

**Settings** (`GET/POST /settings`)
- Form sections:
  - **Outlook**: outlook_folders (comma-separated text), process_flagged_only (checkbox), unflag_after_processing (checkbox), max_emails_per_run (number), max_emails_to_scan (number), start_date (date picker)
  - **Output**: vault_path (text), raw_email_dir (text), action_log_dir (text), output_adapters (multi-select or checkboxes for enabled adapters)
  - **Feature Toggles**: enable_email_filtering, enable_claude_analysis, enable_conversation_grouping, enable_draft_generation (all checkboxes)
  - **Analysis**: claude_cli_model (select: sonnet/opus/haiku), claude_cli_timeout_seconds (number), claude_cli_max_batch_size (number), claude_cli_max_budget_usd (number step=0.01), max_body_chars (number)
  - **Drafts**: draft_cli_model (select), draft_cli_timeout_seconds (number), draft_user_name (text), draft_user_title (text), filter_direct_recipient (email)
- POST saves each field to `config` table, flashes success message

**Filters** (`GET /filters`, `POST /filters/add`, `POST /filters/delete/<id>`)
- 5 tabs, one per rule_type:
  1. Blacklisted Senders
  2. Blacklisted Subjects
  3. Whitelisted Senders
  4. Whitelisted Domains
  5. Project Keywords
- Each tab: table listing current values with delete button per row, add form at bottom
- POST /filters/add: inserts row into filter_rules (form fields: rule_type + value)
- POST /filters/delete/<id>: deletes row from filter_rules

**Prompts** (`GET /prompts`, `POST /prompts/analysis`, `POST /prompts/drafts`, `POST /prompts/reset/<key>`)
- Two large textarea fields:
  1. **Analysis System Prompt** — the full CLAUDE_SYSTEM_PROMPT text. User can edit project names, urgency rules, needs_response criteria.
  2. **Draft System Prompt Template** — the DRAFT_SYSTEM_PROMPT_TEMPLATE text. Help text shows available placeholders: `{user_name}`, `{user_title}`.
- Save button per prompt, Reset to Default button per prompt

**Schedule** (`GET/POST /schedule`, `POST /schedule/run-now`)
- Enable/disable scheduler toggle
- Radio: "Every N minutes" (interval) or "Cron expression" (advanced)
- Number input for interval_minutes
- Text input for cron_expression (with examples: `0 8 * * 1-5` = weekdays 8am)
- Status display: last run time, next scheduled run, running indicator
- "Run Now" button
- POST saves to schedule_config, then calls configure_scheduler() to update the live scheduler immediately

**History** (`GET /history`, `GET /history/<run_id>`)
- Table: started_at, trigger_type, status, emails_extracted, action_items_count, drafts_saved, duration
- Click a row to view /history/<run_id> — shows full captured log_output and action items snapshot

### CSS approach
- Bootstrap 5 via CDN (no local install needed)
- One small style.css for overrides
- No JavaScript framework — standard HTML form POST
- Only JS: auto-refresh meta tag when a run is in progress

---

## Phase 4: Pipeline Reads from DB

Update all pipeline modules to load config from SQLite instead of config.json. Implement incremental batch-level tracking so partial failures don't require reprocessing.

### Changes per module

**pipeline/runner.py — PipelineRunner**
- `__init__` loads config via `models.get_config(db)`, filter rules, prompts, and tracking data from DB
- `run()` creates a `run_history` record at start, updates it with stats at end
- Tracking writes go to DB instead of JSON
- Uses `logging` module via `setup_pipeline_logger()` — buffer handler captures all output for `run_history.log_output`
- Saves `action_items_json` to run_history for dashboard preview
- Calls output adapters at appropriate points

**pipeline/filter.py — EmailFilter**
- `__init__` loads rules from `filter_rules` table via models.get_filter_rules()
- `classify()` method: unchanged logic

**pipeline/analyzer.py — ClaudeAnalyzer**
- `__init__` accepts `system_prompt` parameter (loaded from DB by runner)
- Analysis logic: unchanged

**pipeline/drafts.py — DraftGenerator**
- `__init__` accepts `system_prompt_template` parameter (loaded from DB by runner)
- Formats template with user_name/user_title from config
- Draft generation logic: unchanged

**pipeline/prompts.py**
- `get_analysis_prompt(db)` — loads from system_prompts table, falls back to hardcoded default
- `get_draft_prompt_template(db)` — same pattern

### Incremental batch-level tracking

The current pipeline updates `processed_emails.json` once at the end of `run()`. If the script crashes mid-run, no tracking is saved — but nothing is lost because the JSON was never updated. With the DB, we can be smarter.

**Strategy**: Mark emails as processed after each batch completes successfully, not at the end of the entire run. This means if Claude times out on batch 3 of 5, emails from batches 1-2 are already recorded as processed and won't be re-analyzed on the next run.

```python
# In PipelineRunner.run(), Phase 3 loop:
for batch_idx, batch in enumerate(batches):
    action_items = analyzer.analyze_batch(batch)
    if action_items:
        all_action_items.extend(action_items)
        # Mark this batch's emails as processed immediately
        batch_email_ids = [self._generate_email_id(e) for e in batch]
        db = get_db_standalone(self.db_path)
        mark_emails_processed(db, batch_email_ids, run_id=self.run_id)
        # Also update conversation tracking for this batch
        for email in batch:
            if email.get("conversation_id"):
                upsert_conversation(db, email["conversation_id"], ...)
        db.commit()
        db.close()
        logger.info(f"  Batch {batch_idx + 1}/{len(batches)}: {len(action_items)} items extracted, tracking saved")
    else:
        logger.warning(f"  Batch {batch_idx + 1}/{len(batches)}: analysis failed, emails will be retried next run")
```

Failed batches are simply not marked as processed. On the next run, those emails will be picked up again because their IDs aren't in `processed_emails`. No explicit retry logic needed — the existing dedup mechanism handles it naturally.

### Verify
- Run pipeline via CLI (`python email_monitor.py`), confirm it reads from DB
- Confirm tracking data written to DB tables (not JSON) after each batch
- Confirm run_history row created with correct stats and log_output populated
- Test partial failure: Set `claude_cli_max_batch_size` to 2 in the DB. Ensure 6+ emails are available to process (lower `start_date` temporarily if needed). Start the pipeline, watch logs until the first batch completes and its emails are committed to `processed_emails`. Then kill the process (Ctrl+C or terminate Flask). Restart and verify: (a) the first batch's emails are in `processed_emails`, (b) the remaining emails are picked up and processed on the next run, (c) no duplicates in the action log

---

## Phase 5: Scheduler Integration

Replace Windows Task Scheduler with APScheduler running inside the Flask process. Use a shared threading lock to prevent race conditions between scheduled runs and manual triggers.

### Pipeline execution lock (app.py)

```python
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# Shared lock prevents concurrent pipeline execution.
# Both the scheduler job and "Run Now" must acquire this before running.
# The Outlook COM interface is not thread-safe and will crash if two
# instances access it simultaneously.
pipeline_lock = threading.Lock()

scheduler = BackgroundScheduler(daemon=True)
PIPELINE_JOB_ID = 'email_pipeline'

def _run_pipeline_job(app, trigger_type='scheduled'):
    """Called by APScheduler or manual trigger. Acquires lock before running."""
    acquired = pipeline_lock.acquire(blocking=False)
    if not acquired:
        # Another run is already in progress — skip this one
        logger.warning("Pipeline run skipped: another run is already in progress")
        return

    runner = None  # Must be defined before try block — if __init__ throws,
                   # the except block references runner for log output.
    try:
        with app.app_context():
            db_path = app.config['DATABASE']
            db = get_db_standalone(db_path)
            run_id = create_run_record(db, trigger_type=trigger_type)
            db.commit()
            db.close()

            try:
                runner = PipelineRunner(db_path=db_path, run_id=run_id)
                runner.run()

                db = get_db_standalone(db_path)
                update_run_record(db, run_id, status='completed',
                                  finished_at=datetime.now().isoformat(),
                                  log_output=runner.get_log_output(),
                                  action_items_json=runner.get_action_items_json(),
                                  **runner.get_summary_dict())
                update_schedule_config(db, last_run_at=datetime.now().isoformat())
                db.commit()
                db.close()
            except Exception as e:
                db = get_db_standalone(db_path)
                update_run_record(db, run_id, status='failed',
                                  finished_at=datetime.now().isoformat(),
                                  error_message=str(e),
                                  log_output=runner.get_log_output() if runner else "")
                db.commit()
                db.close()
    finally:
        pipeline_lock.release()

def configure_scheduler(app):
    """Reads schedule_config from DB, adds/removes APScheduler job."""
    db = get_db_standalone(app.config['DATABASE'])
    sched = get_schedule_config(db)
    db.close()

    # Remove existing job if present
    if scheduler.get_job(PIPELINE_JOB_ID):
        scheduler.remove_job(PIPELINE_JOB_ID)

    if not sched or not sched['enabled']:
        return  # Scheduler disabled

    if sched['schedule_type'] == 'cron' and sched.get('cron_expression'):
        # Use APScheduler's built-in parser — handles validation, ranges,
        # step values (*/5), and rejects malformed expressions cleanly.
        try:
            trigger = CronTrigger.from_crontab(sched['cron_expression'])
        except ValueError as e:
            logger.error(f"Invalid cron expression '{sched['cron_expression']}': {e}")
            return  # Don't schedule with a bad expression
    else:
        trigger = IntervalTrigger(minutes=sched.get('interval_minutes', 60))

    scheduler.add_job(
        _run_pipeline_job,
        trigger=trigger,
        id=PIPELINE_JOB_ID,
        args=[app],
        replace_existing=True,
        max_instances=1,        # APScheduler-level guard (belt)
        misfire_grace_time=300  # 5 min grace for missed fires
    )
    # The pipeline_lock is the suspenders — it catches the race between
    # a scheduled fire and a manual "Run Now" click that max_instances can't.
```

The lock provides three guarantees:
1. A scheduled run and a manual run can never overlap (COM thread safety)
2. Two rapid "Run Now" clicks can't spawn two threads (the second gets rejected)
3. If a scheduled run fires while a manual run is active, it silently skips instead of queuing

### Schedule route POST handler
Validates cron expressions before saving, then calls `configure_scheduler(app)` to update the live scheduler immediately — no process restart needed.

```python
@schedule_bp.route('/schedule', methods=['POST'])
def update_schedule():
    # ...parse form fields...

    # Validate cron expression before saving
    if schedule_type == 'cron' and cron_expression:
        try:
            CronTrigger.from_crontab(cron_expression)  # parse to validate
        except ValueError as e:
            flash(f'Invalid cron expression: {e}', 'danger')
            return redirect(url_for('schedule_bp.show_schedule'))

    # Save to DB and reconfigure live scheduler
    update_schedule_config(db, ...)
    db.commit()
    configure_scheduler(current_app._get_current_object())
    flash('Schedule updated.', 'success')
    return redirect(url_for('schedule_bp.show_schedule'))
```

### Run Now endpoint
```python
@schedule_bp.route('/schedule/run-now', methods=['POST'])
def run_now():
    # Check the lock, not just run_history — this eliminates the race condition
    if pipeline_lock.locked():
        flash('A pipeline run is already in progress.', 'warning')
        return redirect(url_for('schedule_bp.show_schedule'))

    import threading
    thread = threading.Thread(
        target=_run_pipeline_job,
        args=[current_app._get_current_object(), 'manual'],
        daemon=True
    )
    thread.start()

    flash('Pipeline run started. Refresh to see progress.', 'info')
    return redirect(url_for('schedule_bp.show_schedule'))
```

### Status display
- Last run: from schedule_config.last_run_at
- Next run: from scheduler.get_job(PIPELINE_JOB_ID).next_run_time
- Running indicator: check `pipeline_lock.locked()` (more reliable than DB query)
- Auto-refresh: `<meta http-equiv="refresh" content="10">` when a run is active

### Verify
- Set interval to 2 minutes, confirm two automatic runs execute
- Change to 5 minutes, confirm next_run_time updates
- Click "Run Now", confirm manual run completes
- Click "Run Now" twice rapidly, confirm second click is rejected
- Click "Run Now" while a scheduled run is active, confirm it's rejected
- Check run_history for correct records

---

## Phase 6: Polish + Cutover

1. Add server-side form validation for all settings (type checking, range limits)
2. Flash messages for all save/delete/error operations
3. Confirmation for destructive actions (reset prompt to default, delete filter rule)
4. Create `run_webapp.bat` — starts Flask dev server (or waitress for production)
5. Remove the Windows Task Scheduler task
6. Update README.md with new setup/usage instructions
7. Keep config.json as dormant backup (do not delete)
8. Update requirements.txt: add `flask>=3.0`, `apscheduler>=3.10`

### SQLite backup

Add a scheduled backup of the database file. This protects against corruption from unexpected crashes during concurrent Flask + scheduler access (rare with WAL mode, but possible).

**Implementation**: Add a second APScheduler job that runs nightly:

```python
import shutil
from datetime import datetime

BACKUP_JOB_ID = 'db_backup'

def _backup_database(app):
    """Copy email_monitor.db to a timestamped backup file."""
    db_path = app.config['DATABASE']
    backup_dir = Path(db_path).parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"email_monitor_{timestamp}.db"

    # Use SQLite's backup API for a consistent copy (safe even during writes)
    import sqlite3
    source = sqlite3.connect(db_path)
    dest = sqlite3.connect(str(backup_path))
    source.backup(dest)
    dest.close()
    source.close()

    # Keep only the last 7 backups
    backups = sorted(backup_dir.glob("email_monitor_*.db"))
    for old_backup in backups[:-7]:
        old_backup.unlink()
```

This uses SQLite's built-in `connection.backup()` method, which produces a consistent snapshot even if the pipeline is writing at the same time. The backup job runs at 2 AM daily (configurable) and retains the last 7 copies.

The backup job is added in `configure_scheduler()` alongside the pipeline job:

```python
scheduler.add_job(
    _backup_database,
    trigger=CronTrigger(hour=2, minute=0),
    id=BACKUP_JOB_ID,
    args=[app],
    replace_existing=True
)
```

---

## Implementation Order + Dependencies

```
Phase 1 (DB + Migration + Schema Versioning)  <-- No risk to existing pipeline
    |
    v
Phase 2 (Module extraction + Logging + Output adapters)  <-- Pipeline still works via JSON fallback
    |
    v
Phase 3 (Flask web app + Dashboard preview)  <-- Can browse settings, save changes to DB
    |
    v
Phase 4 (Pipeline reads DB + Incremental tracking)  <-- DB becomes primary config source
    |
    v
Phase 5 (Scheduler + Pipeline lock)  <-- Replaces Windows Task Scheduler
    |
    v
Phase 6 (Polish + Backup + Cutover)  <-- Final cleanup
```

**Safety rule**: The existing pipeline via `run_email_workflow.bat` continues to work unchanged through Phases 1-3. Phase 4 is where the DB becomes the primary config source. The thin `email_monitor.py` wrapper in Phase 2 maintains backward compatibility.

---

## Files Created/Modified Summary

| File | Action | Phase |
|---|---|---|
| email_monitor/database.py | CREATE | 1 |
| email_monitor/models.py | CREATE | 1 |
| email_monitor/migrate_json.py | CREATE | 1 |
| email_monitor/migrations/001_initial_schema.sql | CREATE | 1 |
| email_monitor/pipeline/__init__.py | CREATE | 2 |
| email_monitor/pipeline/logging_setup.py | CREATE | 2 |
| email_monitor/pipeline/outlook.py | CREATE | 2 |
| email_monitor/pipeline/filter.py | CREATE | 2 |
| email_monitor/pipeline/analyzer.py | CREATE | 2 |
| email_monitor/pipeline/drafts.py | CREATE | 2 |
| email_monitor/pipeline/prompts.py | CREATE | 2 |
| email_monitor/pipeline/outputs/__init__.py | CREATE | 2 |
| email_monitor/pipeline/outputs/obsidian.py | CREATE | 2 |
| email_monitor/pipeline/runner.py | CREATE | 2 |
| email_monitor/email_monitor.py | MODIFY (thin wrapper) | 2 |
| email_monitor/app.py | CREATE | 3 |
| email_monitor/config.py | CREATE | 3 |
| email_monitor/routes/__init__.py | CREATE | 3 |
| email_monitor/routes/dashboard.py | CREATE | 3 |
| email_monitor/routes/settings.py | CREATE | 3 |
| email_monitor/routes/filters.py | CREATE | 3 |
| email_monitor/routes/prompts.py | CREATE | 3 |
| email_monitor/routes/schedule.py | CREATE | 3 |
| email_monitor/routes/history.py | CREATE | 3 |
| email_monitor/templates/base.html | CREATE | 3 |
| email_monitor/templates/dashboard.html | CREATE | 3 |
| email_monitor/templates/settings.html | CREATE | 3 |
| email_monitor/templates/filters.html | CREATE | 3 |
| email_monitor/templates/prompts.html | CREATE | 3 |
| email_monitor/templates/schedule.html | CREATE | 3 |
| email_monitor/templates/history.html | CREATE | 3 |
| email_monitor/templates/history_detail.html | CREATE | 3 |
| email_monitor/static/style.css | CREATE | 3 |
| email_monitor/requirements.txt | MODIFY | 3 |
| email_monitor/run_webapp.bat | CREATE | 6 |
| email_monitor/README.md | MODIFY | 6 |

---

## Verification Plan

After each phase:
1. **Phase 1**: Run migrate_json.py. Query DB to confirm all config values, filter rules, processed emails, and conversations migrated. Confirm schema_version table has version=1. Run existing pipeline via batch file — still works unchanged.
2. **Phase 2**: Run `python email_monitor.py` from the project directory. Output should be identical to pre-refactor. Obsidian files produced in the same format. Confirm logger output matches previous print output.
3. **Phase 3**: Run `flask run`, browse http://localhost:5000. All pages render with current config data. Change a setting, confirm DB updated. Add/delete a filter rule, confirm DB updated. Dashboard shows "no runs yet" initially.
4. **Phase 4**: Run pipeline via CLI. Confirm it reads from DB (not JSON). Check run_history table for new row with log_output and action_items_json populated. Check processed_emails table for new entries. Test partial failure: break Claude CLI after first batch, confirm first batch's emails are tracked and second batch's are not.
5. **Phase 5**: Enable scheduler with 2-min interval. Watch two runs complete. Click "Run Now". Click "Run Now" twice rapidly — second should be rejected. Click "Run Now" during a scheduled run — should be rejected. Check run_history for correct records. Confirm dashboard shows last run's action items preview.
6. **Phase 6**: Full end-to-end: start webapp, configure schedule, let it run automatically, review results in history and dashboard preview, edit a filter rule, trigger manual run, confirm filter change took effect. Check backups/ directory for nightly .db copy.

---

## Updated requirements.txt

```
pywin32>=305
python-dateutil>=2.8.2
flask>=3.0
apscheduler>=3.10
```

No ORM needed (raw sqlite3 is sufficient at this scale). No additional CSS/JS framework needed (Bootstrap 5 via CDN). No auth library needed (single user for now).
