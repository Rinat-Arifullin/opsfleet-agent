"""Golden eval cases as a Langfuse dataset, and live dataset runs (iteration 40b).

    uv run python evals/langfuse_dataset.py upload [--cases-dir DIR] [--dataset NAME]
                                                   [--profile ID ...] [--archive-stale]
    uv run python evals/langfuse_dataset.py run [--dataset NAME] [--run-name NAME]
                                                [--limit N] [--case ID ...] [--profile ID ...]

``upload`` upserts one dataset item per (case, profile) run (D-160 profile matrix: item id
``<case>@<profile>``, the profile in the metadata), so running it again updates the items in
place. Items in the dataset that no current run produces (a case renamed, removed or narrowed
to fewer profiles, or a pre-D-160 item without ``@<profile>``) are listed as stale and left
alone; ``--archive-stale`` archives them (status ARCHIVED; ``run`` skips archived items).

``run`` runs the live system under test (:mod:`evals.live_sut`) on each item, links the item
to the turn's Langfuse trace as a dataset run, and pushes the eval scorers' checks
(:func:`evals.run.run_case`, including the D-160 ``scope:*`` invariants) as scores.

Needs ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY`` and ``LANGFUSE_HOST`` (the project
``.env`` is loaded like the CLI does). Keys are never printed. Decisions:
docs/process/iter40b-ods.md, docs/process/iter-d160-ods.md.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

if __package__ in (None, ""):  # run as a script: make `evals` and the repo importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import judge as judge_mod  # noqa: E402
from evals import profile_matrix as matrix_mod  # noqa: E402
from evals.run import Case, CaseError, Harness, expand_cases, load_cases, run_case  # noqa: E402
from opsfleet_agent.obs.tracer import scrub_text  # noqa: E402

REPO: Final = Path(__file__).resolve().parents[1]
GOLDEN_DIR: Final = REPO / "evals" / "cases" / "golden"
DEFAULT_DATASET: Final = "opsfleet-golden"
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1  # at least one case failed
EXIT_REFUSED: Final = 2  # configuration or connection problem
MAX_ITEMS: Final = 500  # upload and run bound
FLUSH_BOUND_S: Final = 10.0
GIT_TIMEOUT_S: Final = 5.0
MAX_COMMENT: Final = 500
ENV_KEYS: Final = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")
MISSING_ENV_TEXT: Final = (
    "error: Langfuse is not configured. Set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and "
    "LANGFUSE_HOST (see infra/langfuse/README.md)."
)


class DatasetError(Exception):
    """A one-line, key-free message for the user; never a stack trace."""


# --------------------------------------------------------------------------- client


def connect(env: Mapping[str, str], factory: Callable[..., Any] | None = None) -> tuple[Any, str]:
    """A Langfuse client built like the CLI's sink (same masking), checked against the server."""
    from opsfleet_agent.obs.langfuse_sink import _host, build_sink, is_configured

    if not is_configured(env):
        raise DatasetError(MISSING_ENV_TEXT)
    host = _host(env).rstrip("/")
    sink = build_sink(env, factory=factory)
    if sink is None:
        raise DatasetError(f"error: could not start the Langfuse client for {host}.")
    try:
        ok = sink.client.auth_check()
    except Exception as exc:  # noqa: BLE001 - the class only: a message could echo a request
        raise DatasetError(
            f"error: cannot reach Langfuse at {host} ({type(exc).__name__}). "
            "Is the server running and are the keys right?"
        ) from None
    if ok is False:
        raise DatasetError(f"error: Langfuse at {host} rejected the client setup.")
    return sink.client, host


def _bounded(fn: Callable[[], object], timeout_s: float) -> None:
    def step() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - best effort flush
            pass

    worker = threading.Thread(target=step, daemon=True)
    worker.start()
    worker.join(timeout_s)


# --------------------------------------------------------------------------- mapping


def item_id(case_id: str, dataset: str = DEFAULT_DATASET) -> str:
    """Stable dataset item id: the run id, reduced to URL-safe characters (OD-2).

    The run id of a (case, profile) run is ``<case>@<profile>`` (D-160); ``@`` is kept.
    Langfuse item ids are unique across a project's datasets, so any dataset other than the
    default one prefixes its name; the same case in two datasets never collides."""
    safe = re.sub(r"[^A-Za-z0-9_.@-]", "__", case_id)
    if dataset != DEFAULT_DATASET:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "__", dataset) + "." + safe
    return safe[:200]


