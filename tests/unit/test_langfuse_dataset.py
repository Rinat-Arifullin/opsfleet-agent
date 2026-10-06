"""Golden cases as a Langfuse dataset (iteration 40b). Offline: a fake client and a fake SUT."""

from __future__ import annotations

import io
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from evals import langfuse_dataset as D
from evals.run import CaseError, SutResult

ENV = {
    "LANGFUSE_PUBLIC_KEY": "pk-lf-test-public",
    "LANGFUSE_SECRET_KEY": "sk-lf-test-secret",
    "LANGFUSE_HOST": "http://localhost:3999",
}
GOLDEN = Path(D.__file__).parent / "cases" / "golden"


class NotFoundError(Exception):
    status_code = 404


class FakeClient:
    """In-memory stand-in for the Langfuse 4 client methods the module uses."""

    def __init__(self, *, auth: Any = True) -> None:
        self.auth = auth
        self.datasets: dict[str, dict[str, Any]] = {}
        self.items: dict[str, SimpleNamespace] = {}
        self.run_items: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.observations: list[str] = []
        self.flushed = 0
        self.api = SimpleNamespace(dataset_run_items=SimpleNamespace(create=self._run_item))

    def auth_check(self) -> bool:
        if isinstance(self.auth, BaseException):
            raise self.auth
        return self.auth

    def create_dataset(self, *, name: str, description: str, metadata: dict) -> None:
        self.datasets[name] = {"id": f"ds-{name}", "description": description}

    def create_dataset_item(self, *, dataset_name: str, id: str, **kw: Any) -> None:
        assert dataset_name in self.datasets
        self.items[id] = SimpleNamespace(id=id, dataset=dataset_name, status="ACTIVE", **kw)

    def get_dataset(self, name: str) -> SimpleNamespace:
        if name not in self.datasets:
            raise NotFoundError("not found")
        items = [i for i in self.items.values() if i.dataset == name]
        return SimpleNamespace(id=f"ds-{name}", name=name, project_id="proj-1", items=items)

    def _run_item(self, **kw: Any) -> SimpleNamespace:
        self.run_items.append(kw)
        return SimpleNamespace(dataset_run_id="run-1")

    def create_score(self, **kw: Any) -> None:
        self.scores.append(kw)

    def start_observation(self, *, name: str, metadata: dict) -> SimpleNamespace:
        self.observations.append(name)
        return SimpleNamespace(trace_id="trace-placeholder", end=lambda: None)

    def flush(self) -> None:
        self.flushed += 1


