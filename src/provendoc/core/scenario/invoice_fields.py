"""Invoice field skeleton: blocks → expected fields → three-state
placement.

Design discipline (aligned with invoice_anchors):
* this module never decides "is this an invoice" — the scenario
  label is the entry point and is already given by the resolver.
  Here we only answer "did this scenario's expected items get
  extracted".
* three states: EXTRACTED (value extracted) / LABEL_ONLY (label
  present, value not matched) / ABSENT (the whole block never
  appeared). ABSENT is not a failure, just an honest gap.
* missing-item attribution must be locatable: report the block
  name and the field name inside it, so downstream can say "what's
  missing is the invoice date" instead of a vague "not
  recognized".

Scope boundary (settled): documents with conditions or
clauses do not count as invoices; prefer gaps over guesses — when
uncertain, fall back to generic parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from provendoc.core.scenario.invoice_anchors import (
    AnchorResult,
    extract_invoice_anchors,
)

INVOICE_FIELDS_VERSION = "invoice_field_skeleton_v1"

FieldState = Literal["extracted", "label_only", "absent"]


@dataclass(frozen=True)
class BlockSpec:
    """One block: one thing an invoice must state.

    name:    block name (identity / billing / settlement)
    required: how many fields this block must extract at minimum to
        count as stated
    fields:  field names of this block (matching the anchors'
        field naming)
    labels:  label-detection regex for this block — used to
        distinguish LABEL_ONLY from ABSENT
    """

    name: str
    required: int
    fields: tuple[str, ...]
    labels: re.Pattern[str]


# Label detection is for **attribution** only, never for the
# verdict: a label present with no extracted value indicates a
# reading/matching problem (to be fixed), not "this document is
# not an invoice".
_IDENTITY_LABELS = re.compile(
    r"(?i:\binvoice\s*(?:#|no\.?|number)|"
    r"\bi\s+nvoice\s*#|"                       # OCR broken-word I NVOICE #
    r"\b(?:bill|sold|ship|remit)\s+to\b|"
    r"\binvoice\s+date\b|"
    r"\b(?:po|p\.o\.)\s*(?:no\.?|number|#))"
)
_BILLING_LABELS = re.compile(
    r"(?i:\b(?:description|qty|quantity)\b[^\n]{0,40}\b(?:rate|amount|price)\b|"
    r"\bspots?\b|"
    r"\bhours?\b)"
)
_SETTLEMENT_LABELS = re.compile(
    r"(?i:\btotal\b|\bamount\s+due\b|\bsub\s*-?\s*total\b|"
    r"\bgrand\s+total\b|\bnet\b|\bplease\s+(?:pay|remit)\b)"
)


BLOCKS: tuple[BlockSpec, ...] = (
    BlockSpec(
        name="identity",
        required=2,
        fields=("invoice_number", "invoice_date", "tax_id"),
        labels=_IDENTITY_LABELS,
    ),
    BlockSpec(
        name="billing",
        required=1,
        fields=("subtotal",),
        labels=_BILLING_LABELS,
    ),
    BlockSpec(
        name="settlement",
        required=1,
        fields=("total_due", "tax"),
        labels=_SETTLEMENT_LABELS,
    ),
)


@dataclass(frozen=True)
class FieldReport:
    """Placement result of a single field."""

    block: str
    field: str
    state: FieldState
    value: str = ""
    line: int = 0
    pattern: str = ""


@dataclass(frozen=True)
class BlockReport:
    """Placement result of a single block."""

    name: str
    state: FieldState
    extracted: tuple[str, ...]
    label_only: tuple[str, ...]
    absent: tuple[str, ...]


@dataclass(frozen=True)
class InvoiceFieldSkeleton:
    """Field-skeleton placement result for one document."""

    version: str
    fields: tuple[FieldReport, ...]
    blocks: tuple[BlockReport, ...]
    gaps: tuple[str, ...] = ()


def _block_report(
    spec: BlockSpec, anchors: AnchorResult, text: str
) -> BlockReport:
    extracted: list[str] = []
    label_only: list[str] = []
    absent: list[str] = []
    block_has_label = bool(spec.labels.search(text))
    for fname in spec.fields:
        if anchors.by_field(fname):
            extracted.append(fname)
        elif block_has_label:
            # the block's label is present but this field has no
            # value — a "reading/matching" gap, a different disease
            # from "the whole block never appeared"
            label_only.append(fname)
        else:
            absent.append(fname)
    if len(extracted) >= spec.required:
        state: FieldState = "extracted"
    elif label_only:
        state = "label_only"
    else:
        state = "absent"
    return BlockReport(
        name=spec.name,
        state=state,
        extracted=tuple(extracted),
        label_only=tuple(label_only),
        absent=tuple(absent),
    )


def locate_invoice_fields(text: str) -> InvoiceFieldSkeleton:
    """Place fields against the invoice skeleton checklist. Pure
    function, never raises.

    Returns each block's three-state placement + the full field
    list + a gap list (human-readable, for audit).
    """
    anchors = extract_invoice_anchors(text or "")
    reports: list[FieldReport] = []
    blocks: list[BlockReport] = []
    gaps: list[str] = []

    for spec in BLOCKS:
        br = _block_report(spec, anchors, text or "")
        blocks.append(br)
        for fname in spec.fields:
            hits = anchors.by_field(fname)
            if hits:
                reports.append(FieldReport(
                    block=spec.name, field=fname, state="extracted",
                    value=hits[0].value, line=hits[0].line,
                    pattern=hits[0].pattern,
                ))
            elif fname in br.label_only:
                reports.append(FieldReport(
                    block=spec.name, field=fname, state="label_only"
                ))
            else:
                reports.append(FieldReport(
                    block=spec.name, field=fname, state="absent"
                ))
        if br.state != "extracted":
            missing = list(br.label_only) + list(br.absent)
            gaps.append(
                f"{spec.name}:{br.state}("
                + ",".join(missing)
                + ")"
            )

    return InvoiceFieldSkeleton(
        version=INVOICE_FIELDS_VERSION,
        fields=tuple(reports),
        blocks=tuple(blocks),
        gaps=tuple(gaps),
    )


def field_skeleton_payload(text: str) -> dict:
    """Serializable form for metadata use."""
    sk = locate_invoice_fields(text)
    return {
        "version": sk.version,
        "blocks": [
            {
                "name": b.name,
                "state": b.state,
                "extracted": list(b.extracted),
                "label_only": list(b.label_only),
                "absent": list(b.absent),
            }
            for b in sk.blocks
        ],
        "fields": [
            {
                "block": f.block,
                "field": f.field,
                "state": f.state,
                "value": f.value,
                "line": f.line,
                "pattern": f.pattern,
            }
            for f in sk.fields
        ],
        "gaps": list(sk.gaps),
    }


__all__ = [
    "BLOCKS",
    "BlockReport",
    "BlockSpec",
    "FieldReport",
    "FieldState",
    "INVOICE_FIELDS_VERSION",
    "InvoiceFieldSkeleton",
    "field_skeleton_payload",
    "locate_invoice_fields",
]