def case_file(case_id: str, cases_dir: Path) -> str | None:
    """The YAML file a case came from (the case id or its multi-case file stem), if found."""
    for stem in dict.fromkeys((case_id, case_id.split("/", 1)[0])):
        for suffix in (".yaml", ".yml"):
            for p in sorted(cases_dir.rglob(f"{Path(stem).name}{suffix}"))[:1]:
                try:
                    return str(p.resolve().relative_to(REPO))
                except ValueError:
                    return p.name
    return None


def item_payload(case: Case, cases_dir: Path, dataset: str = DEFAULT_DATASET) -> dict[str, Any]:
    """The create_dataset_item arguments for one case (without the dataset name)."""
    metadata: dict[str, Any] = {
        "case_id": case.id,
        "base_case_id": case.base_id or case.id,
        "profile": case.profile,
        "suite": case.suite,
        "tags": list(case.tags),
        "file": case_file(case.base_id or case.id, cases_dir),
    }
    if case.skip:
        metadata["skip"] = case.skip
    if case.known_brands:
        metadata["known_brands"] = list(case.known_brands)
    return {
        "id": item_id(case.id, dataset),
        "input": {
            "turns": list(case.turns),
            "profile": case.session.get("profile"),
            "session": dict(case.session),
        },
        "expected_output": dict(case.expect),
        "metadata": metadata,
    }


def case_from_item(item: Any) -> Case:
    inp = getattr(item, "input", None) or {}
    meta = getattr(item, "metadata", None) or {}
    if not isinstance(inp, dict) or not isinstance(meta, dict):
        raise DatasetError(f"error: dataset item {getattr(item, 'id', '?')} has an unknown shape.")
    turns = inp.get("turns") or []
    if not turns or not all(isinstance(t, str) and t for t in turns):
        raise DatasetError(f"error: dataset item {getattr(item, 'id', '?')} has no turns.")
    session = dict(inp.get("session") or {"profile": inp.get("profile")})
    case_id = str(meta.get("case_id") or getattr(item, "id", ""))
    # An item is one already-expanded (case, profile) run: never expanded again.
    return Case(
        id=case_id,
        suite=str(meta.get("suite") or "golden"),
        tags=list(meta.get("tags") or []),
        turns=list(turns),
        session=session,
        expect=dict(getattr(item, "expected_output", None) or {}),
        skip=meta.get("skip"),
        known_brands=list(meta.get("known_brands") or []) or None,
        profile=meta.get("profile") or session.get("profile"),
        base_id=str(meta.get("base_case_id") or case_id),
    )


def _active(item: Any) -> bool:
    status = getattr(item, "status", None)
    return str(getattr(status, "value", status) or "ACTIVE").upper() == "ACTIVE"


# --------------------------------------------------------------------------- upload


def upload(client: Any, cases: Sequence[Case], dataset: str, cases_dir: Path,
           out: TextIO, *, archive_stale: bool = False, partial: bool = False) -> int:  # fmt: skip
    """Upsert one item per run; report (or archive) items no current run produces.

    ``partial`` (a ``--profile`` filter) uploads a subset, so nothing is called stale."""
    if len(cases) > MAX_ITEMS:
        raise DatasetError(f"error: {len(cases)} cases > cap {MAX_ITEMS}.")
    existing = _existing_ids(client, dataset)
    if existing is None:
        client.create_dataset(
            name=dataset,
            description="OpsFleet golden eval cases (evals/cases/golden); synthetic.",
            metadata={"source": "evals/langfuse_dataset.py"},
        )
        existing = set()
    created = updated = 0
    current: set[str] = set()
    for case in cases:
        payload = item_payload(case, cases_dir, dataset)
        client.create_dataset_item(dataset_name=dataset, **payload)  # upsert by id
        current.add(payload["id"])
        if payload["id"] in existing:
            updated += 1
        else:
            created += 1
    stale = [] if partial else sorted(existing - current)[:MAX_ITEMS]
    archived = _archive(client, dataset, stale, out) if stale and archive_stale else 0
    _bounded(client.flush, FLUSH_BOUND_S)
    profiles = sorted({c.profile for c in cases if c.profile})
    print(f"Dataset {dataset}: {created} created, {updated} updated, {len(cases)} total "
          f"(profiles: {', '.join(profiles) or '-'}).", file=out)  # fmt: skip
    if stale:
        verb = f"{archived} archived" if archive_stale else "kept; --archive-stale archives them"
        more = " ..." if len(stale) > 20 else ""
        print(f"Stale items ({len(stale)}, {verb}): {', '.join(stale[:20])}{more}", file=out)
    return EXIT_OK


