import json

import pytest

from opsfleet_agent.commands.feedback import handle_feedback, parse_args
from opsfleet_agent.obs import tracer as tr
from opsfleet_agent.obs.metrics import metrics_summary
from opsfleet_agent.store import db
from opsfleet_agent.store.feedback import MAX_COMMENT, FeedbackError, FeedbackStore


@pytest.fixture
def store(tmp_path):
    c = db.open_store(tmp_path / "app.db")
    yield FeedbackStore(c)
    c.close()


def _fb(store, args, **kw):
    base = dict(store=store, user_id="u1", session_id="s1", last_turn_id="t1", last_trace_id="tr-1")
    base.update(kw)
    return handle_feedback(args, **base)


def test_feedback_linked_to_trace(store, tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    reply = _fb(store, "down wrong_numbers totals look off", tracer=t)
    assert "recorded" in reply
    (rec,) = store.for_turn("s1", "t1")
    assert (rec.user_id, rec.session_id, rec.turn_id, rec.trace_id) == ("u1", "s1", "t1", "tr-1")
    assert rec.rating == "down" and rec.reason == "wrong_numbers"
    assert rec.comment == "totals look off"
    spans = [json.loads(x) for x in t.path.read_text().splitlines()]
    (sp,) = [s for s in spans if s["name"] == "feedback"]
    assert sp["trace_id"] == "tr-1" and sp["turn_id"] == "t1" and sp["outcome"] == "down"
    assert "totals look off" not in json.dumps(sp)
    # re-rating the same turn replaces, not duplicates
    assert "updated" in _fb(store, "up")
    assert [r.rating for r in store.for_turn("s1", "t1")] == ["up"]


def test_feedback_explicit_turn(store):
    _fb(store, "up", turn_id="t0", trace_id="tr-0")
    (rec,) = store.for_turn("s1", "t0")
    assert rec.trace_id == "tr-0"


def test_feedback_nothing_to_rate(store):
    assert _fb(store, "up", last_turn_id=None) == "Nothing to rate yet."
    assert store.counts()["total"] == 0


def test_feedback_comment_redacted(store):
    _fb(store, "down call me on +1 415 555 2671 or mail jane.doe@example.com key=abc123secret")
    (rec,) = store.for_turn("s1", "t1")
    assert "example.com" not in rec.comment and "415" not in rec.comment
    assert "abc123secret" not in rec.comment
    raw = store.conn.execute("SELECT comment FROM feedback").fetchone()[0]
    assert "jane" not in raw


def test_feedback_comment_bounded(store):
    _fb(store, "down " + "x" * 5000)
    (rec,) = store.for_turn("s1", "t1")
    assert len(rec.comment) <= MAX_COMMENT + len("...[truncated]")


def test_feedback_scrubber_injectable_and_fails_closed(store):
    _fb(store, "down hello", scrubber=lambda s: s.upper())
    assert store.for_turn("s1", "t1")[0].comment == "HELLO"

    def boom(_):
        raise RuntimeError("detector down")

    reply = _fb(store, "down secret@x.com", scrubber=boom, turn_id="t2")
    assert "dropped" in reply
    assert store.for_turn("s1", "t2")[0].comment is None


@pytest.mark.parametrize("args", ["", "sideways", "5", "up slow"])
def test_feedback_validation(store, args):
    reply = _fb(store, args)
    assert "Usage" in reply or "Rating" in reply or "reason" in reply
    assert store.counts()["total"] == 0


def test_parse_args():
    assert parse_args("DOWN misunderstood why").reason == "misunderstood"
    assert parse_args("down not-a-reason text").comment == "not-a-reason text"
    assert parse_args("up").comment is None


def test_store_rejects_bad_values(store):
    with pytest.raises(FeedbackError):
        store.add(user_id="u", session_id="s", turn_id="t", trace_id=None, rating="meh")
    with pytest.raises(FeedbackError):
        store.add(
            user_id="u", session_id="s", turn_id="t", trace_id=None, rating="down", reason="x"
        )
    with pytest.raises(FeedbackError):
        store.add(user_id="", session_id="s", turn_id="t", trace_id=None, rating="up")


def test_metrics_summary_includes_feedback(store, tmp_path):
    t = tr.Tracer(tmp_path, "s1")
    t.record("turn", outcome="ok", turn_id="t1", duration_ms=10)
    _fb(store, "up")
    _fb(store, "down slow", turn_id="t2", trace_id="tr-2")
    _fb(store, "up", user_id="u2", session_id="other", turn_id="t9")
    out = metrics_summary(tmp_path, "s1", feedback=store)
    assert "Feedback: up=1, down=1, thumbs-down rate 50.0%" in out
    assert "turn t2, trace tr-2 reason=slow" in out
    assert "up=2" in metrics_summary(tmp_path, None, feedback=store)
    assert "Feedback" not in metrics_summary(tmp_path, "s1")


def test_delete_user(store):
    _fb(store, "up")
    assert store.delete_user("u1") == 1 and store.counts()["total"] == 0
