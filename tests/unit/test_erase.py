"""User erasure command (iteration 35; SEC-18, AC-28.6, FR-59; D-222..D-226).

Synthetic users only; no network. The residue scan lives in ``tests/unit/test_residue.py``.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from opsfleet_agent import commands
from opsfleet_agent.commands import erase as E
from opsfleet_agent.store import audit as A
from opsfleet_agent.store.db import open_store
from opsfleet_agent.store.feedback import FeedbackStore
from tests.unit.test_residue import (
    KEY_ENV,
    MAINTAINER,
    MAINTAINERS,
    T0,
    USER_A,
    USER_B,
    World,
    build_world,
    user_rows,
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = build_world(tmp_path)
    yield w
    w.conn.close()


def _token(text: str) -> str:
    return re.search(r"--confirm (\S+)", text).group(1)  # type: ignore[union-attr]


def _rows(w: World, user: str) -> dict[str, int]:
    return user_rows(w.conn, user, w.users[user].report_id)


def _events(w: World, event_type: str) -> list[A.AuditEvent]:
    return [e for e in A.AuditLog(w.conn).events() if e.event_type == event_type]


def _untouched(w: World, before: dict[str, int]) -> None:
    assert _rows(w, USER_A) == before
    assert all(p.exists() for ps in w.users[USER_A].files.values() for p in ps)
    assert not _events(w, A.ERASE_EXECUTED)


def test_erase_removes_all_user_rows(world: World) -> None:
    before_b = _rows(world, USER_B)
    code, text = world.erase(USER_A)
    assert code == E.EXIT_OK, text
    assert all(n == 0 for n in _rows(world, USER_A).values())
    assert _rows(world, USER_B) == before_b
    assert "saved_report             1 deleted" in text and "pseudonymised" in text


def test_preview_deletes_nothing_and_prints_counts_not_content(world: World) -> None:
    before = _rows(world, USER_A)
    code, text = world.run("--user", USER_A)
    assert code == E.EXIT_OK and "nothing deleted yet" in text
    assert re.search(r"saved_report\s+1\b", text) and re.search(r"traces\s+1\b", text)
    assert "zqx" not in text and world.users[USER_A].report_id not in text
    assert E.NOT_COVERED in text and "regression-case draft" in text
    _untouched(world, before)


@pytest.mark.parametrize(
    "extra",
    [
        [],  # no --confirm: preview only
        ["--confirm", "not-a-token", "--retype", USER_A],
        ["--confirm", f"{int(T0) + 60}.{'0' * 32}", "--retype", USER_A],  # forged mac
    ],
)
def test_confirmation_required(world: World, extra: list[str]) -> None:
    before = _rows(world, USER_A)
    code, text = world.run("--user", USER_A, *extra)
    if extra:
        assert code == E.EXIT_REFUSED and E.REFUSED_TOKEN in text
    _untouched(world, before)


def test_confirmation_expires(world: World) -> None:
    before = _rows(world, USER_A)
    token = _token(world.run("--user", USER_A)[1])
    late = T0 + E.CONFIRM_TTL_S + 1
    code, text = world.run("--user", USER_A, "--confirm", token, "--retype", USER_A, clock=late)
    assert code == E.EXIT_REFUSED and E.REFUSED_TOKEN in text
    _untouched(world, before)


def test_confirmation_bound_to_user_actor_and_plan(world: World) -> None:
    before = _rows(world, USER_A)
    token = _token(world.run("--user", USER_A)[1])
    # another user with A's token
    code, text = world.run("--user", USER_B, "--confirm", token, "--retype", USER_B)
    assert code == E.EXIT_REFUSED and E.REFUSED_TOKEN in text
    # the plan changed after the preview (new feedback row): the token no longer matches
    FeedbackStore(world.conn).add(user_id=USER_A, session_id="c3" * 16, turn_id="a1b2c3d4e5f6",
                                  trace_id=None, rating="up")  # fmt: skip
    before = _rows(world, USER_A)
    code, text = world.run("--user", USER_A, "--confirm", token, "--retype", USER_A)
    assert code == E.EXIT_REFUSED and E.REFUSED_TOKEN in text
    _untouched(world, before)


def test_retype_must_match(world: World) -> None:
    before = _rows(world, USER_A)
    token = _token(world.run("--user", USER_A)[1])
    for retype in ([], ["--retype", USER_B], ["--retype", USER_A.upper()]):
        code, text = world.run("--user", USER_A, "--confirm", token, *retype)
        assert code == E.EXIT_REFUSED and E.REFUSED_RETYPE in text
    _untouched(world, before)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--as", USER_B, "--user", USER_A], E.REFUSED_ROLE),  # a normal user
        (["--as", "Support Demo", "--user", USER_A], E.REFUSED_ROLE),
        (["--as", MAINTAINER, "--user", "../etc"], E.REFUSED_USER),
        (["--as", MAINTAINER, "--user", ""], E.REFUSED_USER),
        (["--as", MAINTAINER, "--user", MAINTAINER], E.REFUSED_SELF),
    ],
)
def test_refusals_before_any_write(world: World, argv: list[str], message: str) -> None:
    before = _rows(world, USER_A)
    out = io.StringIO()
    code = E.main([*argv, "--data-dir", str(world.data_dir)], conn=world.conn,
                  maintainers=MAINTAINERS, env=KEY_ENV, clock=lambda: T0,
                  cases_dir=world.cases_dir, out=out)  # fmt: skip
    assert code == E.EXIT_REFUSED and message in out.getvalue()
    _untouched(world, before)


def test_user_session_cannot_erase_another_user(world: World) -> None:
    """The REPL /erase only explains the process: no argument makes it delete anything."""
    before = _rows(world, USER_A)
    ctx = commands.CommandContext(user_id=USER_B, session_id="s")
    for line in ("/erase", f"/erase {USER_A}", f"/erase {USER_A} --confirm x --retype x"):
        text = commands.dispatch(line, ctx).text
        assert text == commands.ERASE_INFO_TEXT and "Nothing was deleted" in text
    assert "/erase" in commands.COMMANDS
    _untouched(world, before)


def test_erase_audit_first_aborts_on_audit_failure(world: World, monkeypatch) -> None:
    before = _rows(world, USER_A)
    token = _token(world.run("--user", USER_A)[1])

    def boom(self, event):  # the erase.executed insert fails
        raise A.AuditError("synthetic audit failure")

    monkeypatch.setattr(A.AuditLog, "_insert", boom)
    code, text = world.run("--user", USER_A, "--confirm", token, "--retype", USER_A)
    assert code == E.EXIT_FAIL and "Nothing was deleted" in text
    monkeypatch.undo()
    _untouched(world, before)
    assert not world.conn.in_transaction


def test_erase_rolls_back_when_a_delete_fails_after_the_audit_row(world: World) -> None:
    before = _rows(world, USER_A)
    token = _token(world.run("--user", USER_A)[1])
    # a trigger makes the quota delete fail after erase.executed was inserted
    world.conn.execute(
        "CREATE TRIGGER block_quota BEFORE DELETE ON user_quota "
        "BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
    )
    code, text = world.run("--user", USER_A, "--confirm", token, "--retype", USER_A)
    # D-230: a failure, not a refusal; the message says it was rolled back
    assert code == E.EXIT_FAIL and "Erase FAILED and was rolled back" in text
    assert "Refused" not in text
    assert _rows(world, USER_A) == before
    assert not _events(world, A.ERASE_EXECUTED)  # rolled back with the deletes
    failed = _events(world, A.ERASE_FAILED)
    assert len(failed) == 1 and failed[0].details.get("store") == "app_db"
    (attempted,) = _events(world, A.ERASE_ATTEMPTED)  # the durable start row it matches
    assert (attempted.session_id, attempted.turn_id) == (failed[0].session_id, failed[0].turn_id)
    assert attempted.details["target_user"] == failed[0].details["target_user"]
    assert attempted.actor_user_id == MAINTAINER and attempted.seq < failed[0].seq


def test_failure_after_the_audit_trigger_is_dropped_rolls_back_and_restores_it(
    world: World, monkeypatch
) -> None:
    """D-230: the pseudonymisation drops audit_event_no_update; a failure after the drop
    rolls back the deletes, the rewrite and the DROP, so the trigger is back."""
    before = _rows(world, USER_A)
    audit_before = [(e.event_id, e.actor_user_id) for e in A.AuditLog(world.conn).events()]
    token = _token(world.run("--user", USER_A)[1])
    real = A._no_update_trigger

    def broken() -> str:  # the recreate statement fails after the DROP and the UPDATEs ran
        real()
        raise RuntimeError("synthetic failure after the trigger drop")

    monkeypatch.setattr(A, "_no_update_trigger", broken)
    code, text = world.run("--user", USER_A, "--confirm", token, "--retype", USER_A)
    monkeypatch.undo()
    assert code == E.EXIT_FAIL and "rolled back" in text
    assert _rows(world, USER_A) == before
    kept = {e.event_id: e.actor_user_id for e in A.AuditLog(world.conn).events()}
    assert all(kept[i] == actor for i, actor in audit_before)  # no row pseudonymised
    assert world.conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
        "AND name = 'audit_event_no_update'"
    ).fetchone()[0] == 1
    with pytest.raises(Exception, match="(?i)audit"):  # the trigger still blocks an UPDATE
        world.conn.execute("UPDATE audit_event SET actor_user_id = 'x' WHERE seq = 1")
    assert [e.details.get("store") for e in _events(world, A.ERASE_FAILED)] == ["app_db"]
    assert world.erase(USER_A)[0] == E.EXIT_OK  # and the next erase works


def test_attempted_row_is_written_first_and_aborts_when_it_fails(
    world: World, monkeypatch
) -> None:
    before = _rows(world, USER_A)
    token = _token(world.run("--user", USER_A)[1])

    def boom(self, event):  # only the out-of-transaction erase.attempted append fails
        raise A.AuditError("synthetic audit failure")

    monkeypatch.setattr(A.AuditLog, "_append", boom)
    code, text = world.run("--user", USER_A, "--confirm", token, "--retype", USER_A)
    monkeypatch.undo()
    assert code == E.EXIT_FAIL and "Nothing was deleted" in text
    _untouched(world, before)
    assert not _events(world, A.ERASE_ATTEMPTED)
    with pytest.raises(A.DeleteEventRefusedError):  # not forgeable through the public API
        A.AuditLog(world.conn).build(A.ERASE_ATTEMPTED, actor_user_id=MAINTAINER,
                                     session_id="s", turn_id="t")  # fmt: skip


def test_second_erase_is_noop_and_audited(world: World) -> None:
    assert world.erase(USER_A)[0] == E.EXIT_OK
    code, text = world.erase(USER_A)
    assert code == E.EXIT_OK and "saved_report             0 deleted" in text
    executed = sorted(_events(world, A.ERASE_EXECUTED), key=lambda e: e.seq)
    assert [e.count for e in executed][1:] == [0] and len(executed) == 2
    assert len(_events(world, A.ERASE_ATTEMPTED)) == 2 and not _events(world, A.ERASE_FAILED)


def test_missing_stores_tolerated(tmp_path: Path) -> None:
    """A data dir with only app.db (no checkpoints, traces, exports, candidates, optional
    tables): the erase runs and creates none of the missing stores."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = open_store(data_dir / "app.db")
    for table in ("report_fts", "report_vector", "user_quota"):  # never-created optional stores
        if table == "report_fts":
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'report_fts'"
            ).fetchall():
                conn.execute(f"DROP TABLE {name}")
        else:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    w = World(data_dir, tmp_path / "no-cases", conn, {})
    try:
        code, text = w.erase(USER_A)
        assert code == E.EXIT_OK, text
        assert len(_events(w, A.ERASE_EXECUTED)) == 1
        assert sorted(p.name for p in data_dir.iterdir() if not p.name.startswith("app.db")) == []
    finally:
        conn.close()


