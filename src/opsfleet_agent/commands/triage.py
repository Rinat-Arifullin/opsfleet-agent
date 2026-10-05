"""Feedback triage CLI for maintainers (iteration 36; FR-47, AC-25.4, R4.2; HLD §6.4).

Run it next to the chat, never inside it::

    python -m opsfleet_agent.commands.triage --as support_demo list --state new
    python -m opsfleet_agent.commands.triage --as support_demo show <feedback-id>
    python -m opsfleet_agent.commands.triage --as support_demo classify <feedback-id>
    python -m opsfleet_agent.commands.triage --as support_demo dismiss <id> --reason duplicate
    python -m opsfleet_agent.commands.triage --as support_demo add-eval <id> --question "..."
    python -m opsfleet_agent.commands.triage --as support_demo promote <id> --question "..." \\
        --summary "..." [--sql-file query.sql] [--brands agnostic]

Contract (enforced here in code, not in a prompt):

* **Maintainers only (D-216).** ``--as <id>`` must be listed in ``config/maintainers.yaml``
  (``OPSFLEET_MAINTAINERS_YAML`` overrides the location). Anyone else is refused with exit 2
  before any feedback row or trace is read. A data scope (``all_products``) is not this role.
* **Root cause from the trace (D-217).** :func:`root_cause` maps one turn's spans to a class
  with ordered, deterministic rules; the class is computed on read and never stored.
* **Audit first (D-218).** Every state change writes its audit row (ids, enums and codes only)
  before the change; if that write fails, nothing changes. The state change is a
  compare-and-set, so a concurrent maintainer cannot double-apply it. A refusal is audited
  best-effort with ``outcome=refused`` and the failing ``gate``.
* **No raw PII on screen or on disk (D-219).** Only the stored, already-scrubbed comment is
  shown (scrubbed again here). The question for ``add-eval`` and ``promote`` comes from the
  maintainer, is regex-scrubbed, then scanned by the PII detector; anything left is refused.
* **Gated promote (D-220).** A Golden *candidate* is written only after the seed validator
  (SQL policy, scope, PII, injection, figures, brands), the PII detector, a BigQuery dry run
  and a green offline eval run all pass, in that order. Any failing gate leaves the state and
  the files unchanged. The candidate goes to ``<data dir>/golden_candidates/`` for human
  review; this tool never edits ``config/golden_seed.yaml``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TextIO

import yaml

from opsfleet_agent.guards.pii_regex import scrub
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.obs.metrics import load_spans
from opsfleet_agent.session import default_data_dir, default_profiles_path, load_profiles
from opsfleet_agent.store.audit import ACTOR_ID_RE, AuditError, AuditLog
from opsfleet_agent.store.feedback import (
    DISMISS_REASONS,
    ROOT_CAUSES,
    TRIAGE_STATES,
    FeedbackRecord,
    FeedbackStore,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
MAINTAINERS_ENV: Final = "OPSFLEET_MAINTAINERS_YAML"
CASES_DIR: Final = REPO_ROOT / "evals" / "cases" / "regression"
CANDIDATES_DIR: Final = "golden_candidates"
SLOW_TURN_MS: Final = 30_000
MAX_SPANS: Final = 2_000
MAX_QUESTION: Final = 500
MAX_SUMMARY: Final = 1_000
MAX_SQL_FILE_BYTES: Final = 64_000
EVAL_TIMEOUT_S: Final = 900
EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2

_ID_RE: Final = re.compile(r"[0-9a-f]{8,32}")
_TRIO_ID_RE: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_BLOCKING_GUARDS: Final = frozenset({"input", "router", "output", "customer_id", "echo"})
_NON_ANALYTIC_ROUTES: Final = frozenset({"light", "comment", "refuse"})
# The same markers the golden eval cases forbid in an answer (no customer columns, no e-mail).
_PII_MARKERS: Final = ("@", "first_name", "last_name", "street_address", "email")

PiiScan = Callable[[str], Sequence[str]]  # text -> entity types found (never the values)
DryRun = Callable[[str, Sequence[str]], str | None]  # (sql, brands) -> None or an error code
EvalGate = Callable[[], bool]  # True when the offline eval run is green

REFUSED_ROLE = "Refused: triage is for maintainers only. Pass --as <maintainer id>."


class TriageRefused(Exception):
    """A gate or a precondition failed; the message is safe to print."""

    def __init__(self, message: str, gate: str | None = None) -> None:
        super().__init__(message)
        self.gate = gate


# --- access ----------------------------------------------------------------------------------


def default_maintainers_path() -> Path:
    override = os.environ.get(MAINTAINERS_ENV)  # a file location, never a list of ids
    return Path(override) if override else REPO_ROOT / "config" / "maintainers.yaml"


def load_maintainers(path: Path | None = None) -> frozenset[str]:
    """Allowed maintainer ids. Fails closed: an unreadable or malformed file allows no one."""
    try:
        raw = yaml.safe_load((path or default_maintainers_path()).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return frozenset()
    ids = raw.get("maintainers") if isinstance(raw, Mapping) else None
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(i for i in ids if isinstance(i, str) and ACTOR_ID_RE.fullmatch(i))


# --- root cause (D-217) ----------------------------------------------------------------------


def turn_spans(trace_dir: Path, record: FeedbackRecord) -> list[dict[str, Any]]:
    """The spans of the rated turn (bounded), read from the session's local trace file."""
    try:
        spans = load_spans(trace_dir, record.session_id).spans
    except (OSError, ValueError):
        return []
    return [s for s in spans if s.get("turn_id") == record.turn_id][:MAX_SPANS]


