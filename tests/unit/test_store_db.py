import threading

import pytest

from opsfleet_agent.store import db


def test_secure_delete_on_every_connection(tmp_path):
    p = tmp_path / "app.db"
    conns = [db.open_store(p)] + [db.connect(p) for _ in range(3)]
    for c in conns:
        assert c.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        c.close()


def test_migration_v1_and_idempotent(tmp_path):
    p = tmp_path / "app.db"
    c = db.open_store(p)
    latest = max(v for v, _ in db.MIGRATIONS)
    assert db.current_version(c) == latest
    assert db.migrate(c) == latest
    assert c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(db.MIGRATIONS)
    c.close()


def test_audit_log_is_a_migration(tmp_path):
    """Iteration 21 fold: the audit table and its marker come from migration 2."""
    c = db.open_store(tmp_path / "app.db")
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_event'"
    ).fetchone()
    assert c.execute("SELECT COUNT(*) FROM meta WHERE key='audit_event.created'").fetchone()[0] == 1
    c.close()


def test_write_tx_rolls_back_on_error(tmp_path):
    c = db.open_store(tmp_path / "app.db")
    try:
        with db.write_tx(c):
            c.execute("INSERT INTO meta VALUES ('a','1')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert c.execute("SELECT COUNT(*) FROM meta WHERE key='a'").fetchone()[0] == 0
    c.close()


def test_two_concurrent_writers_do_not_corrupt(tmp_path):
    p = tmp_path / "app.db"
    db.open_store(p).close()
    errors: list[BaseException] = []

    def writer(tag: str) -> None:
        try:
            c = db.connect(p)
            for i in range(50):
                with db.write_tx(c):
                    c.execute("INSERT INTO meta VALUES (?, ?)", (f"{tag}{i}", "x"))
            c.close()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    c = db.connect(p)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT COUNT(*) FROM meta WHERE value='x'").fetchone()[0] == 100
    c.close()


def test_files_are_owner_only(tmp_path):
    p = tmp_path / "app.db"
    c = db.open_store(p)
    with db.write_tx(c):
        c.execute("INSERT INTO meta VALUES ('a','1')")
    for suffix in ("", "-wal", "-shm"):
        f = tmp_path / f"app.db{suffix}"
        assert f.exists(), suffix
        assert (f.stat().st_mode & 0o777) == 0o600, suffix
    c.close()
    p.chmod(0o644)
    db.connect(p).close()
    assert (p.stat().st_mode & 0o777) == 0o600


def test_secure_delete_leaves_no_marker_in_db_or_wal(tmp_path):
    p = tmp_path / "app.db"
    c = db.open_store(p)
    marker = "SYNTHETIC-MARKER-7f3a9c1e-" + "Z" * 20
    with db.write_tx(c):
        c.execute("INSERT INTO meta VALUES ('m', ?)", (marker,))
    db.checkpoint_truncate(c)
    assert marker.encode() in p.read_bytes()  # sanity: it was written
    with db.write_tx(c):
        c.execute("DELETE FROM meta WHERE key='m'")
    db.checkpoint_truncate(c)
    assert marker.encode() not in p.read_bytes()
    wal = tmp_path / "app.db-wal"
    assert not wal.exists() or marker.encode() not in wal.read_bytes()
    c.close()


def test_rejects_newer_schema(tmp_path):
    p = tmp_path / "app.db"
    c = db.open_store(p)
    with db.write_tx(c):
        c.execute("INSERT INTO schema_migrations (version) VALUES (99)")
    c.close()
    with pytest.raises(db.SchemaTooNewError):
        db.open_store(p)
    assert issubclass(db.SchemaTooNewError, db.StoreError)