def _archive(client: Any, dataset: str, ids: Sequence[str], out: TextIO) -> int:
    """Set the items' status to ARCHIVED (an upsert by id that keeps the rest of the item)."""
    try:
        from langfuse.api import DatasetStatus

        status: Any = DatasetStatus.ARCHIVED
    except Exception:  # noqa: BLE001 - an older or absent SDK: the API takes the string
        status = "ARCHIVED"
    ds = client.get_dataset(dataset)
    by_id = {str(getattr(i, "id", "")): i for i in getattr(ds, "items", []) or []}
    done = 0
    for stale_id in ids:
        item = by_id.get(stale_id)
        if item is None or not _active(item):
            continue
        try:
            client.create_dataset_item(
                dataset_name=dataset, id=stale_id, input=getattr(item, "input", None),
                expected_output=getattr(item, "expected_output", None),
                metadata=getattr(item, "metadata", None), status=status,
            )  # fmt: skip
            done += 1
        except Exception as exc:  # noqa: BLE001 - one item never stops the upload
            print(f"  warning: {stale_id} not archived ({type(exc).__name__})", file=out)
    return done


def _existing_ids(client: Any, dataset: str) -> set[str] | None:
    """Item ids already in the dataset, or None when the dataset does not exist."""
    try:
        ds = client.get_dataset(dataset)
    except Exception as exc:  # noqa: BLE001
        if _is_not_found(exc):
            return None
        raise DatasetError(
            f"error: cannot read dataset {dataset} ({type(exc).__name__})."
        ) from None
    return {str(getattr(i, "id", "")) for i in getattr(ds, "items", []) or []}