def _is_error(span: Mapping[str, Any]) -> bool:
    return span.get("status") == "error"


def root_cause(record: FeedbackRecord, spans: Sequence[Mapping[str, Any]]) -> tuple[str, list[str]]:
    """Ordered root-cause rules over one turn's spans. Returns (class, signals).

    First match wins: no_trace, guardrail_block, sql_error, model_down, verifier_fail,
    empty_result, misroute, slow, then intent_or_format for a down rating with a clean trace
    or ``clean`` for an up rating. Signals are span names and enum values only.
    """
    if not spans:
        return "no_trace", []
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for s in spans:
        by_type.setdefault(str(s.get("type")), []).append(s)
    guards, sqls = by_type.get("guard", []), by_type.get("sql", [])

    hits = [
        f"guard:{g.get('name')}:{g.get('verdict')}"
        for g in guards
        if g.get("name") in _BLOCKING_GUARDS and g.get("verdict") in ("block", "refuse")
    ]
    if hits:
        return "guardrail_block", hits

    hits = [
        f"sql:{s.get('error_class') or s.get('rule') or 'error'}"
        for s in sqls
        if _is_error(s) or s.get("policy_verdict") == "refuse"
    ]
    hits += [
        f"tool:run_sql:{t.get('error_code')}"
        for t in by_type.get("tool", [])
        if "run_sql" in (t.get("tool"), t.get("name")) and t.get("error_code")
    ]
    hits += [
        f"role:{r.get('name')}:give_up"
        for r in by_type.get("role", [])
        if r.get("error_class") == "give_up"
    ]
    if hits:
        return "sql_error", hits

    hits = [
        f"llm:{s.get('name')}:{'error' if _is_error(s) else 'fallback'}"
        for s in by_type.get("llm", [])
        if _is_error(s) or s.get("fallback_used")
    ]
    if hits:
        return "model_down", hits

    hits = [
        f"guard:report:{g.get('verdict')}"
        for g in guards
        if g.get("name") == "report" and g.get("verdict") in ("block", "no_draft")
    ]
    hits += [
        f"guard:grounding:{g.get('grounding_flags')}"
        for g in guards
        if g.get("name") == "grounding" and (g.get("grounding_flags") or 0) > 0
    ]
    if hits:
        return "verifier_fail", hits

    ok_rows = [s.get("rows") for s in sqls if not _is_error(s)]
    if ok_rows and all(r == 0 for r in ok_rows):
        return "empty_result", [f"sql:rows=0 x{len(ok_rows)}"]

    if record.rating == "down":
        routes = [str(r.get("route")) for r in by_type.get("router", []) if r.get("route")]
        if record.reason == "misunderstood" or any(r in _NON_ANALYTIC_ROUTES for r in routes):
            return "misroute", [f"router:{r}" for r in routes] or ["reason:misunderstood"]

    slow = [
        int(t.get("duration_ms") or 0)
        for t in by_type.get("turn", [])
        if int(t.get("duration_ms") or 0) > SLOW_TURN_MS
    ]
    if record.reason == "slow" or slow:
        return "slow", [f"turn:duration_ms={d}" for d in slow] or ["reason:slow"]

    return ("clean" if record.rating == "up" else "intent_or_format"), []