def test_bad_checkpoint_key_refuses_before_any_write(world: World) -> None:
    before = _rows(world, USER_A)
    out = io.StringIO()
    code = E.main(["--as", MAINTAINER, "--user", USER_A, "--data-dir", str(world.data_dir)],
                  conn=world.conn, maintainers=MAINTAINERS, env={}, clock=lambda: T0,
                  cases_dir=world.cases_dir, out=out)  # fmt: skip
    assert code == E.EXIT_REFUSED and "checkpoint store cannot be read" in out.getvalue()
    _untouched(world, before)


def test_file_delete_failure_is_audited_and_reported(world: World, monkeypatch) -> None:
    token = _token(world.run("--user", USER_A)[1])
    real = Path.unlink

    def flaky(self: Path, missing_ok: bool = False) -> None:
        if E.EXPORTS_DIR in self.parts and USER_A in self.name:
            raise PermissionError("synthetic")
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky)
    code, text = world.run("--user", USER_A, "--confirm", token, "--retype", USER_A)
    monkeypatch.undo()
    assert code == E.EXIT_FAIL and "exports FAILED (PermissionError)" in text
    assert f"custom-{USER_A}.md" in text  # named for manual removal
    assert all(n == 0 for n in _rows(world, USER_A).values())  # the DB erase stands
    failed = _events(world, A.ERASE_FAILED)
    assert [e.details.get("store") for e in failed] == ["exports"]
    assert failed[0].details.get("error_type") == "PermissionError"


