"""Nessie v2 REST client — branch lifecycle for ephemeral pipeline branches."""

from __future__ import annotations

import functools
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from rat_runner.config import NessieConfig

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Public so callers (e.g. executor.py) can describe retry behaviour in
# their own error messages without hard-coding a magic number.
BRANCH_CREATE_MAX_RETRIES = 3

# Number of attempts merge_branch will make when the merge POST returns 409
# CONFLICT — meaning the target ref (main) moved between our GET and our
# POST, typically because a concurrent run merged first. Each attempt
# re-fetches the target hash and re-posts.
MERGE_CONFLICT_MAX_RETRIES = 3


def _is_transient_error(exc: Exception) -> bool:
    """Return True if the exception is a transient HTTP or connection error.

    Transient errors are 5xx server errors and network-level failures
    (URLError without an HTTP status, e.g. connection refused, DNS failures).
    Client errors (4xx) are NOT retried — they indicate a problem with the request.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        # URLError wraps connection-level failures (refused, timeout, DNS).
        return True
    return isinstance(exc, TimeoutError)


def retry_on_transient(
    max_retries: int = 3,
    initial_backoff: float = 0.5,
    _sleep: Callable[[float], None] | None = None,
) -> Callable[[F], F]:
    """Decorator that retries a function on transient HTTP/connection errors.

    Uses exponential backoff: initial_backoff * 2^attempt (0.5s, 1s, 2s by default).
    Non-transient errors (4xx, ValueError, etc.) are raised immediately.

    Args:
        max_retries: Maximum number of retry attempts (not counting the initial call).
        initial_backoff: Seconds to wait before the first retry. Doubles each attempt.
        _sleep: Sleep function, injectable for testing without real delays.
                When None, resolves time.sleep at call time (allows patching).
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            sleep_fn = _sleep if _sleep is not None else time.sleep
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if not _is_transient_error(exc):
                        raise
                    last_exc = exc
                    if attempt < max_retries:
                        delay = initial_backoff * (2**attempt)
                        logger.warning(
                            "Nessie call %s failed (attempt %d/%d): %s — retrying in %.1fs",
                            fn.__name__,
                            attempt + 1,
                            max_retries + 1,
                            exc,
                            delay,
                        )
                        sleep_fn(delay)
                    else:
                        logger.error(
                            "Nessie call %s failed after %d attempts: %s",
                            fn.__name__,
                            max_retries + 1,
                            exc,
                        )
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


# Branch names must be safe identifiers — no path traversal or special characters.
_SAFE_BRANCH_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


def _validate_branch_name(name: str) -> str:
    """Validate and return a safe branch name.

    Raises ValueError if the name could cause path traversal or injection.
    """
    if not name or not _SAFE_BRANCH_NAME.match(name):
        raise ValueError(f"Invalid Nessie branch name: {name!r}")
    if ".." in name:
        raise ValueError(f"Branch name must not contain '..': {name!r}")
    return name


def _encode_branch(name: str) -> str:
    """Validate branch name and URL-encode it for safe path interpolation."""
    _validate_branch_name(name)
    return urllib.parse.quote(name, safe="")