def _is_not_found(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "NotFoundError"


# --------------------------------------------------------------------------- run


@dataclass
class ItemOutcome:
    case_id: str
    status: str  # pass, fail, na
    checks: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str | None = None
    base_id: str | None = None
    profile: str | None = None


def default_run_name(now: datetime | None = None, git: Callable[[], str] | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{(git or git_short_sha)()}-{stamp}"


def git_short_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return "nogit"
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{4,40}", sha) else "nogit"


def _clean(text: str) -> str:
    """Secrets scrubbed, then the sink's PII/secret mask: score comments carry no raw values."""
    from opsfleet_agent.obs.langfuse_sink import make_mask

    masked = make_mask()(data=scrub_text(text, MAX_COMMENT))
    return masked if isinstance(masked, str) else ""


def score_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Langfuse scores for one case record from :func:`evals.run.run_case` (OD-7)."""
    reasons = "; ".join(record.get("reasons") or [])
    rows = [{
        "name": "pass",
        "value": 1.0 if record.get("status") == "pass" else 0.0,
        "data_type": "BOOLEAN",
        "comment": _clean(reasons) or None,
    }]  # fmt: skip
    for check in record.get("checks") or []:
        rows.append({
            "name": f"check:{check['name']}"[:200],
            "value": 1.0 if check.get("ok") else 0.0,
            "data_type": "BOOLEAN",
            "comment": _clean(str(check.get("reason") or "")) or None,
        })  # fmt: skip
    return rows


def run_dataset(
    client: Any,
    sut: Any,
    *,
    dataset: str,
    run_name: str,
    host: str,
    cases: Sequence[str] = (),
    profiles: Sequence[str] = (),
    limit: int | None = None,
    trace_dir: Path,
    out: TextIO,
) -> int:
    try:
        ds = client.get_dataset(dataset)
    except Exception as exc:  # noqa: BLE001
        hint = " Run `upload` first." if _is_not_found(exc) else ""
        raise DatasetError(
            f"error: cannot read dataset {dataset} ({type(exc).__name__}).{hint}"
        ) from None
    items = [i for i in (getattr(ds, "items", None) or []) if _active(i)]
    pairs = [(i, case_from_item(i)) for i in items]
    if cases:
        wanted = set(cases)
        pairs = [(i, c) for i, c in pairs if wanted & {c.id, c.base_id or c.id, str(i.id)}]
    if profiles:
        keep = set(profiles)
        pairs = [(i, c) for i, c in pairs if c.profile in keep]
    pairs.sort(key=lambda p: p[1].id)
    pairs = pairs[: min(limit if limit is not None else MAX_ITEMS, MAX_ITEMS)]
    if not pairs:
        raise DatasetError(f"error: no items matched in dataset {dataset}.")
    print(f"Run {run_name}: {len(pairs)} item(s) from {dataset}.", file=out)
    harness = Harness(sut=sut)
    judge_model = judge_mod.judge_model_from_models_yaml(None)
    outcomes: list[ItemOutcome] = []
    run_id: str | None = None
    for item, case in pairs:
        cr, record, _ = run_case(case, harness, trace_dir=trace_dir, offline=False,
                                 judge_model=judge_model, judge_counts=False)  # fmt: skip
        outcome = ItemOutcome(case.id, cr.status, record.get("checks") or [],
                              base_id=case.base_id or case.id, profile=case.profile)  # fmt: skip
        outcomes.append(outcome)
        print(f"  {case.id}: {cr.status}", file=out)
        if cr.status == "na":
            continue  # not applicable: no run, nothing to link (OD-7)
        ids = list(getattr(sut, "last_trace_ids", []) or [])
        outcome.trace_id = ids[-1] if ids else _placeholder_trace(client, case, record)
        run_id = _link(client, run_name, item, outcome.trace_id, case) or run_id
        for row in score_rows(record):
            try:
                client.create_score(trace_id=outcome.trace_id, **row)
            except Exception as exc:  # noqa: BLE001 - one bad score never stops the run
                print(f"  warning: score {row['name']} not sent ({type(exc).__name__})", file=out)
    _bounded(client.flush, FLUSH_BOUND_S)
    if hasattr(sut, "flush"):
        sut.flush()
    print_summary(outcomes, out)
    url = run_url(host, getattr(ds, "project_id", None), getattr(ds, "id", None), run_id)
    print(f"Dataset run: {url}", file=out)
    return EXIT_FAILED if any(o.status == "fail" for o in outcomes) else EXIT_OK


def _link(client: Any, run_name: str, item: Any, trace_id: str | None, case: Case) -> str | None:
    """Create the dataset run item: dataset item -> trace, in run ``run_name``."""
    try:
        res = client.api.dataset_run_items.create(
            run_name=run_name,
            run_description="evals/langfuse_dataset.py run (live SUT)",
            metadata={"source": "evals/langfuse_dataset.py"},
            dataset_item_id=str(item.id),
            trace_id=trace_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  warning: {case.id} not linked to the run ({type(exc).__name__})",
              file=sys.stderr)  # fmt: skip
        return None
    return getattr(res, "dataset_run_id", None)


def _placeholder_trace(client: Any, case: Case, record: Mapping[str, Any]) -> str | None:
    """A minimal trace to hold the scores when the SUT failed before any turn (OD-6)."""
    try:
        span = client.start_observation(
            name="eval-case-failed",
            metadata={"case_id": case.id,
                      "reasons": _clean("; ".join(record.get("reasons") or []))},
        )  # fmt: skip
        span.end()
        return getattr(span, "trace_id", None)
    except Exception:  # noqa: BLE001
        return None


def run_url(host: str, project_id: str | None, dataset_id: str | None, run_id: str | None) -> str:
    if project_id and dataset_id and run_id:
        return f"{host}/project/{project_id}/datasets/{dataset_id}/runs/{run_id}"
    if project_id and dataset_id:
        return f"{host}/project/{project_id}/datasets/{dataset_id}"
    return host


def print_summary(outcomes: Sequence[ItemOutcome], out: TextIO) -> None:
    width = max([len(o.case_id) for o in outcomes] + [4])
    print(f"\n{'case'.ljust(width)}  status  checks  trace", file=out)
    for o in outcomes:
        ok = sum(1 for c in o.checks if c.get("ok"))
        checks = f"{ok}/{len(o.checks)}" if o.checks else "-"
        print(f"{o.case_id.ljust(width)}  {o.status:<6}  {checks:<6}  {o.trace_id or '-'}",
              file=out)  # fmt: skip
    counts = {s: sum(o.status == s for o in outcomes) for s in ("pass", "fail", "na")}
    print(f"\npass {counts['pass']}, fail {counts['fail']}, n/a {counts['na']}", file=out)
    table = matrix_mod.matrix(
        (o.base_id or o.case_id, o.profile, o.status) for o in outcomes if o.base_id != o.case_id
    )
    if table:
        print(f"\nProfile matrix ({sum(len(r) for r in table.values())} runs):", file=out)
        for line in matrix_mod.matrix_lines(table, matrix_mod.profile_ids()):
            print("  " + line, file=out)


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="evals/langfuse_dataset.py",
                                 description=__doc__.split("\n\n")[0])  # fmt: skip
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="upsert the cases as dataset items")
    up.add_argument("--cases-dir", type=Path, default=GOLDEN_DIR)
    up.add_argument("--dataset", default=DEFAULT_DATASET)
    up.add_argument("--profile", action="append", default=[],
                    help="upload only the runs under this profile; repeatable")  # fmt: skip
    up.add_argument("--archive-stale", action="store_true",
                    help="archive items no current (case, profile) run produces")  # fmt: skip
    run = sub.add_parser("run", help="run the live SUT on the dataset and score it")
    run.add_argument("--dataset", default=DEFAULT_DATASET)
    run.add_argument("--run-name", default=None, help="default: git short sha + UTC timestamp")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--case", action="append", default=[],
                     help="run id (case@profile) or case id; repeatable")  # fmt: skip
    run.add_argument("--profile", action="append", default=[],
                     help="run only the items under this profile; repeatable")  # fmt: skip
    return ap


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    factory: Callable[..., Any] | None = None,
    sut_factory: Callable[[], Any] | None = None,
    out: TextIO | None = None,
    load_env: bool = True,
) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    if args.cmd == "run" and args.limit is not None and args.limit < 1:
        print("error: --limit must be at least 1.", file=out)
        return EXIT_REFUSED
    if env is None:
        if load_env:
            from dotenv import load_dotenv

            load_dotenv()  # the project .env, as the CLI does; values are never printed
        env = os.environ
    try:
        client, host = connect(env, factory)
        if args.cmd == "upload":
            try:
                cases = expand_cases(load_cases(args.cases_dir), only=args.profile,
                                     all_golden=_is_golden_dir(args.cases_dir))  # fmt: skip
            except (CaseError, Exception) as exc:  # noqa: BLE001
                raise DatasetError(f"error: cannot load cases: {exc}") from None
            return upload(client, cases, args.dataset, args.cases_dir, out,
                          archive_stale=args.archive_stale, partial=bool(args.profile))  # fmt: skip
        sut = (sut_factory or _live_sut)()
        try:
            from evals.live_sut import eval_data_dir

            return run_dataset(
                client, sut, dataset=args.dataset, run_name=args.run_name or default_run_name(),
                host=host, cases=args.case, profiles=args.profile, limit=args.limit,
                trace_dir=eval_data_dir() / "traces", out=out,
            )  # fmt: skip
        finally:
            close = getattr(sut, "close", None)
            if close is not None:
                close()
    except DatasetError as exc:
        print(str(exc), file=out)
        return EXIT_REFUSED


def _is_golden_dir(path: Path) -> bool:
    """The cases dir is (inside) a ``golden`` dir: its root-level cases are golden cases."""
    return "golden" in path.resolve().parts


def _live_sut() -> Any:  # pragma: no cover - the real runtime
    from evals.live_sut import LiveSut

    return LiveSut()


if __name__ == "__main__":
    sys.exit(main())
