"""Report writer (FR-23, AC-06.1, AC-06.2, AC-21.1; HLD role ``report_writer``).

The writer turns the turn's analysis (the deep analyst's grounded answer) and its SQL ledger
into a :class:`reports.schema.ReportDraft`. It calls no tools: one JSON object per call
(``specs=[]``). The code owns everything a persona could weaken: the section list and the
Markdown (``render_markdown``), the scope label and data window (``with_context``), the
"Data used" section (described from the ledger SQL, never from the model; D-151a) and the checks.

Bounded loop (every call goes through the budgeted ``LLMWrapper``):

* at most :data:`MAX_WRITER_CALLS` writer calls: the first draft, then one repair per problem
  round (invalid JSON, a pre-check failure, or a verifier rejection);
* at most :data:`MAX_VERIFIER_CALLS` verifier calls (:mod:`roles.verifier`).

Outcomes: ``passed`` (the verifier passed the last draft), ``unverified`` (the verifier could
not answer: a note says so), ``issues`` (problems remain after the last allowed rewrite: they
are listed under "Verification notes"), or ``failed`` (no parseable draft: the caller shows
the analysis answer instead and nothing can be saved).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from opsfleet_agent.graph.context import KIND_HISTORY_TURN, KIND_LEDGER, fence_untrusted
from opsfleet_agent.graph.llm import LLMSuccess, LLMWrapper
from opsfleet_agent.guards.plain_language import PLAIN_LANGUAGE_SECTION, REPORT_PLAIN_LANGUAGE_RULE
from opsfleet_agent.persona import Persona, assemble_prompt
from opsfleet_agent.reports.schema import (
    MAX_JSON_CHARS,
    ReportDraft,
    draft_hash,
    parse_draft,
    render_markdown,
    with_context,
)
from opsfleet_agent.roles.analyst import AnalystInvoke, ModelTurn
from opsfleet_agent.roles.verifier import VERIFIER_ROLE, Verdict, precheck, verify_report

__all__ = [
    "MAX_VERIFIER_CALLS",
    "MAX_WRITER_CALLS",
    "REPORT_ROLE_SUBCAPS",
    "UNVERIFIED_NOTE",
    "WRITER_ROLE",
    "ReportResult",
    "load_report_prompt",
    "produce_report",
]

WRITER_ROLE: Final = "report_writer"
REPORT_PROMPT_VERSION: Final = "report-writer-v1"
MAX_WRITER_CALLS: Final = 3
MAX_VERIFIER_CALLS: Final = 2
REPORT_ROLE_SUBCAPS: Final = {WRITER_ROLE: MAX_WRITER_CALLS, VERIFIER_ROLE: MAX_VERIFIER_CALLS}
MAX_ANALYSIS_CHARS: Final = 6000
UNVERIFIED_NOTE: Final = "Unverified: the automatic check could not run on this draft."
_BAD_JSON: Final = "the reply was not one valid JSON object with every required key"


@dataclass(frozen=True)
class ReportResult:
    status: str  # passed | unverified | issues | failed
    draft: ReportDraft | None = None
    markdown: str = ""
    sql_used: tuple[str, ...] = ()
    draft_hash: str = ""
    model: str = ""
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != "failed" and self.draft is not None


def _prompt_path() -> Path:
    return Path(__file__).resolve().parents[3] / "prompts" / "report_writer.md"


def load_report_prompt(path: Path | None = None) -> str:
    text = (path or _prompt_path()).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("report writer prompt is empty")
    return text


def _figure_line(figures: Sequence[Mapping[str, Any]], cap: int = 200) -> str:
    out: list[str] = []
    for group in figures:
        for v in group.get("values") or ():
            if isinstance(v, int | float) and not isinstance(v, bool):
                out.append(f"{v:g}")
            if len(out) >= cap:
                return ", ".join(out)
    return ", ".join(out)


def _text_of(res: Any) -> str | None:
    if not isinstance(res, LLMSuccess):
        return None
    value = res.response.value
    text = value.text if isinstance(value, ModelTurn) else value
    if not isinstance(text, str) or getattr(value, "tool_calls", ()):
        return None
    return text[:MAX_JSON_CHARS]


def produce_report(
    *,
    question: str,
    analysis: str,
    sql_ledger: Sequence[Mapping[str, Any]],
    figures: Sequence[Mapping[str, Any]],
    scope_label: str,
    window: tuple[date, date],
    llm: LLMWrapper,
    invoke: AnalystInvoke,
    models: Mapping[str, tuple[str, str | None]],
    persona: Persona,
    deadline_hit: Callable[[], bool] | None = None,
    prompt: str | None = None,
) -> ReportResult:
    """Write, check and render a report draft. Never saves anything (the graph confirms)."""
    sql_used = tuple(str(e.get("sql", "")) for e in sql_ledger if str(e.get("sql", "")).strip())
    lo, hi = window[0].isoformat(), window[1].isoformat()
    queries = "\n".join(
        f"- {' '.join(str(e.get('purpose') or '').split())} ({e.get('rows', 0)} rows): "
        f"{' '.join(str(e.get('sql', '')).split())}"
        for e in sql_ledger
    )
    system = assemble_prompt(
        [
            ("Scope", f"Data access: {scope_label}. The data covers {lo} to {hi}."),
            ("Report writer rules", prompt if prompt is not None else load_report_prompt()),
            (PLAIN_LANGUAGE_SECTION, REPORT_PLAIN_LANGUAGE_RULE),  # D-151: prose only
            (
                "Analysis",
                fence_untrusted(KIND_HISTORY_TURN, analysis, max_chars=MAX_ANALYSIS_CHARS),
            ),
            ("Queries", fence_untrusted(KIND_LEDGER, queries or "(none)")),
            ("Result figures", _figure_line(figures) or "(none)"),
        ],
        persona,
    )
    base = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    w_model, w_fb = models[WRITER_ROLE]
    v_model, v_fb = models[VERIFIER_ROLE]

    def write(messages: list[dict[str, Any]]) -> tuple[str | None, str]:
        res = llm.call(
            WRITER_ROLE, w_model, lambda t: invoke(w_model, messages, [], t),
            w_fb, (lambda t: invoke(w_fb, messages, [], t)) if w_fb else None,
        )  # fmt: skip
        return _text_of(res), (res.model if isinstance(res, LLMSuccess) else "")

    messages = list(base)
    draft: ReportDraft | None = None
    model_used = ""
    problems: list[str] = []  # what the next repair message lists
    verdict: Verdict | None = None
    # M2: the verdict and issues of the CURRENT draft only (None: not checked yet)
    draft_verdict: Verdict | None = None
    draft_issues: list[str] | None = None
    verifier_calls = 0
    for _ in range(MAX_WRITER_CALLS):  # bounded: one draft plus at most two repairs
        raw, model = write(messages)
        if raw is None:  # the writer is unavailable (budget, provider): keep any earlier draft
            break
        parsed = parse_draft(raw)
        verdict = None
        if parsed is None:
            problems = [_BAD_JSON]
        else:
            draft, model_used = (
                with_context(parsed, scope_label=scope_label, data_window=f"{lo} to {hi}"),
                model,
            )
            draft_verdict, draft_issues = None, None
            if verifier_calls >= MAX_VERIFIER_CALLS:
                break  # the final draft still gets the code checks below (no stale notes)
            verdict = verify_report(
                draft, sql_ledger, figures, llm=llm, invoke=invoke, model=v_model,
                fallback_model=v_fb, persona=persona, window=window, deadline_hit=deadline_hit,
            )  # fmt: skip
            if verdict.source != "code":
                verifier_calls += 1
            problems = list(verdict.issues)
            draft_verdict, draft_issues = verdict, list(verdict.issues)
            if verdict.passed or verdict.source == "unavailable":
                break
        listed = "\n".join(f"- {p}" for p in problems)
        messages = [
            *base,
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"Problems found in the draft:\n{listed}\n"
                                        "Reply with the full corrected JSON object."},
        ]  # fmt: skip
    if draft is None:
        return ReportResult("failed", issues=tuple(problems))
    if draft_issues is None:  # M2: never checked (verifier cap): deterministic checks only
        draft_issues = precheck(draft, figures, window=window, deadline_hit=deadline_hit)
    if draft_verdict is not None and draft_verdict.passed:
        status, notes, verification = "passed", (), ()
    elif draft_verdict is not None and draft_verdict.source == "unavailable":
        status, notes, verification = "unverified", (UNVERIFIED_NOTE,), ()
    elif draft_issues:
        status, notes, verification = "issues", (), tuple(draft_issues)
    else:  # the final draft passed the code checks but no LLM verifier saw it (cap reached)
        status, notes, verification = "unverified", (UNVERIFIED_NOTE,), ()
    markdown = render_markdown(draft, sql_used, notes=notes, verification=verification)
    return ReportResult(
        status, draft, markdown, sql_used, draft_hash(draft, sql_used), model_used,
        tuple(draft_issues),
    )  # fmt: skip