def test_token_helpers() -> None:
    key = b"\x01" * 32
    tok = E.make_token(key, MAINTAINER, USER_A, "d" * 64, T0)
    assert E.check_token(key, tok, MAINTAINER, USER_A, "d" * 64, T0 + 10)
    assert not E.check_token(key, tok, MAINTAINER, USER_A, "e" * 64, T0)
    assert not E.check_token(key, tok, "other_maint", USER_A, "d" * 64, T0)
    assert not E.check_token(b"\x02" * 32, tok, MAINTAINER, USER_A, "d" * 64, T0)
    assert not E.check_token(key, tok, MAINTAINER, USER_A, "d" * 64, T0 + E.CONFIRM_TTL_S + 1)
    far = f"{int(T0) + 10 * E.CONFIRM_TTL_S}.{'0' * 32}"  # expiry beyond the TTL window
    assert not E.check_token(key, far, MAINTAINER, USER_A, "d" * 64, T0)


def test_legacy_export_matches_on_header_line_only(world: World) -> None:
    """D-228: a flat (pre-D-228) export is matched by its header line, not by a report id
    that appears later in the body. User B's export mentioning A's report id is kept."""
    rid_a = world.users[USER_A].report_id
    rid_b = world.users[USER_B].report_id
    decoy = world.data_dir / E.EXPORTS_DIR / "mentions-other.md"
    decoy.write_text(f"# Synthetic\n\nReport R-{rid_b}, created 2026-01-01."
                     f"\n\nQuoted:\nReport R-{rid_a}, created 2026-01-01.\n")
    code, text = world.erase(USER_A)
    assert code == E.EXIT_OK, text
    assert decoy.exists()
    assert not any(p.exists() for p in world.users[USER_A].files["exports"])
    assert all(p.exists() for p in world.users[USER_B].files["exports"])
