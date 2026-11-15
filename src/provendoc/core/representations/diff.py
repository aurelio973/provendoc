"""Representation diff — reproducibility & difference comparison (B12).

Compares two RepresentationResults and reports a deterministic, structured
diff.  Used for:

- reparse-consistency checks (same file parsed twice must diff to
  ``identical=True``),
- cross-parser / cross-strategy comparison (``evaluate`` vs ``ingest``),
- cache-validity investigations.

Design rules:

- comparison basis is unit text hashed with SHA-256 (first 16 hex chars);
  no wall-clock timestamps, no environment-dependent values — diffing the
  same pair twice must produce the same diff;
- units are compared per granularity (base / coarse / fine) by position;
- metadata is compared at key-set level only: metadata key presence is
  compared, metadata values are intentionally ignored (unit-level metadata
  contains environment-specific evidence that is not part of
  reproducibility);
- ``identical`` means BOTH unit content and the result-level metadata key
  set match ("are these two pipeline results interchangeable?").  Callers
  interested only in content (e.g. cache-hit / did-the-content-change
  checks) must use ``content_identical`` instead — never ``identical``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Sequence, Tuple

# Single source of truth for the dimensions ``identical`` covers.  If the
# identity criteria ever gain or lose a dimension, change this constant
# together with the return dict below — the invariant test
# (test_identity_basis_invariants) fails until both are in sync.
_IDENTITY_BASIS: Sequence[str] = ("unit_content", "metadata_key_set")


def _text_hash(unit: Any) -> str:
    return hashlib.sha256(unit.text.encode("utf-8")).hexdigest()[:16]


def _granularity_lists(result: Any) -> Dict[str, List[Any]]:
    """Extract per-granularity unit lists from a RepresentationResult."""
    content = getattr(result, "content", None)
    if content is None:
        return {}
    if hasattr(content, "base"):
        lists: Dict[str, List[Any]] = {"base": list(content.base)}
        if getattr(content, "coarse", None):
            lists["coarse"] = list(content.coarse)
        if getattr(content, "fine", None):
            lists["fine"] = list(content.fine)
        return lists
    return {"base": list(content)}


def _diff_list(
    granularity: str,
    list_a: Sequence[Any],
    list_b: Sequence[Any],
    changes: List[Dict[str, Any]],
) -> None:
    for index in range(max(len(list_a), len(list_b))):
        in_a = index < len(list_a)
        in_b = index < len(list_b)
        if in_a and not in_b:
            changes.append(
                {
                    "granularity": granularity,
                    "index": index,
                    "kind": "removed",
                    "text_hash_a": _text_hash(list_a[index]),
                }
            )
        elif in_b and not in_a:
            changes.append(
                {
                    "granularity": granularity,
                    "index": index,
                    "kind": "added",
                    "text_hash_b": _text_hash(list_b[index]),
                }
            )
        else:
            hash_a = _text_hash(list_a[index])
            hash_b = _text_hash(list_b[index])
            if hash_a != hash_b:
                changes.append(
                    {
                        "granularity": granularity,
                        "index": index,
                        "kind": "changed",
                        "text_hash_a": hash_a,
                        "text_hash_b": hash_b,
                    }
                )


def _diff_metadata_keys(a: Any, b: Any) -> List[str]:
    keys_a = set((getattr(a, "metadata", None) or {}).keys())
    keys_b = set((getattr(b, "metadata", None) or {}).keys())
    return sorted(keys_a ^ keys_b)


def diff_representation_results(a: Any, b: Any) -> Dict[str, Any]:
    """Deterministic structured diff of two RepresentationResults.

    Returns a JSON-compatible dict::

        {
            "identical": bool,                # unit content AND metadata key set
            "content_identical": bool,        # unit content only
            "metadata_keys_identical": bool,  # result-level metadata key set only
            "identity_basis": ["unit_content", "metadata_key_set"],
            "unit_counts": {"base": [n_a, n_b], ...},
            "changes": [
                {"granularity", "index", "kind", "text_hash_a", "text_hash_b"},
                ...
            ],
            "metadata_keys_only_in_one": [...],
            "comparison_basis": "sha256_first16_per_unit_text",
        }

    Caller guidance:

    - cache hit / "did the content change" → ``content_identical``
    - "are these two pipeline results interchangeable" → ``identical``
    - debugging provenance completeness → ``metadata_keys_only_in_one``
    """
    lists_a = _granularity_lists(a)
    lists_b = _granularity_lists(b)
    changes: List[Dict[str, Any]] = []
    unit_counts: Dict[str, Tuple[int, int]] = {}

    for granularity in ("base", "coarse", "fine"):
        granularity_a = lists_a.get(granularity, [])
        granularity_b = lists_b.get(granularity, [])
        if not granularity_a and not granularity_b:
            continue
        unit_counts[granularity] = (len(granularity_a), len(granularity_b))
        _diff_list(granularity, granularity_a, granularity_b, changes)

    metadata_key_diff = _diff_metadata_keys(a, b)
    content_identical = not changes
    metadata_keys_identical = not metadata_key_diff

    return {
        "identical": content_identical and metadata_keys_identical,
        "content_identical": content_identical,
        "metadata_keys_identical": metadata_keys_identical,
        "identity_basis": list(_IDENTITY_BASIS),
        "unit_counts": {
            granularity: list(counts) for granularity, counts in unit_counts.items()
        },
        "changes": changes,
        "metadata_keys_only_in_one": metadata_key_diff,
        "comparison_basis": "sha256_first16_per_unit_text",
    }
