"""Delete token and confirmation proof (ADR-007, HLD §6.3.3).

* ``K_delete`` is 32 random bytes made in memory when the process starts (:func:`new_key`).
  It is never written to disk, a checkpoint, a trace, a log or an error: :class:`DeleteKey`
  hides it from ``repr``/``str`` and registers its hex form with the tracer scrubber.
* The token is derived, never stored::

      token = HMAC-SHA256(K_delete, pending_action_id | ids_sha256 | owner | session_id
                                    | preview_turn | expires_at)

  Graph state (and so the checkpoint) holds only ``sha256(token)``.
* The confirmation proof binds the user's reply to one pending action::

      proof = HMAC-SHA256(token, reply | pending_action_id | ids_sha256 | preview_turn
                                 | expires_at)

  and is compared in constant time. The proof stays in process memory
  (:class:`~opsfleet_agent.delete.flow.DeleteService`); the resume value, and so the
  checkpoint write, carries only ``sha256(proof)`` (iteration 22a OD-11).
* The flow registers each token and proof with the scrubber once per pending action and
  forgets the oldest past a bound (``register=False`` here, :func:`forget_secret`).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable, Mapping
from typing import Any, Final

from opsfleet_agent.obs.tracer import forget_secret as _forget_secret
from opsfleet_agent.obs.tracer import register_secret

__all__ = [
    "KEY_BYTES",
    "TOKEN_FIELDS",
    "DeleteKey",
    "derive_token",
    "forget_secret",
    "ids_sha256",
    "make_proof",
    "new_key",
    "token_sha256",
    "verify_proof",
]

KEY_BYTES: Final = 32
TOKEN_FIELDS: Final = (
    "pending_action_id", "ids_sha256", "owner", "session_id", "preview_turn", "expires_at",
)  # fmt: skip
PROOF_FIELDS: Final = ("pending_action_id", "ids_sha256", "preview_turn", "expires_at")
_SEP: Final = "\x1f"  # unit separator: never part of a hex id, an actor id or an int


class DeleteKey:
    """``K_delete``. Its bytes are reachable only through :meth:`mac`; ``repr`` and ``str``
    print ``[secret]``, and pickling is refused, so it cannot reach a checkpoint."""

    __slots__ = ("_k",)

    def __init__(self, raw: bytes) -> None:
        if not isinstance(raw, bytes) or len(raw) != KEY_BYTES:
            raise ValueError("K_delete must be 32 bytes")
        self._k = raw
        register_secret(raw.hex())

    def mac(self, message: bytes) -> bytes:
        return hmac.new(self._k, message, hashlib.sha256).digest()

    def __repr__(self) -> str:
        return "[secret]"

    __str__ = __repr__

    def __reduce__(self):  # noqa: D105 - pickling would write the key somewhere
        raise TypeError("K_delete cannot be serialised")


def new_key() -> DeleteKey:
    """A fresh in-memory ``K_delete`` (process start)."""
    return DeleteKey(secrets.token_bytes(KEY_BYTES))


def ids_sha256(ids: Iterable[str]) -> str:
    """Order-independent digest of a target set (sorted, unit-separated)."""
    return hashlib.sha256(_SEP.join(sorted(str(i) for i in ids)).encode()).hexdigest()


def _message(fields: Mapping[str, Any], names: tuple[str, ...]) -> bytes:
    parts = []
    for n in names:
        v = fields[n]  # KeyError on a missing field: the caller fails closed
        if type(v) not in (str, int):
            raise TypeError(f"{n} must be str or int")
        parts.append(str(v))
    return _SEP.join(parts).encode()


def derive_token(key: DeleteKey, fields: Mapping[str, Any], *, register: bool = True) -> str:
    """The token for a pending action (hex), never stored. ``register``: add it to the
    scrubber here (the flow passes False and registers once per pending action)."""
    token = key.mac(_message(fields, TOKEN_FIELDS)).hex()
    if register:
        register_secret(token)
    return token


def forget_secret(value: str) -> None:
    """Drop a value the flow registered (bounded scrubber set)."""
    _forget_secret(value)


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_proof(token: str, reply: str, fields: Mapping[str, Any], *,
               register: bool = True) -> str:  # fmt: skip
    """The proof for a reply (hex). ``register`` as for :func:`derive_token`."""
    msg = reply.encode() + _SEP.encode() + _message(fields, PROOF_FIELDS)
    proof = hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()
    if register:
        register_secret(proof)
    return proof


def verify_proof(token: str, reply: str, fields: Mapping[str, Any], proof: object) -> bool:
    """Constant-time check; any malformed input is False (fail closed)."""
    if not isinstance(proof, str):
        return False
    try:
        expected = make_proof(token, reply, fields, register=False)
    except (KeyError, TypeError):
        return False
    return hmac.compare_digest(expected.encode(), proof.encode())