@retry_on_transient(max_retries=BRANCH_CREATE_MAX_RETRIES)
def create_branch(
    nessie_config: NessieConfig,
    branch_name: str,
    from_branch: str = "main",
) -> str:
    """Create a new Nessie branch from an existing branch.

    Returns the hash of the new branch head.
    Retries transient errors (5xx / network / timeout) up to
    BRANCH_CREATE_MAX_RETRIES times with exponential backoff; permanent
    errors (4xx, invalid name) raise immediately.
    Idempotent: if the branch already exists (409 Conflict), returns its current hash.
    """
    _validate_branch_name(branch_name)
    _validate_branch_name(from_branch)

    # First, get the hash of the source branch
    source_ref = _get_reference(nessie_config, from_branch)
    source_hash = source_ref["hash"]

    # Nessie v2 POST /trees uses query parameters for the new branch's
    # name/type, and the request body is the source Reference (NOT a
    # wrapped {name,type,reference:{...}} object as some older clients
    # documented). The response wraps the created branch under a
    # "reference" key. Both shapes verified against Nessie 0.99.x.
    url = (
        f"{nessie_config.api_v2_url}/trees"
        f"?name={urllib.parse.quote(branch_name, safe='')}&type=BRANCH"
    )
    source_payload = {
        "type": "BRANCH",
        "name": from_branch,
        "hash": source_hash,
    }
    data = json.dumps(source_payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["reference"]["hash"]
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # Branch already exists — return its current hash
            existing = _get_reference(nessie_config, branch_name)
            return existing["hash"]
        raise


@retry_on_transient()
def merge_branch(
    nessie_config: NessieConfig,
    source: str,
    target: str = "main",
) -> None:
    """Merge a source branch into a target branch.

    Nessie v2 requires the target ref's current hash in the path using
    the `{ref}@{hash}` syntax (the `expected-hash` query-string form is
    not accepted on /history/merge — confirmed against Nessie 0.99.x).
    Without it the merge returns 400 "Expected hash must be provided".

    Failure classification:
      * 5xx / URLError / TimeoutError — transient, retried by the outer
        @retry_on_transient decorator with exponential backoff.
      * 409 CONFLICT on the merge POST — the target ref moved between
        our GET and our POST (another concurrent run merged first).
        We refetch the target hash and retry up to MERGE_CONFLICT_MAX_RETRIES
        times. This loop is local because the outer decorator treats all
        4xx as permanent; only 409 on the merge endpoint is recoverable.
      * Other 4xx — permanent, raised immediately.
    """
    source_ref = _get_reference(nessie_config, source)
    source_hash = source_ref["hash"]

    # Refetch target hash on every attempt — that's the whole point of the
    # 409 retry: another run moved `target` and we need the current head.
    last_409: urllib.error.HTTPError | None = None
    for attempt in range(MERGE_CONFLICT_MAX_RETRIES):
        target_ref = _get_reference(nessie_config, target)
        target_hash = target_ref["hash"]

        # Path-embed the expected target hash with @-syntax (URL-encoded).
        target_path = _encode_branch(target) + urllib.parse.quote("@" + target_hash, safe="")
        url = f"{nessie_config.api_v2_url}/trees/{target_path}/history/merge"
        payload = {
            "fromRefName": source,
            "fromHash": source_hash,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()  # consume response
            return
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
            last_409 = e
            logger.warning(
                "Nessie merge 409 CONFLICT on attempt %d/%d "
                "(target %s moved during merge window) — refetching target hash",
                attempt + 1,
                MERGE_CONFLICT_MAX_RETRIES,
                target,
            )
            # Loop: next iteration re-GETs target and re-POSTs.

    # Exhausted 409 retries — surface the last 409 to the caller.
    assert last_409 is not None
    raise last_409


def delete_branch(
    nessie_config: NessieConfig,
    branch_name: str,
) -> None:
    """Delete a Nessie branch. Ignores 404 (branch already deleted)."""
    try:
        ref = _get_reference(nessie_config, branch_name)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise

    # Nessie v2 expects the branch's current hash embedded in the ref path
    # via `{ref}@{hash}` syntax — the `?expected-hash=` query form is rejected
    # with 400 (same constraint as merge_branch; confirmed on Nessie 0.99.x).
    branch_hash = ref["hash"]
    ref_path = _encode_branch(branch_name) + urllib.parse.quote("@" + branch_hash, safe="")
    url = f"{nessie_config.api_v2_url}/trees/{ref_path}"
    req = urllib.request.Request(url, method="DELETE")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise


@retry_on_transient()
def _get_reference(nessie_config: NessieConfig, branch_name: str) -> dict[str, str]:
    """Get a Nessie branch reference (returns the inner {type,name,hash}).

    Nessie v2 GET /trees/{ref} wraps the reference in a top-level
    `reference` key. We unwrap so callers can do `ref["hash"]` directly.
    """
    url = f"{nessie_config.api_v2_url}/trees/{_encode_branch(branch_name)}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    # Tolerate both shapes: the wrapped {"reference": {...}} returned by
    # Nessie 0.99.x, and a raw {type,name,hash} if a future Nessie
    # version flattens it.
    return body.get("reference", body)
