"""Semantic search over saved reports, fused with the FTS ranks (iteration 38, AC-21.13/14).

One vector per report in ``report_vector`` (same ``app.db``), embedded with the Golden model
(:mod:`golden.seed` client, D-143 prefixes). Rules enforced here, in code:

* **Never in a transaction, never fails a save.** :meth:`SemanticIndex.index` runs after the
  report's transaction has committed (save and rename); any embedding or write failure is
  logged by class name and counted, and the report stays saved (D-209). Search then simply
  finds it through FTS until a later search backfills the vector.
* **No network in a migration.** Migration 6 is DDL only. Reports saved before it (or whose
  embedding failed) are backfilled lazily by the next search of their owner, in the same
  single embed call as the query and at most :data:`MAX_BACKFILL` of them (D-210).
* **Only stored, guarded text is embedded.** The text is built from the saved row (which
  already passed the output guard and the secret scrub at save time) and passes the PII
  regex scrubber again; the query is whitespace-collapsed, capped at ``MAX_QUERY_CHARS`` and
  scrubbed the same way before it leaves the process (D-211).
* **Owner first, scope before scoring.** Vectors are read with an owner filter in SQL, joined
  to ``saved_report`` on owner and id, and only for the candidate ids the caller passes,
  which :func:`reports.library.search_reports` has already filtered by scope, tags and dates.
  Cosine is computed for nothing else.
* **Bounded.** At most one embed call per search (query plus backfill), :data:`MAX_CANDIDATES`
  scored, :data:`MAX_HITS` returned; a small LRU keeps recent query vectors.
* **Deleted with the report.** ``report_vector`` is a plain declared dependent of the audited
  delete (``delete.flow.setup_delete``): same transaction, exact change count, audit first.
  The table has no FK and no trigger, and ``secure_delete`` zeroes the freed pages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from opsfleet_agent.golden.seed import (
    DEFAULT_MIN_SCORE,
    MAX_QUERY_CHARS,
    QUERY_LRU_SIZE,
    Embedder,
    _cosine,
    _valid_vector,
)
from opsfleet_agent.guards.pii_regex import scrub
from opsfleet_agent.store.db import write_tx
from opsfleet_agent.store.vector_schema import VECTOR_KEY, VECTOR_MIGRATION, VECTOR_TABLE

__all__ = [
    "MAX_BACKFILL",
    "MAX_HITS",
    "RRF_K",
    "VECTOR_KEY",
    "VECTOR_MIGRATION",
    "VECTOR_TABLE",
    "SemanticIndex",
    "document_text",
    "rrf_fuse",
]

log = logging.getLogger(__name__)

RRF_K: Final = 60
MAX_CANDIDATES: Final = 200  # the owner's newest MAX_LIST reports, never more
MAX_HITS: Final = 50  # semantic list length fed into RRF
MAX_BACKFILL: Final = 16  # missing/stale report vectors embedded per search, with the query
MAX_DOC_CHARS: Final = 2000

# Upsert only while the report still exists for that owner: a delete racing the embed call
# leaves no orphan vector behind.
_UPSERT: Final = (
    f"INSERT INTO {VECTOR_TABLE} ({VECTOR_KEY}, owner_user_id, model, dims, vector, content_hash) "
    "SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS "
    "(SELECT 1 FROM saved_report WHERE report_id = ? AND owner_user_id = ?) "
    f"ON CONFLICT ({VECTOR_KEY}) DO UPDATE SET owner_user_id = excluded.owner_user_id, "
    "model = excluded.model, dims = excluded.dims, vector = excluded.vector, "
    "content_hash = excluded.content_hash"
)


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split())


def document_text(rec: Any) -> str:
    """What is embedded for a report: title, the Summary section (or the start of the body)
    and the tags, from the stored (already guarded) row, PII-scrubbed again and capped."""
    sections = rec.sections if isinstance(rec.sections, dict) else {}
    summary = sections.get("Summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = str(rec.body_markdown)[:MAX_DOC_CHARS]
    tags = " ".join(str(t) for t in (rec.tags or ()))
    text = " ".join(f"{rec.title}\n{summary}\n{tags}".split())[:MAX_DOC_CHARS]
    return scrub(text).text


def pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: Any, dim: int) -> list[float] | None:
    if not isinstance(blob, bytes) or len(blob) != 4 * dim:
        return None
    return list(struct.unpack(f"<{dim}f", blob))


def rrf_fuse(*rankings: Sequence[str], k: int = RRF_K) -> list[str]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank), rank from 1.
    Ties: the better best rank first, then the id (deterministic)."""
    score: dict[str, float] = {}
    best: dict[str, int] = {}
    for ranking in rankings:
        for rank, rid in enumerate(dict.fromkeys(ranking), start=1):
            score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank)
            best[rid] = min(best.get(rid, rank), rank)
    return sorted(score, key=lambda rid: (-score[rid], best[rid], rid))