class FakeSut:
    """Answers every case with the item's expected outcome, unless told to fail."""

    def __init__(self, fail: set[str] = frozenset(), raise_for: set[str] = frozenset()) -> None:
        self.fail, self.raise_for = fail, raise_for
        self.seen: list[str] = []
        self.last_trace_ids: list[str] = []
        self.closed = self.flushed = False

    def __call__(self, case, ctx) -> SutResult:
        self.seen.append(case.id)
        self.last_trace_ids = []
        if case.id in self.raise_for:
            raise CaseError("timed out after 1s")
        self.last_trace_ids = [f"t-{case.id}-0", f"t-{case.id}-1"]
        outcome = "nope" if case.id in self.fail else case.expect.get("outcome", "answered")
        return SutResult(outcome=outcome, text="ok")

    def flush(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True


def write_case(root: Path, name: str, *, outcome: str = "answered", skip: str | None = None):
    body = f"id: {name}\nsession: {{profile: analyst_a}}\ntags: [t1]\n"
    body += "turns: ['How many orders last month?']\n"
    body += f"expect: {{outcome: {outcome}}}\n"
    if skip:
        body += f"skip: {skip}\n"
    (root / f"{name}.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def cases_dir(tmp_path: Path) -> Path:
    root = tmp_path / "cases"
    root.mkdir()
    write_case(root, "alpha")
    write_case(root, "beta", outcome="refused")
    write_case(root, "gamma", skip="needs a seed")
    return root


def main(args, client, *, sut=None, env=ENV, tmp_path=None, monkeypatch=None):
    out = io.StringIO()
    if monkeypatch is not None and tmp_path is not None:
        monkeypatch.setenv("OPSFLEET_EVAL_DATA_DIR", str(tmp_path / "eval"))
    code = D.main(args, env=env, factory=lambda **kw: client, sut_factory=lambda: sut, out=out)
    return code, out.getvalue()


# --------------------------------------------------------------------------- configuration


def test_missing_env_is_one_line_error_without_keys():
    client = FakeClient()
    code, out = main(["upload"], client, env={"LANGFUSE_HOST": "http://localhost:3999"})
    assert code == D.EXIT_REFUSED
    assert out.strip() == D.MISSING_ENV_TEXT
    assert "Traceback" not in out


def test_unreachable_server_names_host_and_class_only():
    client = FakeClient(auth=ConnectionError("boom sk-lf-test-secret"))
    code, out = main(["run"], client)
    assert code == D.EXIT_REFUSED
    assert "cannot reach Langfuse at http://localhost:3999 (ConnectionError)" in out
    assert "sk-lf" not in out and "pk-lf" not in out and "boom" not in out
    assert "Traceback" not in out


def test_rejected_auth_is_refused():
    code, out = main(["upload"], FakeClient(auth=False))
    assert code == D.EXIT_REFUSED and "rejected" in out


def test_limit_below_one_is_refused():
    code, out = main(["run", "--limit", "0"], FakeClient())
    assert code == D.EXIT_REFUSED and "--limit" in out


# --------------------------------------------------------------------------- upload


def test_upload_maps_case_to_item(cases_dir):
    client = FakeClient()
    code, out = main(["upload", "--cases-dir", str(cases_dir)], client)
    assert code == D.EXIT_OK
    assert "3 created, 0 updated" in out
    item = client.items["alpha"]
    assert item.dataset == D.DEFAULT_DATASET
    assert item.input == {
        "turns": ["How many orders last month?"],
        "profile": "analyst_a",
        "session": {"profile": "analyst_a"},
    }
    assert item.expected_output == {"outcome": "answered"}
    assert item.metadata["case_id"] == "alpha"
    assert item.metadata["tags"] == ["t1"]
    assert item.metadata["file"].endswith("alpha.yaml")
    assert client.items["gamma"].metadata["skip"] == "needs a seed"
    assert client.flushed >= 1


def test_upload_is_idempotent(cases_dir):
    client = FakeClient()
    main(["upload", "--cases-dir", str(cases_dir)], client)
    before = {k: vars(v).copy() for k, v in client.items.items()}
    code, out = main(["upload", "--cases-dir", str(cases_dir)], client)
    assert code == D.EXIT_OK
    assert "0 created, 3 updated" in out
    assert {k: vars(v) for k, v in client.items.items()} == before
    assert len(client.datasets) == 1


def test_item_ids_are_stable_and_prefixed_for_other_datasets():
    assert D.item_id("aov_by_traffic_source") == "aov_by_traffic_source"
    assert D.item_id("multi/case 1") == "multi__case__1"
    assert D.item_id("alpha", "my set") == "my__set.alpha"
    assert D.item_id("alpha", "other") != D.item_id("alpha")


def test_upload_golden_cases_round_trip():
    client = FakeClient()
    code, _ = main(["upload", "--cases-dir", str(GOLDEN)], client)
    assert code == D.EXIT_OK
    for item in client.items.values():
        case = D.case_from_item(item)
        assert D.item_id(case.id) == item.id
        assert case.turns and case.expect == item.expected_output
        assert item.metadata["file"] and item.metadata["file"].startswith("evals/cases/golden/")


def test_upload_case_filter_builds_a_small_dataset(cases_dir):
    client = FakeClient()
    args = ["upload", "--cases-dir", str(cases_dir), "--dataset", "smoke", "--case", "alpha"]
    code, out = main([*args, "--case", "beta"], client)
    assert code == D.EXIT_OK and "2 created" in out and "Stale" not in out
    assert sorted(client.items) == ["smoke.alpha", "smoke.beta"]


def test_upload_case_filter_on_golden_keeps_every_profile_run():
    client = FakeClient()
    code, out = main(["upload", "--dataset", "smoke", "--case", "monthly_revenue_12m",
                      "--case", "customer_contact_request@ceo_demo"], client)  # fmt: skip
    assert code == D.EXIT_OK
    ids = sorted(client.items)
    assert "smoke.customer_contact_request@ceo_demo" in ids
    assert not any(i.startswith("smoke.customer_contact_request@analyst") for i in ids)
    assert sum(i.startswith("smoke.monthly_revenue_12m@") for i in ids) >= 2


def test_upload_unknown_case_is_refused(cases_dir):
    client = FakeClient()
    code, out = main(["upload", "--cases-dir", str(cases_dir), "--case", "alhpa"], client)
    assert code == D.EXIT_REFUSED and "unknown case(s): alhpa" in out and not client.items


# --------------------------------------------------------------------------- run


def uploaded(cases_dir) -> FakeClient:
    client = FakeClient()
    main(["upload", "--cases-dir", str(cases_dir)], client)
    return client


def test_run_links_last_turn_trace_and_scores(cases_dir, tmp_path, monkeypatch):
    client, sut = uploaded(cases_dir), FakeSut()
    args = ["run", "--run-name", "abc-1"]
    code, out = main(args, client, sut=sut, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == D.EXIT_OK
    assert sut.seen == ["alpha", "beta"]  # gamma is n/a: never run
    linked = {r["dataset_item_id"]: r for r in client.run_items}
    assert set(linked) == {"alpha", "beta"}
    assert linked["alpha"]["trace_id"] == "t-alpha-1"  # the scored (last) turn
    assert {r["run_name"] for r in client.run_items} == {"abc-1"}
    names = {(s["trace_id"], s["name"]) for s in client.scores}
    assert ("t-alpha-1", "pass") in names and ("t-beta-1", "check:outcome") in names
    assert all(s["data_type"] == "BOOLEAN" for s in client.scores)
    assert "http://localhost:3999/project/proj-1/datasets/ds-opsfleet-golden/runs/run-1" in out
    assert "pass 2, fail 0, n/a 1" in out
    assert sut.flushed and sut.closed
    assert "sk-lf" not in out and "pk-lf" not in out


def test_run_failure_scores_zero_and_exit_one(cases_dir, tmp_path, monkeypatch):
    client, sut = uploaded(cases_dir), FakeSut(fail={"beta"})
    code, out = main(["run"], client, sut=sut, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == D.EXIT_FAILED
    beta = {s["name"]: s for s in client.scores if s["trace_id"] == "t-beta-1"}
    assert beta["pass"]["value"] == 0.0 and beta["check:outcome"]["value"] == 0.0
    assert "outcome" in beta["pass"]["comment"]


def test_run_sut_error_gets_placeholder_trace(cases_dir, tmp_path, monkeypatch):
    client, sut = uploaded(cases_dir), FakeSut(raise_for={"alpha"})
    args = ["run", "--case", "alpha"]
    code, _ = main(args, client, sut=sut, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == D.EXIT_FAILED
    assert client.observations == ["eval-case-failed"]
    assert client.run_items[0]["trace_id"] == "trace-placeholder"
    run_score = next(s for s in client.scores if s["name"] == "check:run")
    assert run_score["value"] == 0.0 and "timed out" in run_score["comment"]


def test_run_limit_and_case_filters(cases_dir, tmp_path, monkeypatch):
    client = uploaded(cases_dir)
    sut = FakeSut()
    main(["run", "--limit", "1"], client, sut=sut, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert sut.seen == ["alpha"]
    sut = FakeSut()
    main(["run", "--case", "beta"], client, sut=sut, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert sut.seen == ["beta"]
    code, out = main(["run", "--case", "nope"], client, sut=FakeSut(), tmp_path=tmp_path,
                     monkeypatch=monkeypatch)  # fmt: skip
    assert code == D.EXIT_REFUSED and "no items matched" in out


def test_run_without_dataset_hints_upload(tmp_path, monkeypatch):
    sut = FakeSut()
    code, out = main(["run"], FakeClient(), sut=sut, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert code == D.EXIT_REFUSED and "Run `upload` first" in out
    assert sut.closed


def test_score_rows_mapping():
    record = {
        "status": "fail",
        "reasons": ["outcome: expected answered, got refused"],
        "checks": [
            {"name": "outcome", "ok": False, "reason": "expected answered, got refused"},
            {"name": "sql_pattern", "ok": True, "reason": ""},
        ],
    }
    rows = D.score_rows(record)
    assert [(r["name"], r["value"]) for r in rows] == [
        ("pass", 0.0),
        ("check:outcome", 0.0),
        ("check:sql_pattern", 1.0),
    ]
    assert rows[0]["comment"].startswith("outcome:")
    assert rows[2]["comment"] is None
    assert D.score_rows({"status": "pass"})[0]["value"] == 1.0


# --------------------------------------------------------------------------- run name


def test_default_run_name_is_sha_and_utc_stamp():
    now = datetime(2026, 10, 5, 12, 30, 1, tzinfo=UTC)
    assert D.default_run_name(now, git=lambda: "ca80603") == "ca80603-20261005T123001Z"


def test_git_short_sha_falls_back(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(D.subprocess, "run", boom)
    assert D.git_short_sha() == "nogit"
    monkeypatch.setattr(
        D.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="ca80603\n")
    )
    assert D.git_short_sha() == "ca80603"


def test_run_url_degrades_without_ids():
    assert D.run_url("http://h", "p", "d", "r") == "http://h/project/p/datasets/d/runs/r"
    assert D.run_url("http://h", "p", "d", None) == "http://h/project/p/datasets/d"
    assert D.run_url("http://h", None, None, None) == "http://h"


@pytest.mark.live
def test_live_upload_against_local_langfuse():  # pragma: no cover - needs a running server
    assert D.main(["upload"]) == D.EXIT_OK
