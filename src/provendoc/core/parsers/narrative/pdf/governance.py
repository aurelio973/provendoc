"""Pluggable, fail-closed governance decisions for reconstructed PDF context.

Visible PDF text can suggest a classification, a version, or a status, but it
cannot prove user authorization or that a document is the currently effective
version.  This module therefore separates source annotations from external
policy decisions. A missing policy provider results in an explicit annotation-
only outcome, never implicit authorization.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class PDFGovernanceProvider(Protocol):
    """External source of ACL/version/time policy decisions for one document."""

    def evaluate(
        self,
        *,
        file_path: str | Path,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the external policy decision (ACL/version/effectivity)."""
        ...


@dataclass(frozen=True)
class GovernanceDecision:
    """Normalized policy result with explicit evidence and enforcement state."""

    action: str = "annotation_only"
    authorized: bool | None = None
    current_version: bool | None = None
    effective: bool | None = None
    visibility: str = "unknown"
    provider: dict[str, Any] | None = None
    reasons: tuple[str, ...] = ()
    external_references: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Serialize to a dict."""
        return {
            "policy_action": self.action,
            "filtering_recommended": self.action in {"allow", "deny", "exclude"},
            "filtering_applied": False,
            "authorized": self.authorized,
            "current_version": self.current_version,
            "effective": self.effective,
            "visibility": self.visibility,
            "provider": self.provider or {"status": "not_configured"},
            "reasons": list(self.reasons),
            "external_references": list(self.external_references),
        }


class PDFGovernanceEvaluator:
    """Normalize external policy output and preserve source-marker separation."""

    _VALID_ACTIONS = {"annotation_only", "allow", "deny", "exclude"}

    def __init__(self, provider: PDFGovernanceProvider | None = None):
        """Inject the external policy provider (may be None)."""
        self.provider = provider

    def evaluate(self, *, file_path: str | Path, context: dict[str, Any]) -> dict[str, Any]:
        """Normalize external policy output; failures fail closed to annotation-only."""
        if self.provider is None:
            return GovernanceDecision(
                provider={"status": "not_configured"},
                reasons=(
                    "No external ACL/version/effectivity provider is configured; "
                    "visible PDF markers remain annotations only.",
                ),
            ).as_dict()

        try:
            raw = self.provider.evaluate(file_path=file_path, context=context)
        except Exception as error:  # Governance must fail closed, without breaking parsing.
            return GovernanceDecision(
                action="annotation_only",
                provider={"status": "failed", "name": type(self.provider).__name__},
                reasons=(f"Governance provider failed: {error}",),
            ).as_dict()
        if not isinstance(raw, dict):
            return GovernanceDecision(
                action="annotation_only",
                provider={"status": "invalid", "name": type(self.provider).__name__},
                reasons=("Governance provider returned a non-mapping result.",),
            ).as_dict()
        return self._normalize(raw).as_dict()

    def _normalize(self, raw: dict[str, Any]) -> GovernanceDecision:
        action = str(raw.get("action", "annotation_only")).casefold()
        if action not in self._VALID_ACTIONS:
            action = "annotation_only"
        provider = raw.get("provider")
        if not isinstance(provider, dict):
            provider = {"status": "configured", "name": type(self.provider).__name__}
        reasons = self._string_tuple(raw.get("reasons"))
        references = self._string_tuple(raw.get("external_references"))
        return GovernanceDecision(
            action=action,
            authorized=self._optional_bool(raw.get("authorized")),
            current_version=self._optional_bool(raw.get("current_version")),
            effective=self._optional_bool(raw.get("effective")),
            visibility=str(raw.get("visibility", "unknown")),
            provider=provider,
            reasons=reasons,
            external_references=references,
        )

    @staticmethod
    def _string_tuple(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple, set)):
            return tuple(str(item) for item in value)
        return (str(value),)

    @staticmethod
    def _optional_bool(value: Any) -> bool | None:
        return value if isinstance(value, bool) else None
