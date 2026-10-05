"""Golden query seed and top-k retrieval (HLD 6.1, AC-26.1..26.4, iteration 31).

A *trio* is a synthetic question, the SQL that answers it and a one-line report pattern (no
figures). The seed lives in ``config/golden_seed.yaml``. Everything here is enforced in code:

* **Load** (:func:`load_seed`): shape and size bounds, duplicate ids, the SQL policy
  (``guards.sql_policy.check_sql``), a PII scan (``guards.pii_regex.scrub``), an injection scan
  (``guards.input._scan``) and a no-figures rule for the summary. A trio that fails is rejected
  with a reason code and is never cleaned. ``strict=False`` (default) skips it with a logged
  warning (AC-26.1); ``strict=True`` raises :class:`GoldenSeedError` (the HLD's "a bad seed
  fails at startup"). Logs carry the trio id and the reason code only, never the text.
* **Scope**: a trio tagged ``agnostic`` is offered to every scope; a brand-tied trio only when
  the caller's scope covers all its brands (``context.covers``, AC-26.2).
* **Retrieval** (:meth:`GoldenIndex.retrieve`): cosine top-k (k=3, minimum score 0.6) over the
  eligible trios. At most ONE embedding call per retrieval (the query plus any trio vector not
  yet cached, in a single batch), a single attempt, no retry. Trio vectors are cached on disk
  keyed by sha256(normalised trio, model id, dimension); query vectors in a bounded process
  LRU.
* **Degrade** (AC-26.4): if the embedder is missing, raises, or returns something malformed,
  the result is ``unavailable=True`` (the caller records ``golden.unavailable``). With
  ``degrade="none"`` (default, per AC-26.4) there are no examples; with ``degrade="lexical"``
  a deterministic token-overlap top-k is returned instead. Never an exception.

The embedding model is unverified (owner L0 spike pending); nothing here depends on its name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import sqlglot
import yaml
from sqlglot import exp

from opsfleet_agent.graph.context import (
    KIND_GOLDEN,
    MAX_STORE_ITEMS,
    StoreItem,
    covers,
    snapshot_of,
)
from opsfleet_agent.guards.input import scan_injection
from opsfleet_agent.guards.pii_regex import scrub
from opsfleet_agent.guards.scope import ProductScope, ScopedQuery, ScopeError, apply_scope
from opsfleet_agent.guards.sql_policy import check_sql, regenerate_sql

log = logging.getLogger(__name__)

AGNOSTIC: Final = "agnostic"
DEFAULT_K: Final = 3
DEFAULT_MIN_SCORE: Final = 0.6
LEXICAL_MIN_SCORE: Final = 0.2
MAX_TRIOS: Final = 50
MAX_SEED_BYTES: Final = 256_000
MAX_QUESTION_CHARS: Final = 500
MAX_SUMMARY_CHARS: Final = 1000
MAX_SQL_CHARS: Final = 8000
MAX_TAGS: Final = 10
MAX_BRANDS: Final = 10
MAX_QUERY_CHARS: Final = 1000
MAX_CACHE_ENTRIES: Final = 1000
MAX_CACHE_BYTES: Final = 32_000_000
QUERY_LRU_SIZE: Final = 64
CACHE_FILE: Final = "golden_embeddings.json"

_ID_RE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TOKEN_RE: Final = re.compile(r"[a-z0-9]+")
_STOPWORDS: Final = frozenset(
    "a an and are by do does for how in is it of on or per the to was what which with".split()
)


class GoldenSeedError(Exception):
    """The seed is unreadable or (strict mode) contains a trio that fails validation."""


class Embedder(Protocol):
    """Anything that turns texts into vectors. One call per retrieval; may raise."""

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(frozen=True)
class GoldenTrio:
    trio_id: str
    version: int
    question: str
    sql: str
    report_summary: str
    tags: tuple[str, ...] = ()
    brands: tuple[str, ...] = ()  # empty = agnostic

    @property
    def ref(self) -> str:
        return f"{self.trio_id}@{self.version}"

    @property
    def agnostic(self) -> bool:
        return not self.brands

    @property
    def scope(self) -> ProductScope:
        """The scope a caller needs to see this trio (brand-tied only)."""
        return ProductScope.for_brands(self.brands)

    def embed_text(self) -> str:
        return f"{self.question}\n{self.report_summary}"

    def content_key(
        self, model: str, dim: int, query_prefix: str = "", document_prefix: str = ""
    ) -> str:
        """sha256 of the normalised trio plus model id plus dimension (AC-26.3).

        D-143: non-empty embedding prefixes join the key, so changing a prefix invalidates the
        cached vectors. Empty prefixes (Gemini) give the original key byte for byte."""
        norm = {
            "id": self.trio_id,
            "v": self.version,
            "q": _norm(self.question),
            "sql": _norm(self.sql),
            "s": _norm(self.report_summary),
            "tags": sorted(_norm(t) for t in self.tags),
            "brands": sorted(self.brands),
        }
        parts: list[Any] = [norm, model, dim]
        if query_prefix or document_prefix:
            parts.append({"qp": query_prefix, "dp": document_prefix})
        blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class SeedLoad:
    trios: tuple[GoldenTrio, ...]
    rejected: tuple[tuple[str, str], ...]  # (trio_id or "#<index>", reason code)


def default_seed_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "golden_seed.yaml"


# --- loading and validation ---------------------------------------------------------------------


def load_seed(path: Path | str | None = None, *, strict: bool = False) -> SeedLoad:
    """Load and validate the seed. See the module docstring for the rules."""
    p = Path(path) if path is not None else default_seed_path()
    try:
        if p.stat().st_size > MAX_SEED_BYTES:
            raise GoldenSeedError(f"{p.name} is too large (max {MAX_SEED_BYTES} bytes).")
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except GoldenSeedError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise GoldenSeedError(f"{p.name} cannot be read: {type(exc).__name__}.") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("trios"), list):
        raise GoldenSeedError(f"{p.name} is invalid: expected a mapping with a 'trios' list.")
    entries = raw["trios"]
    if len(entries) > MAX_TRIOS:
        raise GoldenSeedError(f"{p.name} is invalid: more than {MAX_TRIOS} trios.")

    known = _known_brands(entries)
    trios: list[GoldenTrio] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        label = f"#{i}"
        if (
            isinstance(entry, Mapping)
            and isinstance(entry.get("trio_id"), str)
            and _ID_RE.match(entry["trio_id"])
        ):
            label = entry["trio_id"]
        trio, reason = _validate(entry, known)
        if trio is not None and trio.trio_id in seen:
            trio, reason = None, "duplicate_id"
        if trio is None:
            if strict:
                raise GoldenSeedError(f"golden trio {label} rejected: {reason}")
            log.warning("golden trio %s rejected: %s", label, reason)
            rejected.append((label, reason))
            continue
        seen.add(trio.trio_id)
        trios.append(trio)
    return SeedLoad(tuple(trios), tuple(rejected))


def _str_list(value: Any, limit: int) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > limit:
        return None
    if not all(isinstance(v, str) and 0 < len(v.strip()) <= 64 for v in value):
        return None
    return tuple(v.strip() for v in value)


def _known_brands(entries: Sequence[Any]) -> frozenset[str]:
    """Brands named by the seed itself plus config/profiles.yaml (best effort, offline).

    Always reads the default ``config/profiles.yaml`` next to the default seed, whatever seed
    path is being loaded; a missing or unreadable file just means seed-only brands.
    """
    names: set[str] = set()
    for e in entries:
        if isinstance(e, Mapping) and isinstance(e.get("brands"), list):
            names.update(b.strip() for b in e["brands"] if isinstance(b, str))
    try:
        prof = yaml.safe_load((default_seed_path().parent / "profiles.yaml").read_text("utf-8"))
        for p in (prof or {}).get("profiles", []) if isinstance(prof, Mapping) else []:
            if isinstance(p, Mapping) and isinstance(p.get("brands"), list):
                names.update(b for b in p["brands"] if isinstance(b, str))
    except Exception:  # noqa: BLE001 - optional input
        pass
    names.discard(AGNOSTIC)
    return frozenset(n for n in names if n)


_BRAND_PROJECTION: Final = (
    exp.Select,
    exp.Group,
    exp.Ordered,
    exp.Alias,
    exp.Distinct,
    exp.Count,
    exp.Order,
)


def _sql_brand_literals(sql: str) -> set[str] | None:
    """Brand literals used by predicates on a ``brand`` column, or None if any use is unsafe.

    A predicate must be a direct ``brand = 'x'`` or ``brand IN ('x', ...)`` over plain string
    literals. Function-wrapped, LIKE, <>, column-to-column, subquery-fed and CTE-fed uses return
    None (the caller rejects). A bare column in SELECT / GROUP BY / ORDER BY is not a predicate.
    """
    out: set[str] = set()
    for col in sqlglot.parse_one(sql, dialect="bigquery").find_all(exp.Column):
        if col.name.casefold() != "brand":
            continue
        parent = col.parent
        if isinstance(parent, _BRAND_PROJECTION):
            continue
        if isinstance(parent, exp.In):
            lits = parent.expressions
            if parent.args.get("query") is not None or parent.args.get("unnest") is not None:
                return None
            if parent.this is not col or not lits:
                return None
        elif isinstance(parent, exp.EQ):
            other = parent.right if parent.left is col else parent.left
            lits = [other]
        else:
            return None
        if not all(isinstance(x, exp.Literal) and x.is_string for x in lits):
            return None
        out.update(x.name for x in lits)
    return out


_LITERAL_RE: Final = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"")


def _names_brand(text: str, known: frozenset[str]) -> bool:
    low = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(b.casefold())}(?!\w)", low) for b in known)


def _validate(entry: Any, known: frozenset[str] = frozenset()) -> tuple[GoldenTrio | None, str]:
    if not isinstance(entry, Mapping):
        return None, "shape"
    tid, ver = entry.get("trio_id"), entry.get("version")
    q, sql, summ = entry.get("question"), entry.get("sql"), entry.get("report_summary")
    if not isinstance(tid, str) or not _ID_RE.match(tid):
        return None, "shape"
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        return None, "shape"
    if not all(isinstance(v, str) and v.strip() for v in (q, sql, summ)):
        return None, "shape"
    assert isinstance(q, str) and isinstance(sql, str) and isinstance(summ, str)
    if len(q) > MAX_QUESTION_CHARS or len(summ) > MAX_SUMMARY_CHARS or len(sql) > MAX_SQL_CHARS:
        return None, "too_long"
    tags = _str_list(entry.get("tags", []), MAX_TAGS)
    brands_raw = _str_list(entry.get("brands"), MAX_BRANDS)
    if tags is None or brands_raw is None or not brands_raw:
        return None, "shape"
    if AGNOSTIC in brands_raw:
        if len(brands_raw) != 1:
            return None, "shape"
        brands: tuple[str, ...] = ()
    else:
        brands = tuple(sorted(set(brands_raw)))
        if any(k != b and k.casefold() == b.casefold() for b in brands for k in known):
            return None, "brand_case"
        try:
            ProductScope.for_brands(brands)
        except ScopeError:
            return None, "shape"
    q, sql, summ = q.strip(), sql.strip(), summ.strip()

    decision = check_sql(sql)
    if not decision.allowed:
        return None, f"sql_policy:{decision.reason_code}"
    if "\\" in re.sub(r"\\[\\']", "", sql):
        return None, "sql_escape"  # only \' and \\ are allowed escapes
    if not isinstance(apply_scope(sql, ProductScope.all()), ScopedQuery):
        return None, "sql_scope"
    prose = [q, summ, *tags]
    if any(scrub(t).redacted for t in [*prose, sql]):
        return None, "pii"
    # Raw SQL text (comments included) and every string literal are scanned for injection.
    literals = [a or b for a, b in _LITERAL_RE.findall(sql)]
    for text in [*prose, sql, *literals]:
        rule = scan_injection(text)
        if rule is not None:
            return None, f"injection:{rule}"
    if any(ch.isnumeric() for ch in summ + q):
        return None, "figures"
    # The prompt gets the canonical SQL (comments dropped), never the author's text.
    norm_sql = regenerate_sql(sql)
    used = _sql_brand_literals(norm_sql)
    if used is None or not used <= set(brands) or (not brands and used):
        return None, "brand_mismatch"
    # No trio may name a brand it does not declare (an agnostic trio names none at all).
    foreign = known - set(brands)
    texts = [*prose, *(a or b for a, b in _LITERAL_RE.findall(norm_sql))]
    if any(_names_brand(t, foreign) for t in texts):
        return None, "brand_mismatch"
    return GoldenTrio(tid, ver, q, norm_sql, summ, tags, brands), "ok"


# --- embedding client ---------------------------------------------------------------------------


class GenaiEmbedder:
    """Real embedder over google-genai. Lazy client, one attempt, bounded timeout, no retry."""

    def __init__(self, api_key: str, model: str, dimensionality: int, *, timeout_s: float = 10.0):
        self._api_key = api_key
        self._model = model
        self._dim = dimensionality
        self._timeout_ms = int(timeout_s * 1000)
        self._client: Any = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        from google import genai
        from google.genai import types

        if self._client is None:
            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(
                    timeout=self._timeout_ms,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        resp = self._client.models.embed_content(
            model=self._model,
            contents=list(texts),
            config=types.EmbedContentConfig(output_dimensionality=self._dim),
        )
        return [list(e.values or []) for e in (resp.embeddings or [])]


# --- vector cache -------------------------------------------------------------------------------


class _VectorCache:
    """Trio vectors on disk, keyed by content hash. Corrupt or unwritable = an empty cache."""

    def __init__(self, directory: Path | None, dim: int):
        self._path = (directory / CACHE_FILE) if directory is not None else None
        self._dim = dim
        self._mem: dict[str, list[float]] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            if self._path.stat().st_size > MAX_CACHE_BYTES:
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            entries = data["entries"]
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 - any bad cache is an empty cache
            log.warning("golden embedding cache ignored: %s", type(exc).__name__)
            return
        if not isinstance(entries, dict):
            return
        for key, vec in list(entries.items())[:MAX_CACHE_ENTRIES]:
            ok = _valid_vector(vec, self._dim)
            if isinstance(key, str) and ok:
                self._mem[key] = [float(x) for x in vec]

    def get(self, key: str) -> list[float] | None:
        return self._mem.get(key)

    def put_many(self, items: Mapping[str, list[float]], keep: frozenset[str]) -> None:
        """Add ``items`` and drop every key not in ``keep`` (stale trios), then persist."""
        if not items:
            return
        self._mem.update(items)
        self._mem = {k: v for k, v in self._mem.items() if k in keep}
        while len(self._mem) > MAX_CACHE_ENTRIES:
            self._mem.pop(next(iter(self._mem)))
        if self._path is None:
            return
        tmp = ""
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".golden-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "entries": self._mem}, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("golden embedding cache not written: %s", type(exc).__name__)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _valid_vector(vec: Any, dim: int) -> bool:
    return (
        isinstance(vec, Sequence)
        and not isinstance(vec, str)
        and len(vec) == dim
        and all(
            isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x) for x in vec
        )
    )


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RE.findall(text.casefold()) if t not in _STOPWORDS)


# --- retrieval ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    trio: GoldenTrio
    score: float


@dataclass(frozen=True)
class Retrieval:
    hits: tuple[Hit, ...] = ()
    unavailable: bool = False  # AC-26.4: record golden.unavailable in the trace
    mode: Literal["embedding", "lexical", "none"] = "none"

    @property
    def trio_refs(self) -> list[tuple[str, float]]:
        """``[trio_id@version, score]`` for the trace (AC-26.3)."""
        return [(h.trio.ref, round(h.score, 4)) for h in self.hits]


@dataclass
class GoldenIndex:
    """Scope-filtered cosine top-k over validated trios. See the module docstring."""

    trios: Sequence[GoldenTrio]
    embedder: Embedder | None
    model: str
    dimensionality: int
    cache_dir: Path | None = None
    k: int = DEFAULT_K
    min_score: float = DEFAULT_MIN_SCORE
    degrade: Literal["none", "lexical"] = "none"
    # D-143: task prefixes some embedding models need (nomic: "search_query: " and
    # "search_document: "). Empty for Gemini, so its embed texts are unchanged.
    query_prefix: str = ""
    document_prefix: str = ""
    _cache: _VectorCache = field(init=False, repr=False)
    _queries: OrderedDict[str, list[float]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.trios = tuple(self.trios)
        # k is never trusted: clamp to 1..MAX_STORE_ITEMS (assemble_context renders no more)
        k = self.k if isinstance(self.k, int) and not isinstance(self.k, bool) else DEFAULT_K
        self.k = max(1, min(k, MAX_STORE_ITEMS))
        self._cache = _VectorCache(self.cache_dir, self.dimensionality)
        self._queries = OrderedDict()

    def eligible(self, scope: ProductScope) -> list[GoldenTrio]:
        """AC-26.2: agnostic trios for anyone; brand-tied only when the scope covers them."""
        return [t for t in self.trios if t.agnostic or covers(scope, snapshot_of(t.scope))]

    def retrieve(self, question: str, scope: ProductScope) -> Retrieval:
        """Top-k eligible trios for ``question``. Never raises; at most one embed call."""
        if not isinstance(scope, ProductScope):
            return Retrieval((), True, "none")
        q = " ".join(question.split())[:MAX_QUERY_CHARS] if isinstance(question, str) else ""
        try:
            pool = self.eligible(scope)
            if not q or not pool or self.k < 1:
                return Retrieval()
            try:
                hits = self._embedding_hits(q, pool)
            except Exception as exc:  # noqa: BLE001 - any provider failure degrades (AC-26.4)
                log.warning("golden retrieval unavailable: %s", type(exc).__name__)
                return self._degraded(q, pool)
        except Exception as exc:  # noqa: BLE001 - retrieve never raises
            log.warning("golden retrieval failed: %s", type(exc).__name__)
            return Retrieval((), True, "none")
        return Retrieval(tuple(hits), False, "embedding")

    def _key(self, t: GoldenTrio) -> str:
        return t.content_key(
            self.model, self.dimensionality, self.query_prefix, self.document_prefix
        )

    def _embedding_hits(self, q: str, pool: list[GoldenTrio]) -> list[Hit]:
        if self.embedder is None:
            raise RuntimeError("no embedder")
        qkey = hashlib.sha256(f"{self.model}|{self.dimensionality}|{_norm(q)}".encode()).hexdigest()
        qvec = self._queries.get(qkey)
        keys = {t.trio_id: self._key(t) for t in pool}
        missing = [t for t in pool if self._cache.get(keys[t.trio_id]) is None]
        if qvec is None or missing:
            texts = ([self.query_prefix + q] if qvec is None else []) + [
                self.document_prefix + t.embed_text() for t in missing
            ]
            out = self.embedder.embed(texts)
            if len(out) != len(texts) or not all(
                _valid_vector(v, self.dimensionality) for v in out
            ):
                raise ValueError("malformed embedding response")
            vecs = [[float(x) for x in v] for v in out]
            if qvec is None:
                qvec, vecs = vecs[0], vecs[1:]
                self._queries[qkey] = qvec
                while len(self._queries) > QUERY_LRU_SIZE:
                    self._queries.popitem(last=False)
            self._cache.put_many(
                {keys[t.trio_id]: v for t, v in zip(missing, vecs, strict=True)},
                frozenset(self._key(t) for t in self.trios),
            )
        else:
            self._queries.move_to_end(qkey)
        scored = []
        for t in pool:
            tvec = self._cache.get(keys[t.trio_id])
            if tvec is not None:
                scored.append(Hit(t, _cosine(qvec, tvec)))
        return _top_k(scored, self.k, self.min_score)

    def _degraded(self, q: str, pool: list[GoldenTrio]) -> Retrieval:
        if self.degrade != "lexical":
            return Retrieval((), True, "none")
        qt = _tokens(q)
        scored = []
        for t in pool:
            tt = _tokens(f"{t.question} {' '.join(t.tags)}")
            union = qt | tt
            scored.append(Hit(t, len(qt & tt) / len(union) if union else 0.0))
        return Retrieval(tuple(_top_k(scored, self.k, LEXICAL_MIN_SCORE)), True, "lexical")


def _top_k(scored: list[Hit], k: int, min_score: float) -> list[Hit]:
    keep = [h for h in scored if h.score >= min_score]
    keep.sort(key=lambda h: (-h.score, h.trio.trio_id))
    return keep[:k]


# --- hand-off to the prompt layer ---------------------------------------------------------------


def to_store_items(hits: Sequence[Hit], scope: ProductScope) -> list[StoreItem]:
    """:class:`StoreItem` list for ``assemble_context`` (prompt layer 6, data-only fences).

    Brand-tied trios carry their own brands as the snapshot; agnostic trios carry the caller's
    current scope (there is no "agnostic" snapshot; ``context.covers`` then admits them).
    """
    items = []
    for h in hits:
        t = h.trio
        snap = snapshot_of(scope if t.agnostic else t.scope)
        text = f"Question: {t.question}\nSQL:\n{t.sql}\nReport pattern: {t.report_summary}"
        items.append(StoreItem(KIND_GOLDEN, text, snap, t.ref))
    return items
