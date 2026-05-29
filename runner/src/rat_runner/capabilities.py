"""Plan-time capability matching for a data-plane composition (ADR-024).

Before a run touches anything, the runner asks each axis (engine / catalog /
storage) what it can do via its ``Describe`` RPC and checks the resolved
``DataPlane`` binding against those capabilities. A mismatch is a *config*
error surfaced up-front (``CompositionError``) — not a mid-run crash after a
branch has already been created or half the pipeline has executed.

Matching is pure string equality over the open-set identifiers the contracts
use (formats, capabilities, schemes), so a third-party engine/catalog/storage
participates without any change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rat_runner.bindings import DataPlane


class CompositionError(Exception):
    """A data-plane binding is incompatible with its axes' advertised capabilities."""


@dataclass(frozen=True)
class AxisCapabilities:
    """The capability surface of a composition, gathered from the three Describe RPCs."""

    engine_formats: list[str]
    engine_languages: list[str]
    catalog_formats: list[str]
    catalog_capabilities: list[str]
    storage_schemes: list[str]


def check_composition(
    plane: DataPlane,
    caps: AxisCapabilities,
    *,
    language: str | None = None,
    storage_scheme: str = "s3",
) -> None:
    """Validate ``plane`` against ``caps``; raise ``CompositionError`` listing every mismatch.

    Checks (each independent, all reported together so one fix-cycle resolves the binding):
      * the engine can read/write the plane's table format;
      * the catalog manages that format;
      * if the binding declares branching, the catalog actually advertises it
        (otherwise phase 0 would FAILED_PRECONDITION mid-run);
      * the storage axis serves the scheme the run will use;
      * (optional) the engine can execute the pipeline's language.
    """
    errors: list[str] = []

    if plane.format not in caps.engine_formats:
        errors.append(
            f"engine does not support format {plane.format!r} "
            f"(engine formats: {caps.engine_formats})"
        )
    if plane.format not in caps.catalog_formats:
        errors.append(
            f"catalog does not manage format {plane.format!r} "
            f"(catalog formats: {caps.catalog_formats})"
        )
    if plane.supports_branching and "branching" not in caps.catalog_capabilities:
        errors.append(
            "binding declares supports_branching=true but catalog "
            f"{plane.catalog_protocol!r} lacks the 'branching' capability "
            f"(catalog capabilities: {caps.catalog_capabilities})"
        )
    if storage_scheme not in caps.storage_schemes:
        errors.append(
            f"storage does not serve scheme {storage_scheme!r} "
            f"(storage schemes: {caps.storage_schemes})"
        )
    if language is not None and language not in caps.engine_languages:
        errors.append(
            f"engine cannot execute language {language!r} "
            f"(engine languages: {caps.engine_languages})"
        )

    if errors:
        bullets = "\n  - ".join(errors)
        raise CompositionError(
            f"data plane {plane.name!r} is not a valid composition:\n  - {bullets}"
        )
