"""The report schema (AC-21.1), its validation and the Markdown renderer.

The model writes the report as JSON; this module parses it into :class:`ReportDraft`,
validates it in code and renders the Markdown body. The renderer owns the section list, so a
persona (or a model) cannot remove a section: every section of :data:`REQUIRED_SECTIONS` is
always rendered, and :func:`missing_sections` lets the store refuse a body without one.

Code-owned fields: the scope label and the data window are set by the caller from the profile
and the warehouse window, never taken from the model (:func:`with_context`).

D-151a: the body never shows SQL. The last section, "Data used", describes the executed
queries in business words (:func:`opsfleet_agent.guards.plain_language.describe_data_used`);
the statements themselves stay in the stored ``sql_used`` field.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from opsfleet_agent.guards.plain_language import describe_data_used

__all__ = [
    "MAX_SUMMARY_WORDS",
    "MIN_ACTION_ITEMS",
    "REQUIRED_SECTIONS",
    "ActionItem",
    "Insight",
    "KeyMetric",
    "ReportDraft",
    "draft_hash",
    "grounding_text",
    "missing_sections",
    "parse_draft",
    "quarter_without_year",
    "render_markdown",
    "validate_draft",
    "with_context",
]

MAX_SUMMARY_WORDS: Final = 120
MIN_ACTION_ITEMS: Final = 3
MAX_ITEMS: Final = 20  # every list in a draft (bounded input)
MAX_FIELD_CHARS: Final = 1200
MAX_TITLE_CHARS: Final = 120
MAX_JSON_CHARS: Final = 40_000  # a model reply longer than this is not parsed
MAX_SQL_CHARS: Final = 4000  # one statement described in the "Data used" section

# Rendered headings, in order. The title is the "# " line; these are the "## " sections.
REQUIRED_SECTIONS: Final = (
    "Definitions",
    "Summary",
    "Key metrics",
    "Insights",
    "Action items",
    "Limitations & hypotheses",
    "Data used",  # D-151a: was "SQL used"; describes the queries, never shows them
)
QUARTER_NOTE: Final = (
    "Note: a quarter is named without a year; the data window above shows the exact period."
)

# Words that cannot open a verb-first action (AC-21.1). A heuristic, not a grammar check
# (iter17 OD-5): it catches "The team should ..." and "Revenue is ..." style items.
_NOT_VERBS: Final = frozenset(
    "a an the we our this that these those it its they their there i you your he she "
    "revenue sales customers users orders products".split()
)
_QUARTER: Final = re.compile(r"\bQ[1-4]\b")
_YEAR: Final = re.compile(r"\b(?:19|20)\d{2}\b")
_FENCE: Final = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

_Text = Field(min_length=1, max_length=MAX_FIELD_CHARS)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, frozen=True)


class KeyMetric(_Model):
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=200)


class Insight(_Model):
    n: int = Field(ge=1, le=MAX_ITEMS)
    text: str = _Text
    figures: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)


class ActionItem(_Model):
    action: str = _Text
    insight_ref: int = Field(ge=1, le=MAX_ITEMS)
    metric_to_watch: str = Field(min_length=1, max_length=300)
    owner_function: str = Field(min_length=1, max_length=120)
    timeframe: str = Field(min_length=1, max_length=120)


class ReportDraft(_Model):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    scope_label: str = Field(default="", max_length=200)  # code-owned (with_context)
    data_window: str = Field(default="", max_length=80)  # code-owned (with_context)
    definitions: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    summary: str = Field(min_length=1, max_length=MAX_FIELD_CHARS * 2)
    key_metrics: list[KeyMetric] = Field(default_factory=list, max_length=MAX_ITEMS)
    insights: list[Insight] = Field(default_factory=list, max_length=MAX_ITEMS)
    action_items: list[ActionItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    limitations: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)
    tags: list[str] = Field(default_factory=list, max_length=8)


def parse_draft(text: Any) -> ReportDraft | None:
    """The draft in a model reply (a JSON object, optionally in a code fence), or None."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_JSON_CHARS:
        return None
    body = _FENCE.sub("", text.strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
        return ReportDraft.model_validate(_coerce(data)) if isinstance(data, dict) else None
    except (ValueError, ValidationError, TypeError):
        return None


# --- live1: tolerant shapes (small local models) ---------------------------------------------
# A model reply is coerced into the schema shape before validation. Only the SHAPE is
# repaired (a string where a list was asked, a dict of metrics, insights without numbers);
# no content is invented except neutral placeholders for missing action-item details. The
# title and the summary stay required: a reply without them is still no draft.

_NOT_STATED: Final = "(not stated)"
_FIGURE: Final = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_METRIC_KEYS: Final = (("name", "metric", "label", "key"), ("value", "amount", "figure", "result"))
_ACTION_KEYS: Final = {
    "action": ("action", "text", "item", "recommendation", "description"),
    "metric_to_watch": ("metric_to_watch", "metric", "kpi"),
    "owner_function": ("owner_function", "owner", "team", "function"),
    "timeframe": ("timeframe", "time_frame", "when", "deadline", "timeline"),
}


def _cut(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip(" -*\t") for line in value.splitlines() if line.strip(" -*\t")]
    if isinstance(value, dict):
        return [value]
    return list(value)[:MAX_ITEMS] if isinstance(value, list | tuple) else [value]


def _pick(item: dict[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if item.get(k) not in (None, ""):
            return item[k]
    return None


def _metrics(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict) and not ({"name", "value"} <= set(value)):
        value = [{"name": k, "value": v} for k, v in value.items()]
    out: list[dict[str, str]] = []
    for m in _as_list(value)[:MAX_ITEMS]:
        if isinstance(m, str):
            name, sep, val = m.partition(":")
            m = {"name": name, "value": val} if sep else None
        if isinstance(m, dict):
            name, val = _pick(m, _METRIC_KEYS[0]), _pick(m, _METRIC_KEYS[1])
            if name not in (None, "") and val not in (None, ""):
                out.append({"name": _cut(name, 200), "value": _cut(val, 200)})
    return out


def _ref(value: Any, default: int = 1) -> int:
    m = re.search(r"\d+", str(value)) if value is not None else None
    n = int(m.group()) if m else default
    return n if 1 <= n <= MAX_ITEMS else default


def _insights(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k, ins in enumerate(_as_list(value)[:MAX_ITEMS], 1):
        if isinstance(ins, str):
            ins = {"text": ins}
        if not isinstance(ins, dict):
            continue
        text = _pick(ins, ("text", "insight", "description", "finding"))
        if text in (None, ""):
            continue
        text = _cut(text, MAX_FIELD_CHARS)
        figures = ins.get("figures")
        figures = [_cut(f, 200) for f in _as_list(figures)] if figures else _FIGURE.findall(text)
        out.append({"n": _ref(ins.get("n"), k), "text": text, "figures": figures[:MAX_ITEMS]})
    return out


def _actions(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in _as_list(value)[:MAX_ITEMS]:
        if isinstance(a, str):
            a = {"action": a}
        if not isinstance(a, dict):
            continue
        item = {k: _pick(a, keys) for k, keys in _ACTION_KEYS.items()}
        if item["action"] in (None, ""):
            continue
        limits = {"action": MAX_FIELD_CHARS, "metric_to_watch": 300}
        out.append({
            **{k: _cut(v if v not in (None, "") else _NOT_STATED, limits.get(k, 120))
               for k, v in item.items()},
            "insight_ref": _ref(_pick(a, ("insight_ref", "insight", "ref"))),
        })  # fmt: skip
    return out


def _coerce(data: dict[str, Any]) -> dict[str, Any]:
    """The reply object in the schema's shape (see the comment above)."""
    inner = data.get("report")
    if isinstance(inner, dict) and "title" not in data:
        data = inner
    out = dict(data)
    if isinstance(out.get("summary"), list):
        out["summary"] = " ".join(str(x) for x in out["summary"])
    if isinstance(out.get("title"), str):
        out["title"] = _cut(out["title"], MAX_TITLE_CHARS)
    for name in ("definitions", "limitations", "tags"):
        if isinstance(out.get(name), dict):  # {"term": "meaning"}
            out[name] = [f"{k}: {v}" for k, v in out[name].items()]
        if name in out:
            cap = 8 if name == "tags" else MAX_ITEMS
            out[name] = [_cut(x, MAX_FIELD_CHARS) for x in _as_list(out[name]) if str(x).strip()]
            out[name] = out[name][:cap]
    if "key_metrics" in out:
        out["key_metrics"] = _metrics(out["key_metrics"])
    if "insights" in out:
        out["insights"] = _insights(out["insights"])
    if "action_items" in out:
        out["action_items"] = _actions(out["action_items"])
    return out


def with_context(draft: ReportDraft, *, scope_label: str, data_window: str) -> ReportDraft:
    """The draft with the code-owned fields set (the model's values are discarded)."""
    return draft.model_copy(update={"scope_label": scope_label, "data_window": data_window})


def quarter_without_year(text: str) -> bool:
    """True when a "Q1".."Q4" appears with no year within the same sentence (AC-06.2)."""
    for sentence in re.split(r"(?<=[.!?;])\s+|\n", text):
        if _QUARTER.search(sentence) and not _YEAR.search(sentence):
            return True
    return False


def _narrative(draft: ReportDraft) -> list[str]:
    return [
        draft.summary,
        *(f"{m.name}: {m.value}" for m in draft.key_metrics),
        *(i.text for i in draft.insights),
        *(a.action for a in draft.action_items),
    ]


def validate_draft(draft: ReportDraft) -> list[str]:
    """Issues with the draft (empty when it meets AC-21.1). Short, model-readable lines."""
    issues: list[str] = []
    if len(draft.summary.split()) > MAX_SUMMARY_WORDS:
        issues.append(f"summary has more than {MAX_SUMMARY_WORDS} words")
    for name in ("definitions", "key_metrics", "insights", "limitations"):
        if not getattr(draft, name):
            issues.append(f"section {name} is empty")
    numbers = [i.n for i in draft.insights]
    if numbers != list(range(1, len(numbers) + 1)):
        issues.append("insights must be numbered 1, 2, 3 ... in order")
    for ins in draft.insights:
        if not ins.figures or not any(re.search(r"\d", f) for f in ins.figures):
            issues.append(f"insight {ins.n} cites no figure")
    if len(draft.action_items) < MIN_ACTION_ITEMS:
        issues.append(f"fewer than {MIN_ACTION_ITEMS} action items")
    for k, item in enumerate(draft.action_items, 1):
        first = item.action.split()[0].lower().strip(",.:;") if item.action.split() else ""
        if not first.isalpha() or first in _NOT_VERBS:
            issues.append(f"action item {k} does not start with a verb")
        if item.insight_ref not in numbers:
            issues.append(f"action item {k} refers to a missing insight")
    if any(quarter_without_year(t) for t in _narrative(draft)):
        issues.append("a quarter is named without its year; state the year (e.g. Q1 2025)")
    return issues


def grounding_text(draft: ReportDraft) -> str:
    """The narrative fields checked against the SQL results (summary, metrics, insights).

    Joined without list numbering, so "1." of a numbered list is never read as a figure.
    Definitions, actions and limitations are excluded: they hold thresholds and timeframes
    ("within 30 days") that are not query results (iter17 OD-10).
    """
    return "\n".join(
        [draft.summary, *(f"{m.name}: {m.value}" for m in draft.key_metrics)]
        + [i.text for i in draft.insights]
    )


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def render_markdown(
    draft: ReportDraft,
    sql_used: Sequence[str],
    *,
    notes: Sequence[str] = (),
    verification: Sequence[str] = (),
) -> str:
    """The report body. Every section of REQUIRED_SECTIONS is rendered, empty or not."""
    lines = [f"# {_one_line(draft.title)}", ""]
    lines.append(f"Scope: {_one_line(draft.scope_label) or '(not set)'}")
    lines.append(f"Data window: {_one_line(draft.data_window) or '(not set)'}")
    defs = [_one_line(d) for d in draft.definitions]
    if any(quarter_without_year(t) for t in _narrative(draft)):
        defs.append(QUARTER_NOTE)  # AC-06.2: never leave the period implicit
    lines += ["", "## Definitions", *(f"- {d}" for d in defs or ["(none)"])]
    lines += ["", "## Summary", _one_line(draft.summary)]
    lines += [f"- {n}" for n in notes]
    lines += ["", "## Key metrics"]
    lines += [f"- {_one_line(m.name)}: {_one_line(m.value)}" for m in draft.key_metrics] or [
        "(none)"
    ]
    lines += ["", "## Insights"]
    for ins in draft.insights:
        figs = ", ".join(_one_line(f) for f in ins.figures)
        lines.append(f"{ins.n}. {_one_line(ins.text)}" + (f" (figures: {figs})" if figs else ""))
    if not draft.insights:
        lines.append("(none)")
    lines += ["", "## Action items"]
    for k, a in enumerate(draft.action_items, 1):
        lines.append(
            f"{k}. {_one_line(a.action)} (insight {a.insight_ref}; metric to watch: "
            f"{_one_line(a.metric_to_watch)}; owner: {_one_line(a.owner_function)}; "
            f"timeframe: {_one_line(a.timeframe)})"
        )
    if not draft.action_items:
        lines.append("(none)")
    lines += ["", "## Limitations & hypotheses"]
    lines += [f"- {_one_line(x)}" for x in draft.limitations] or ["(none)"]
    if verification:
        lines += ["", "## Verification notes", *(f"- {_one_line(v)}" for v in verification)]
    lines += ["", "## Data used"]
    stmts = [s.strip()[:MAX_SQL_CHARS] for s in sql_used if isinstance(s, str) and s.strip()]
    described = describe_data_used(stmts) if stmts else ""
    if described:
        lines.append(described)
    elif stmts:
        lines.append("Store data for the scope and window above.")
    else:
        lines.append("(none)")
    return "\n".join(lines).strip() + "\n"


def missing_sections(markdown: str) -> list[str]:
    """Required sections absent from a rendered body (the title line counts as one)."""
    found = {m.group(1).strip() for m in re.finditer(r"^## (.+)$", markdown, re.M)}
    missing = [s for s in REQUIRED_SECTIONS if s not in found]
    for label in ("Data window", "Scope"):
        if not re.search(rf"^{label}: \S", markdown, re.M):
            missing.insert(0, label)
    if not re.search(r"^# \S", markdown, re.M):
        missing.insert(0, "Title")
    return missing


def draft_hash(draft: ReportDraft, sql_used: Sequence[str]) -> str:
    """Stable hash of a draft and its SQL: a revised draft always gets a new one (AC-06.4)."""
    payload = json.dumps(
        {"draft": draft.model_dump(mode="json"), "sql": list(sql_used)},
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
