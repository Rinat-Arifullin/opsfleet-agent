"""D-160 profile matrix: case expansion, overrides, scope invariants, Langfuse ids. Offline."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from evals import langfuse_dataset as D
from evals import profile_matrix as M
from evals import run as R
from evals.run import Case, CaseError, SutResult

from opsfleet_agent.guards import scope_ctes
from opsfleet_agent.guards.scope import ProductScope, apply_scope
from opsfleet_agent.obs.tracer import sanitize_sql
from opsfleet_agent.session import load_profiles
from tests.unit.test_langfuse_dataset import ENV, FakeClient, FakeSut

PROFILES = ["analyst_a", "analyst_b", "ceo_demo"]
QUERY = (
    "SELECT p.category, SUM(oi.sale_price) AS revenue FROM order_items oi "
    "JOIN products p ON p.id = oi.product_id WHERE oi.status = 'Complete' GROUP BY p.category"
)


def golden(cid: str = "c", **kw: Any) -> Case:
    base: dict[str, Any] = {
        "turns": ["q"],
        "session": {"profile": "analyst_a"},
        "expect": {"outcome": "answered", "must_contain": ["revenue"]},
        "fake": {"outcome": "answered", "text": "revenue"},
    }
    base.update(kw)
    return Case(id=cid, suite="golden", **base)


def scope(pid: str) -> ProductScope:
    return ProductScope.from_profile(load_profiles()[pid])


def scoped_sql(pid: str, sql: str = QUERY) -> str:
    return apply_scope(sql, scope(pid)).sql


def names(checks):
    return {n: (ok, why) for n, ok, why in checks}


# --------------------------------------------------------------------------- expansion


def test_golden_case_defaults_to_every_profile():
    runs = M.expand_profiles([golden()], PROFILES)
    assert [r.id for r in runs] == ["c@analyst_a", "c@analyst_b", "c@ceo_demo"]
    assert {r.base_id for r in runs} == {"c"}
    assert [r.profile for r in runs] == PROFILES
    assert [r.session["profile"] for r in runs] == PROFILES


def test_profiles_all_and_explicit_list():
    assert len(M.expand_profiles([golden(profiles="all")], PROFILES)) == 3
    runs = M.expand_profiles([golden(profiles=["ceo_demo"], profiles_reason="CEO only")], PROFILES)
    assert [r.id for r in runs] == ["c@ceo_demo"]


def test_non_golden_case_is_a_single_legacy_run():
    case = Case(id="router/x", suite="router", turns=["q"], session={"profile": "analyst_b"})
    (run,) = M.expand_profiles([case], PROFILES)
    assert run.id == "router/x" and run.base_id == "router/x" and run.profile == "analyst_b"
    assert M.expand_profiles([case], PROFILES, only=["analyst_a"]) == []


def test_all_golden_flag_expands_root_level_cases():
    case = Case(id="x", suite=".", turns=["q"], session={"profile": "analyst_a"})
    assert len(M.expand_profiles([case], PROFILES)) == 1
    assert len(M.expand_profiles([case], PROFILES, all_golden=True)) == 3


def test_subset_without_reason_is_an_error():
    with pytest.raises(M.MatrixError, match="profiles_reason"):
        M.expand_profiles([golden(profiles=["analyst_a"])], PROFILES)


def test_unknown_profiles_are_errors():
    with pytest.raises(M.MatrixError, match="unknown profiles"):
        M.expand_profiles([golden(profiles=["nobody"], profiles_reason="x")], PROFILES)
    with pytest.raises(M.MatrixError, match="unknown --profile"):
        M.expand_profiles([golden()], PROFILES, only=["nobody"])
    with pytest.raises(M.MatrixError, match="does not run under"):
        case = golden(profiles=["analyst_a"], profiles_reason="x",
                      per_profile={"ceo_demo": {"fake": {"text": "t"}}})  # fmt: skip
        M.expand_profiles([case], PROFILES)


def test_only_filter_keeps_runs_under_the_profile():
    runs = M.expand_profiles([golden(), golden("d")], PROFILES, only=["ceo_demo"])
    assert [r.id for r in runs] == ["c@ceo_demo", "d@ceo_demo"]


def test_per_profile_overrides_merge_over_the_base():
    case = golden(
        per_profile={
            "ceo_demo": {
                "expect": {"must_contain": ["revenue", "all products"]},
                "fake": {"text": "revenue across all products"},
            },
            "analyst_b": {"skip": "needs a seed"},
        }
    )
    runs = {r.profile: r for r in M.expand_profiles([case], PROFILES)}
    assert runs["analyst_a"].expect == case.expect
    assert runs["ceo_demo"].expect == {"outcome": "answered",
                                       "must_contain": ["revenue", "all products"]}  # fmt: skip
    assert runs["ceo_demo"].fake == {"outcome": "answered", "text": "revenue across all products"}
    assert runs["analyst_b"].skip == "needs a seed" and runs["analyst_a"].skip is None
    assert case.expect == {"outcome": "answered", "must_contain": ["revenue"]}  # not mutated


@pytest.mark.parametrize(
    "raw, match",
    [
        ({"profiles": []}, "profiles"),
        ({"profiles": ["a", "a"]}, "profiles"),
        ({"profiles_reason": " "}, "profiles_reason"),
        ({"per_profile": {"analyst_a": {"turns": ["x"]}}}, "may only set"),
        ({"per_profile": {"analyst_a": {"expect": {"bogus": 1}}}}, "unknown expect keys"),
        ({"known_brands": "Acme"}, "known_brands"),
    ],
)
def test_validate_fields_rejects_bad_shapes(raw, match):
    with pytest.raises(M.MatrixError, match=match):
        M.validate_fields(raw, "c", ["outcome", "must_contain"])


def test_runner_expand_cases_wraps_matrix_errors():
    with pytest.raises(CaseError, match="unknown --profile"):
        R.expand_cases([golden()], PROFILES, only=["nobody"])


def test_select_cases_matches_run_and_base_ids():
    runs = R.expand_cases([golden()], PROFILES)
    assert len(R.select_cases(runs, [], ["c"])) == 3
    assert [r.id for r in R.select_cases(runs, [], ["c@ceo_demo"])] == ["c@ceo_demo"]


# --------------------------------------------------------------------------- invariants


def run_checks(pid: str, text: str, sql: list[str] | None = None, **kw: Any):
    case = golden(session={"profile": pid}, **kw)
    return names(M.scope_checks(case, SutResult(outcome="answered", text=text, sql=sql or [])))


def test_answer_naming_an_out_of_scope_brand_fails():
    checks = run_checks("analyst_a", "Carhartt led revenue.")
    assert checks["scope:answer_brands"][0] is False
    assert "Carhartt" in checks["scope:answer_brands"][1]
    assert run_checks("analyst_a", "Calvin Klein led revenue.")["scope:answer_brands"][0]


def test_case_known_brands_extend_the_list():
    assert run_checks("analyst_a", "Acme led.")["scope:answer_brands"][0]
    checks = run_checks("analyst_a", "Acme led.", known_brands=["Acme"])
    assert checks["scope:answer_brands"][0] is False


def test_all_products_profile_has_no_brand_check_but_a_path_check():
    checks = run_checks("ceo_demo", "Carhartt and Calvin Klein led.")
    assert "scope:answer_brands" not in checks
    assert checks["scope:all_products_path"] == (True, "")


def test_apply_scope_sql_passes_for_each_profile():
    for pid in PROFILES:
        checks = run_checks(pid, "revenue", [scoped_sql(pid)])
        assert checks["scope:sql"] == (True, ""), pid


def test_unscoped_sql_fails_for_a_brand_scoped_profile():
    checks = run_checks("analyst_a", "revenue", [QUERY])
    assert checks["scope:sql"][0] is False
    checks = run_checks("analyst_b", "revenue", [scoped_sql("ceo_demo")])
    assert checks["scope:sql"][0] is False


def test_brand_filter_on_the_all_products_path_fails():
    checks = run_checks("ceo_demo", "revenue", [scoped_sql("analyst_a")])
    assert checks["scope:sql"][0] is False
    assert checks["scope:all_products_path"][0] is False


def test_sanitized_trace_sql_is_judged_like_the_original():
    text = sanitize_sql(scoped_sql("analyst_a"))[0]
    assert "?" in text and "'Complete'" not in text
    assert run_checks("analyst_a", "revenue", [text])["scope:sql"] == (True, "")
    assert run_checks("analyst_a", "revenue", [sanitize_sql(QUERY)[0]])["scope:sql"][0] is False


def test_hash_only_trace_entry_fails():
    checks = run_checks("analyst_a", "revenue", ["sha256:0123abcd"])
    assert checks["scope:sql"][0] is False and "unparseable" in checks["scope:sql"][1]


def test_no_sql_means_no_sql_check_and_no_profile_means_no_checks():
    assert "scope:sql" not in run_checks("analyst_a", "revenue")
    case = Case(id="x", suite="router", turns=["q"])
    assert M.scope_checks(case, SutResult(text="Carhartt")) == []


def test_unknown_profile_is_a_failed_check():
    checks = run_checks("nobody", "revenue")
    assert checks["scope:profile"][0] is False


@pytest.mark.parametrize("name", scope_ctes.CODE_CTE_ORDER)
@pytest.mark.parametrize("scoped", [True, False])
def test_code_cte_bodies_have_no_literals(name, scoped):
    """The `?` -> literal swap in the invariant is sound only while this holds."""
    body = scope_ctes.cte_body_sql(name, scoped=scoped)
    assert "?" not in sanitize_sql(f"SELECT * FROM ({body})")[0]


def test_matrix_table():
    table = M.matrix([("a", "analyst_a", "pass"), ("a", "ceo_demo", "fail"),
                      ("b", "analyst_a", "na"), ("legacy", None, "pass")])  # fmt: skip
    assert table == {"a": {"analyst_a": "pass", "ceo_demo": "fail"}, "b": {"analyst_a": "na"}}
    lines = M.matrix_lines(table, PROFILES)
    assert lines[0].split() == ["case", "analyst_a", "ceo_demo"]
    assert lines[1].split() == ["a", "PASS", "FAIL"]
    assert lines[2].split() == ["b", "N/A", "-"]
    assert M.matrix_lines({}) == []


# --------------------------------------------------------------------------- golden set


def test_every_golden_case_expands_and_subsets_say_why():
    cases = R.load_cases(R.CASES_DIR)
    runs = R.expand_cases(cases)
    golden_runs = [r for r in runs if M.is_golden(r)]
    assert golden_runs and all("@" in r.id for r in golden_runs)
    for case in cases:
        if M.is_golden(case) and case.profiles not in (None, "all"):
            assert set(case.profiles) == set(PROFILES) or case.profiles_reason, case.id


# --------------------------------------------------------------------------- Langfuse


class ArchivingClient(FakeClient):
    """FakeClient whose upsert keeps a passed status (the real API's behaviour)."""

    def create_dataset_item(self, *, dataset_name: str, id: str, **kw: Any) -> None:
        assert dataset_name in self.datasets
        status = kw.pop("status", None)
        status = str(getattr(status, "value", status) or "ACTIVE")
        self.items[id] = SimpleNamespace(id=id, dataset=dataset_name, status=status, **kw)


def write_golden(root: Path) -> Path:
    gdir = root / "golden"
    gdir.mkdir(parents=True)
    (gdir / "alpha.yaml").write_text(
        "input: How many orders last month?\n"
        "expect: {outcome: answered}\n"
        "per_profile:\n  ceo_demo: {expect: {must_contain: [all products]}}\n",
        encoding="utf-8",
    )
    (gdir / "beta.yaml").write_text(
        "input: Show my saved report\nexpect: {outcome: answered}\n"
        "profiles: [analyst_a]\nprofiles_reason: fixture owned by analyst_a\n",
        encoding="utf-8",
    )
    return gdir


def lf(args, client, sut=None, tmp_path=None, monkeypatch=None):
    out = io.StringIO()
    if monkeypatch is not None:
        monkeypatch.setenv("OPSFLEET_EVAL_DATA_DIR", str(tmp_path / "eval"))
    code = D.main(args, env=ENV, factory=lambda **kw: client, sut_factory=lambda: sut, out=out)
    return code, out.getvalue()


def test_item_id_keeps_the_profile_separator():
    assert D.item_id("q1_report@ceo_demo") == "q1_report@ceo_demo"
    assert D.item_id("a b@ceo_demo", "my set") == "my__set.a__b@ceo_demo"


def test_upload_creates_one_item_per_case_and_profile(tmp_path):
    client = ArchivingClient()
    code, out = lf(["upload", "--cases-dir", str(write_golden(tmp_path))], client)
    assert code == D.EXIT_OK
    assert sorted(client.items) == ["alpha@analyst_a", "alpha@analyst_b", "alpha@ceo_demo",
                                    "beta@analyst_a"]  # fmt: skip
    item = client.items["alpha@ceo_demo"]
    assert item.metadata["profile"] == "ceo_demo"
    assert item.metadata["base_case_id"] == "alpha"
    assert item.metadata["file"].endswith("alpha.yaml")
    assert item.input["profile"] == "ceo_demo" and item.input["session"]["profile"] == "ceo_demo"
    assert item.expected_output == {"outcome": "answered", "must_contain": ["all products"]}
    case = D.case_from_item(item)
    assert (case.id, case.base_id, case.profile) == ("alpha@ceo_demo", "alpha", "ceo_demo")
    assert "4 created, 0 updated" in out and "Stale" not in out


def test_upload_profile_filter_and_stale_items(tmp_path):
    gdir = write_golden(tmp_path)
    client = ArchivingClient()
    code, out = lf(["upload", "--cases-dir", str(gdir), "--profile", "ceo_demo"], client)
    assert code == D.EXIT_OK and sorted(client.items) == ["alpha@ceo_demo"]
    client.items["alpha"] = SimpleNamespace(id="alpha", dataset=D.DEFAULT_DATASET,
                                            status="ACTIVE", input={"turns": ["q"]},
                                            expected_output={}, metadata={})  # fmt: skip
    code, out = lf(["upload", "--cases-dir", str(gdir), "--profile", "ceo_demo"], client)
    assert "Stale" not in out  # a partial upload names nothing stale
    code, out = lf(["upload", "--cases-dir", str(gdir)], client)
    assert "Stale items (1, kept; --archive-stale archives them): alpha" in out
    assert client.items["alpha"].status == "ACTIVE"
    code, out = lf(["upload", "--cases-dir", str(gdir), "--archive-stale"], client)
    assert code == D.EXIT_OK and "1 archived" in out
    assert client.items["alpha"].status == "ARCHIVED"
    assert client.items["alpha"].input == {"turns": ["q"]}  # the rest of the item kept


def test_upload_unknown_profile_is_refused(tmp_path):
    code, out = lf(["upload", "--cases-dir", str(write_golden(tmp_path)), "--profile", "x"],
                   ArchivingClient())  # fmt: skip
    assert code == D.EXIT_REFUSED and "unknown --profile" in out


def test_run_filters_by_profile_and_case_and_prints_the_matrix(tmp_path, monkeypatch):
    client = ArchivingClient()
    lf(["upload", "--cases-dir", str(write_golden(tmp_path))], client)
    sut = FakeSut()
    code, out = lf(["run", "--profile", "analyst_a"], client, sut, tmp_path, monkeypatch)
    assert sorted(sut.seen) == ["alpha@analyst_a", "beta@analyst_a"]
    assert "Profile matrix (2 runs):" in out
    sut = FakeSut()
    lf(["run", "--case", "alpha"], client, sut, tmp_path, monkeypatch)
    assert sorted(sut.seen) == ["alpha@analyst_a", "alpha@analyst_b", "alpha@ceo_demo"]
    linked = {r["dataset_item_id"] for r in client.run_items}
    assert "alpha@ceo_demo" in linked
