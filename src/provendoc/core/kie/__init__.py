"""Key-information extraction (KIE) — structured fields from parsed text.

One module:
    Invoice (invoice.py) — invoice field extraction from narrative text
    for BOTH template families: Chinese electronic-invoice (电子发票)
    and Western invoice (Invoice # / Bill To / TOTAL AMOUNT DUE).
    Family dispatch runs on anchor evidence
    (core/scenario/invoice_anchors), upgrading ``domain_signals`` marker
    counts into per-field values with provenance and confidence grades.
"""

from provendoc.core.kie.invoice import extract_invoice_fields, flatten

__all__ = [
    "extract_invoice_fields",
    "flatten",
]