@dataclass
class SemanticIndex:
    """The report embedder plus its model identity. ``embedder`` None = semantic off."""

    embedder: Embedder | None
    model: str
    dim: int
    query_prefix: str = ""
    document_prefix: str = ""
    min_score: float = DEFAULT_MIN_SCORE
    failures: int = 0  # embedding/write failures since start (save never fails on them)
    _queries: OrderedDict[str, list[float]] = field(default_factory=OrderedDict, repr=False)

    def content_hash(self, text: str) -> str:
        parts: list[Any] = [_norm(text), self.model, self.dim]
        if self.query_prefix or self.document_prefix:
            parts.append({"qp": self.query_prefix, "dp": self.document_prefix})
        blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # --- write side --------------------------------------------------------------------------

    def index(self, conn: sqlite3.Connection, rec: Any) -> bool:
        """Embed one saved report and store its vector. Called after the report's transaction
        committed. Never raises: False when the vector is not (re)written."""
        if self.embedder is None:
            return False
        try:
            text = document_text(rec)
            chash = self.content_hash(text)
            if self._current(conn, rec.report_id, rec.owner_user_id) == chash:
                return True
            out = self.embedder.embed([self.document_prefix + text])
            if len(out) != 1 or not _valid_vector(out[0], self.dim):
                raise ValueError("malformed embedding response")
            self._write(conn, [(rec.report_id, rec.owner_user_id, out[0], chash)])
            return True
        except Exception as exc:  # noqa: BLE001 - search degrades; the save stands (D-209)
            self.failures += 1
            log.warning("report embedding failed: %s", type(exc).__name__)
            return False

    def _current(self, conn: sqlite3.Connection, report_id: str, owner: str) -> str | None:
        row = conn.execute(
            f"SELECT content_hash FROM {VECTOR_TABLE} WHERE {VECTOR_KEY} = ? AND owner_user_id = ? "
            "AND model = ? AND dims = ?",
            (str(report_id), str(owner), self.model, int(self.dim)),
        ).fetchone()
        return str(row[0]) if row else None

    def _write(
        self, conn: sqlite3.Connection, items: Iterable[tuple[str, str, Sequence[float], str]]
    ) -> None:
        with write_tx(conn):
            for rid, owner, vec, chash in items:
                rid, owner = str(rid), str(owner)
                vals = [float(x) for x in vec]
                conn.execute(
                    _UPSERT, (rid, owner, self.model, int(self.dim), pack(vals), chash, rid, owner)
                )

    # --- query side --------------------------------------------------------------------------

    def search(
        self, conn: sqlite3.Connection, owner: str, candidates: Sequence[Any], text: str
    ) -> list[tuple[str, float]] | None:
        """``(report_id, cosine)`` best first among ``candidates`` (the caller's in-scope,
        filtered rows of ``owner``), at most :data:`MAX_HITS`, cosine >= ``min_score``.
        None when semantic search is unavailable (no embedder, provider error, bad reply):
        the caller degrades to the FTS / word-match ranking. At most one embed call."""
        if self.embedder is None:
            return None
        try:
            return self._search(conn, str(owner), list(candidates)[:MAX_CANDIDATES], text)
        except Exception as exc:  # noqa: BLE001 - any failure degrades, never raises
            log.warning("semantic report search unavailable: %s", type(exc).__name__)
            return None

    def _search(
        self, conn: sqlite3.Connection, owner: str, cands: list[Any], text: str
    ) -> list[tuple[str, float]]:
        q = scrub(" ".join(str(text).split())[:MAX_QUERY_CHARS]).text
        if not q.strip():
            return []
        cands = [r for r in cands if str(r.owner_user_id) == owner]  # belt and braces
        stored = self._stored(conn, owner, [r.report_id for r in cands])
        hashes = {r.report_id: self.content_hash(document_text(r)) for r in cands}
        missing = [
            r for r in cands if stored.get(r.report_id, (None, ""))[1] != hashes[r.report_id]
        ]
        backfill = missing[:MAX_BACKFILL]
        qkey = hashlib.sha256(f"{self.model}|{self.dim}|{_norm(q)}".encode()).hexdigest()
        qvec = self._queries.get(qkey)
        if qvec is None or backfill:
            texts = ([self.query_prefix + q] if qvec is None else []) + [
                self.document_prefix + document_text(r) for r in backfill
            ]
            out = self.embedder.embed(texts)  # type: ignore[union-attr]
            if len(out) != len(texts) or not all(_valid_vector(v, self.dim) for v in out):
                raise ValueError("malformed embedding response")
            vecs = [[float(x) for x in v] for v in out]
            if qvec is None:
                qvec, vecs = vecs[0], vecs[1:]
                self._queries[qkey] = qvec
                while len(self._queries) > QUERY_LRU_SIZE:
                    self._queries.popitem(last=False)
            new = [
                (r.report_id, owner, v, hashes[r.report_id])
                for r, v in zip(backfill, vecs, strict=True)
            ]
            for rid, _o, v, h in new:
                stored[rid] = (v, h)
            try:
                self._write(conn, new)
            except Exception as exc:  # noqa: BLE001 - scoring still uses the fresh vectors
                self.failures += 1
                log.warning("report vector backfill not written: %s", type(exc).__name__)
        else:
            self._queries.move_to_end(qkey)
        scored = []
        for r in cands:
            vec, chash = stored.get(r.report_id, (None, ""))
            if vec is not None and chash == hashes[r.report_id]:
                score = _cosine(qvec, vec)
                if score >= self.min_score:
                    scored.append((r.report_id, score))
        scored.sort(key=lambda h: (-h[1], h[0]))
        return scored[:MAX_HITS]

    def _stored(
        self, conn: sqlite3.Connection, owner: str, ids: Sequence[str]
    ) -> dict[str, tuple[list[float] | None, str]]:
        """The owner's stored vectors for ``ids`` (owner filtered in SQL on both tables)."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT v.{VECTOR_KEY}, v.vector, v.content_hash FROM {VECTOR_TABLE} v "
            "JOIN saved_report s "
            "ON s.report_id = v.report_id AND s.owner_user_id = v.owner_user_id "
            "WHERE v.owner_user_id = ? AND v.model = ? AND v.dims = ? "
            f"AND v.{VECTOR_KEY} IN ({marks})",
            (owner, self.model, int(self.dim), *ids),
        ).fetchall()
        out: dict[str, tuple[list[float] | None, str]] = {}
        for rid, blob, chash in rows:
            vec = unpack(blob, self.dim)
            out[str(rid)] = (vec, str(chash) if vec is not None else "")
        return out


def build_semantic_index(settings: Any, embedder: Embedder | None = None) -> SemanticIndex | None:
    """The production index (lazy provider embedder: no network at construction). None when
    the settings name no embedding model, so search stays on FTS."""
    try:
        from opsfleet_agent.graph.providers import build_embedder, embedding_prefixes

        model, dim = settings.embedding_model, int(settings.embedding_dimensionality)
        if not model or dim < 1:
            return None
        qp, dp = embedding_prefixes(settings)
        return SemanticIndex(embedder or build_embedder(settings), model, dim, qp, dp)
    except Exception as exc:  # noqa: BLE001 - semantic search is optional
        log.warning("semantic report search disabled: %s", type(exc).__name__)
        return None