# --- helpers ---------------------------------------------------------------------------------


def _safe(text: str | None, limit: int = 500) -> str:
    """Display form of stored text: secrets and length bound, then the PII regex again."""
    if not text:
        return "-"
    return scrub(tr.scrub_text(text, limit)).text


def _resolve(store: FeedbackStore, ident: str) -> FeedbackRecord:
    if not _ID_RE.fullmatch(ident or ""):
        raise TriageRefused("Feedback id must be 8-32 lowercase hex characters.")
    rows = store.conn.execute(
        "SELECT feedback_id FROM feedback WHERE substr(feedback_id, 1, ?) = ? LIMIT 2",
        (len(ident), ident),
    ).fetchall()
    if len(rows) != 1:
        raise TriageRefused("No such feedback item." if not rows else "Ambiguous feedback id.")
    rec = store.get(rows[0][0])
    assert rec is not None
    return rec


def _clean_question(text: str, pii_scan: PiiScan) -> str:
    """Regex-scrub, then refuse if the detector still finds PII (fail closed on errors)."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_QUESTION:
        raise TriageRefused(f"The question must be 1-{MAX_QUESTION} characters.", "pii")
    cleaned = scrub(tr.scrub_text(text.strip(), MAX_QUESTION)).text
    try:
        found = list(pii_scan(cleaned))
    except Exception:  # noqa: BLE001 - a detector failure is a refusal, never a pass
        raise TriageRefused("Refused: the PII scan could not run.", "pii") from None
    if found:
        types = ", ".join(sorted(set(map(str, found))))
        raise TriageRefused(f"Refused: personal data remains after scrubbing ({types}).", "pii")
    return cleaned


def _profile(user_id: str, profiles_path: Path | None) -> Any | None:
    try:
        return load_profiles(profiles_path or default_profiles_path()).get(user_id)
    except Exception:  # noqa: BLE001 - optional: a case without a profile is still valid
        return None


# --- default gates (production wiring; tests inject fakes) -----------------------------------


def default_pii_scan(text: str) -> list[str]:  # pragma: no cover - loads the NER model
    from opsfleet_agent.guards.pii import default_detector

    return [f.type for f in default_detector().detect(text)]


def default_dry_run(sql: str, brands: Sequence[str]) -> str | None:  # pragma: no cover - BQ
    """Scope the SQL like run_sql does and dry-run it with the byte caps. None when it passes."""
    from opsfleet_agent.bq.client import BigQueryRunner, SessionByteBudget, make_bigquery_client
    from opsfleet_agent.bq.errors import BqFailure
    from opsfleet_agent.golden.seed import AGNOSTIC
    from opsfleet_agent.guards.scope import ProductScope, ScopedQuery, apply_scope
    from opsfleet_agent.tools import run_sql

    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        return "no_project"
    real = [b for b in brands if b != AGNOSTIC]
    scope = ProductScope.for_brands(real) if real else ProductScope.all()
    scoped = apply_scope(sql, scope)
    if not isinstance(scoped, ScopedQuery):
        return "scope"
    runner = BigQueryRunner(
        make_bigquery_client(project), project,
        job_config_factory=run_sql.scoped_job_config_factory(),
    )  # fmt: skip
    with run_sql._parameters(scoped):
        res = runner.prepare(scoped.sql, SessionByteBudget())
    return res.code.value if isinstance(res, BqFailure) else None


def default_eval_gate() -> bool:  # pragma: no cover - runs the offline eval suite
    cmd = [sys.executable, str(REPO_ROOT / "evals" / "run.py"), "--offline", "--yes"]
    try:
        done = subprocess.run(
            [*cmd, "--suite", "golden"], cwd=REPO_ROOT, capture_output=True,
            timeout=EVAL_TIMEOUT_S, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# --- the commands ----------------------------------------------------------------------------


class Triage:
    def __init__(
        self,
        conn: sqlite3.Connection,
        actor: str,
        *,
        trace_dir: Path,
        data_dir: Path,
        cases_dir: Path = CASES_DIR,
        profiles_path: Path | None = None,
        pii_scan: PiiScan = default_pii_scan,
        dry_run: DryRun = default_dry_run,
        eval_gate: EvalGate = default_eval_gate,
        out: TextIO,
    ) -> None:
        self.store = FeedbackStore(conn)
        self.audit = AuditLog(conn)
        self.actor = actor
        self.trace_dir, self.data_dir, self.cases_dir = trace_dir, data_dir, cases_dir
        self.profiles_path = profiles_path
        self.pii_scan, self.dry_run, self.eval_gate = pii_scan, dry_run, eval_gate
        self.out = out

    def _say(self, text: str) -> None:
        print(text, file=self.out)

    def cause(self, rec: FeedbackRecord) -> tuple[str, list[str]]:
        return root_cause(rec, turn_spans(self.trace_dir, rec))

    # audit -----------------------------------------------------------------------------------

    def _audit(self, event: str, rec: FeedbackRecord, outcome: str, **details: Any) -> None:
        row = self.audit.record(
            event,
            actor_user_id=self.actor,
            session_id=rec.session_id,
            turn_id=rec.turn_id,
            target_ids=[rec.feedback_id],
            outcome=outcome,
            details=details,
        )
        if row is None:  # ignored as a duplicate: no row was written, so no change either
            raise AuditError("audit row not written")

    def _audit_best_effort(self, event: str, rec: FeedbackRecord, outcome: str, **kw: Any) -> None:
        try:
            self._audit(event, rec, outcome, **kw)
        except AuditError:
            self._say("Warning: the refusal could not be audited.")

    def _change(
        self,
        event: str,
        rec: FeedbackRecord,
        to: str,
        expected: tuple[str, ...],
        write: Callable[[], Path | None] | None = None,
        **details: Any,
    ) -> Path | None:
        """Audit first, then the optional file write, then the compare-and-set state change."""
        if rec.triage_state not in expected:
            raise TriageRefused(f"Refused: the item is already {rec.triage_state}.", "eligibility")
        try:
            self._audit(event, rec, "ok", triage_state=to, **details)
        except AuditError:
            raise TriageRefused("Aborted: the audit write failed; nothing was changed.") from None
        written: Path | None = None
        try:
            if write is not None:
                written = write()
            if not self.store.set_state(rec.feedback_id, to, expected=expected):
                raise TriageRefused("Aborted: the item changed state meanwhile.", "write")
        except BaseException as exc:
            if written is not None:
                written.unlink(missing_ok=True)
            self._audit_best_effort(event, rec, "failed", gate="write", **details)
            if isinstance(exc, TriageRefused):
                raise
            if isinstance(exc, OSError):
                raise TriageRefused("Aborted: the file could not be written.", "write") from None
            raise
        return written

    # read commands ---------------------------------------------------------------------------

    def list(self, state: str | None, cause: str | None, rating: str | None, limit: int) -> int:
        rows = self.store.list_items(state=state, rating=rating, limit=limit)
        shown = 0
        for rec in rows:
            cls, _ = self.cause(rec)
            if cause is not None and cls != cause:
                continue
            shown += 1
            self._say(
                f"{rec.feedback_id}  {rec.triage_state:<9} {rec.rating:<4} "
                f"{rec.reason or '-':<14} {cls:<16} {rec.created_at}"
            )
        self._say(f"{shown} item(s).")
        return EXIT_OK

    def show(self, ident: str) -> int:
        rec = _resolve(self.store, ident)
        cls, signals = self.cause(rec)
        for label, value in (
            ("id", rec.feedback_id),
            ("state", rec.triage_state),
            ("rating", rec.rating),
            ("reason", rec.reason or "-"),
            ("user", rec.user_id),
            ("session", rec.session_id),
            ("turn", rec.turn_id),
            ("trace", _safe(rec.trace_id, 80)),
            ("created", rec.created_at),
            ("root cause", cls),
            ("signals", ", ".join(signals) or "-"),
            ("comment", _safe(rec.comment)),
        ):
            self._say(f"{label:<11} {value}")
        self._say(f"Full trace: /trace {rec.turn_id} in that session, or the trace file.")
        return EXIT_OK

    # state changes ---------------------------------------------------------------------------

    def classify(self, ident: str) -> int:
        rec = _resolve(self.store, ident)
        cls, _ = self.cause(rec)
        try:
            self._change("feedback.triaged", rec, "triaged", ("new",), root_cause=cls)
        except TriageRefused as exc:
            if exc.gate == "eligibility":
                self._audit_best_effort(
                    "feedback.triaged", rec, "refused", gate="eligibility", root_cause=cls
                )
            raise
        self._say(f"Triaged {rec.feedback_id}: root cause {cls}.")
        return EXIT_OK

    def dismiss(self, ident: str, reason: str) -> int:
        rec = _resolve(self.store, ident)
        if reason not in DISMISS_REASONS:
            raise TriageRefused(f"--reason must be one of: {', '.join(DISMISS_REASONS)}.")
        self._change(
            "feedback.dismissed", rec, "dismissed", ("new", "triaged"), dismiss_reason=reason
        )
        self._say(f"Dismissed {rec.feedback_id} ({reason}).")
        return EXIT_OK

    def add_eval(self, ident: str, question: str, expect_refusal: bool) -> int:
        rec = _resolve(self.store, ident)
        cls, _ = self.cause(rec)
        try:
            text = _clean_question(question, self.pii_scan)
        except TriageRefused as exc:
            self._audit_best_effort(
                "eval.case_added", rec, "refused", gate=exc.gate, root_cause=cls
            )
            raise
        path = self.cases_dir / f"triage_{rec.feedback_id[:12]}.yaml"
        case = build_case(text, cls, _profile(rec.user_id, self.profiles_path), expect_refusal)

        def write() -> Path:
            self.cases_dir.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as fh:  # never overwrite a reviewed case
                fh.write(f"# Triage draft from feedback {rec.feedback_id} (iteration 36).\n")
                fh.write("# Expectations are derived from the root cause: review them, record\n")
                fh.write("# a `fake` block, then remove `skip`.\n")
                yaml.safe_dump(case, fh, sort_keys=False, allow_unicode=True)
            return path

        self._change(
            "eval.case_added", rec, "triaged", ("new", "triaged"), write=write, root_cause=cls
        )
        self._say(f"Wrote eval case {path} (root cause {cls}); review it before enabling.")
        return EXIT_OK

    def promote(
        self,
        ident: str,
        *,
        question: str,
        summary: str,
        sql_file: Path | None,
        brands: Sequence[str] | None,
        trio_id: str | None,
    ) -> int:
        rec = _resolve(self.store, ident)
        cls, _ = self.cause(rec)
        try:
            entry = self._promote_gates(rec, cls, question, summary, sql_file, brands, trio_id)
        except TriageRefused as exc:
            self._audit_best_effort(
                "golden.promoted", rec, "refused", gate=exc.gate or "eligibility", root_cause=cls
            )
            raise
        out_dir = self.data_dir / CANDIDATES_DIR
        path = out_dir / f"{entry['trio_id']}.yaml"

        def write() -> Path:
            out_dir.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as fh:
                fh.write(f"# Golden candidate from feedback {rec.feedback_id} (iteration 36).\n")
                fh.write("# Passed: seed validator, PII scan, BigQuery dry run, offline evals.\n")
                fh.write("# A human reviews it and copies the trio into config/golden_seed.yaml.\n")
                yaml.safe_dump({"version": 1, "trios": [entry]}, fh, sort_keys=False)
            return path

        sql_hash = hashlib.sha256(entry["sql"].encode("utf-8")).hexdigest()
        self._change(
            "golden.promoted", rec, "promoted", ("new", "triaged"), write=write,
            root_cause=cls, sql_hash=sql_hash,
        )  # fmt: skip
        self._say(f"Golden candidate written to {path}; review it before adding it to the seed.")
        return EXIT_OK

    def _promote_gates(
        self,
        rec: FeedbackRecord,
        cls: str,
        question: str,
        summary: str,
        sql_file: Path | None,
        brands: Sequence[str] | None,
        trio_id: str | None,
    ) -> dict[str, Any]:
        from opsfleet_agent.golden.seed import AGNOSTIC, _known_brands, _validate

        if rec.triage_state not in ("new", "triaged"):
            raise TriageRefused(f"Refused: the item is already {rec.triage_state}.", "eligibility")
        if rec.rating != "up" or cls != "clean":
            raise TriageRefused(
                f"Refused: only an up-rated turn with a clean trace can be promoted "
                f"(rating {rec.rating}, root cause {cls}).",
                "eligibility",
            )
        sql = self._promote_sql(rec, sql_file)
        tid = trio_id or f"triage-{rec.feedback_id[:12]}"
        if not _TRIO_ID_RE.fullmatch(tid):
            raise TriageRefused("--trio-id must match [A-Za-z0-9_.-]{1,64}.", "validate")
        if not brands:
            prof = _profile(rec.user_id, self.profiles_path)
            if prof is None:
                raise TriageRefused("Unknown profile: pass --brands.", "validate")
            brands = [AGNOSTIC] if prof.all_products else list(prof.brands)
        q = (question or "").strip()
        s = (summary or "").strip()
        if not q or not s or len(q) > MAX_QUESTION or len(s) > MAX_SUMMARY:
            raise TriageRefused("--question and --summary are required and bounded.", "validate")
        entry: dict[str, Any] = {
            "trio_id": tid,
            "version": 1,
            "brands": list(brands),
            "tags": ["triage", "promoted"],
            "question": q,
            "sql": sql,
            "report_summary": s,
        }
        # Gate 1: the seed validator (SQL policy and scope, regex PII, injection, figures, brands).
        trio, why = _validate(entry, _known_brands([entry]))
        if trio is None:
            gate = "pii" if why == "pii" or why.startswith("injection") else "validate"
            raise TriageRefused(f"Refused by the seed validator ({why}).", gate)
        entry["sql"] = trio.sql  # the canonical SQL, comments dropped
        # Gate 2: the PII detector on the prose (the regex pass above is necessary, not enough).
        try:
            found = [t for text in (q, s) for t in self.pii_scan(text)]
        except Exception:  # noqa: BLE001 - fail closed
            raise TriageRefused("Refused: the PII scan could not run.", "pii") from None
        if found:
            raise TriageRefused(
                f"Refused: personal data found ({', '.join(sorted(set(found)))}).", "pii"
            )
        # Gate 3: the BigQuery dry run (scope applied, byte caps).
        try:
            err = self.dry_run(trio.sql, list(brands))
        except Exception:  # noqa: BLE001 - fail closed
            err = "dry_run_error"
        if err is not None:
            raise TriageRefused(f"Refused: the dry run failed ({err}).", "dry_run")
        # Gate 4: the offline eval suite must be green.
        try:
            green = bool(self.eval_gate())
        except Exception:  # noqa: BLE001 - fail closed
            green = False
        if not green:
            raise TriageRefused("Refused: the offline eval run is not green.", "eval")
        return entry

    def _promote_sql(self, rec: FeedbackRecord, sql_file: Path | None) -> str:
        if sql_file is not None:
            try:
                data = sql_file.read_bytes()[: MAX_SQL_FILE_BYTES + 1]
            except OSError:
                raise TriageRefused("Cannot read --sql-file.", "sql_source") from None
            if len(data) > MAX_SQL_FILE_BYTES:
                raise TriageRefused("--sql-file is too large.", "sql_source")
            sql = data.decode("utf-8", errors="replace").strip()
        else:
            ok = [
                s.get("sql_text")
                for s in turn_spans(self.trace_dir, rec)
                if s.get("type") == "sql" and not _is_error(s) and s.get("sql_text")
            ]
            sql = str(ok[-1]).strip() if ok else ""
            if not sql:
                raise TriageRefused(
                    "No successful SQL in the trace: pass --sql-file.", "sql_source"
                )
        if "?" in sql:
            # Traces keep SQL with every literal replaced by `?` (tracer.sanitize_sql).
            raise TriageRefused(
                "The trace SQL has redacted literals (?): pass the reviewed SQL with --sql-file.",
                "sql_source",
            )
        return sql


def build_case(
    question: str, cause: str, profile: Any | None, expect_refusal: bool
) -> dict[str, Any]:
    """A regression eval case drafted from one feedback item (D-219)."""
    expect: dict[str, Any] = {"outcome": "refused" if expect_refusal else "answered"}
    if expect_refusal:
        expect["no_sql"] = True
    expect["must_not_contain"] = list(_PII_MARKERS)
    case: dict[str, Any] = {"input": question, "tags": ["regression", "triage", cause]}
    if profile is not None:
        case["session"] = {"profile": profile.user_id}
    case["expect"] = expect
    case["skip"] = f"triage draft ({cause}): review the expectations and record a fake"
    return case


# --- entry point -----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m opsfleet_agent.commands.triage",
        description="Feedback triage for maintainers (FR-47).",
    )
    ap.add_argument("--as", dest="actor", required=True, help="maintainer id (maintainers.yaml)")
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument("--trace-dir", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--state", choices=TRIAGE_STATES)
    p.add_argument("--class", dest="cause", choices=ROOT_CAUSES)
    p.add_argument("--rating", choices=("up", "down"))
    p.add_argument("--limit", type=int, default=50)
    for name in ("show", "classify"):
        sub.add_parser(name).add_argument("id")
    p = sub.add_parser("dismiss")
    p.add_argument("id")
    p.add_argument("--reason", required=True, choices=DISMISS_REASONS)
    p = sub.add_parser("add-eval")
    p.add_argument("id")
    p.add_argument("--question", required=True)
    p.add_argument("--expect-refusal", action="store_true")
    p.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    p = sub.add_parser("promote")
    p.add_argument("id")
    p.add_argument("--question", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--sql-file", type=Path)
    p.add_argument("--brands", nargs="+")
    p.add_argument("--trio-id")
    return ap


def main(
    argv: Sequence[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    maintainers: frozenset[str] | None = None,
    profiles_path: Path | None = None,
    pii_scan: PiiScan | None = None,
    dry_run: DryRun | None = None,
    eval_gate: EvalGate | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    allowed = load_maintainers() if maintainers is None else maintainers
    if not ACTOR_ID_RE.fullmatch(args.actor or "") or args.actor not in allowed:
        print(REFUSED_ROLE, file=out)
        return EXIT_REFUSED
    data_dir = args.data_dir or default_data_dir()
    own = conn is None
    if conn is None:
        from opsfleet_agent.store.db import open_store

        conn = open_store(data_dir / "app.db")
    triage = Triage(
        conn,
        args.actor,
        trace_dir=args.trace_dir or data_dir / "traces",
        data_dir=data_dir,
        cases_dir=getattr(args, "cases_dir", CASES_DIR),
        profiles_path=profiles_path,
        pii_scan=pii_scan or default_pii_scan,
        dry_run=dry_run or default_dry_run,
        eval_gate=eval_gate or default_eval_gate,
        out=out,
    )
    try:
        if args.cmd == "list":
            return triage.list(args.state, args.cause, args.rating, args.limit)
        if args.cmd == "show":
            return triage.show(args.id)
        if args.cmd == "classify":
            return triage.classify(args.id)
        if args.cmd == "dismiss":
            return triage.dismiss(args.id, args.reason)
        if args.cmd == "add-eval":
            return triage.add_eval(args.id, args.question, args.expect_refusal)
        return triage.promote(
            args.id, question=args.question, summary=args.summary, sql_file=args.sql_file,
            brands=args.brands, trio_id=args.trio_id,
        )  # fmt: skip
    except TriageRefused as exc:
        print(str(exc), file=out)
        return EXIT_REFUSED
    finally:
        if own:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
