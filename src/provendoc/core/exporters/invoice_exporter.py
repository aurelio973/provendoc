"""Invoice structural framework exporter — the primary field
artifact for the invoice scenario (pure presentation layer).

Contract:
    - **Zero parsing changes**: only reads existing
      ``document.metadata`` keys (invoice_fields / scenario /
      ocr_unresolved) for rendering — no recomputation, no
      backfilling, no second-guessing; null fields only get their
      reason registered, values are never fabricated out of
      digit fragments or image regions.
    - The output is a human-readable framework with Chinese keys
      (scenario / invoice / checks / unextracted fields /
      notes), one file per invoice; markdown stays the narrative
      presentation layer, this export is the invoice scenario's
      primary field artifact.
    - Writes go through atomic_write_json (temp + os.replace,
      same implementation as the markdown/raw/json exporters,
      single source of truth).

Two-family value-shape mapping (family comes from
invoice_fields.evidence.family):
    - chinese_invoice: items {项目名称,金额,税率,税额}, totals
      {金额合计,税额合计}, grand total {大写,小写};
    - western_invoice: items {description,amount,qty,unit_price} →
      {名称,金额,数量,单价}, totals {subtotal,tax} →
      {金额,税额}, grand total {total_due} → {小写};
    - item rows carry no raw source line (the result.json
      envelope is authoritative; the framework stays clean).
"""

from __future__ import annotations

from typing import Any, Optional

from provendoc.base.exporter import BaseExporter
from provendoc.core.exporters.atomic_write import atomic_write_json

# Ten-field keys → framework Chinese labels (shared by the
# "unextracted fields" and "invoice" sections — one naming map,
# never a second field-name mapping).
_FIELD_LABELS = {
    "title": "标题",
    "invoice_number": "发票号码",
    "issue_date": "开票日期",
    "buyer": "购买方",
    "seller": "销售方",
    "items": "明细",
    "totals": "合计",
    "grand_total": "价税合计",
    "drawer": "开票人",
    "remark": "备注",
}

_FAMILY_LABELS = {
    "chinese_invoice": "中文电子发票",
    "western_invoice": "西式发票",
}

_SOURCE_NOTE = (
    "字段来自锚点提取通道（invoice_fields 信封）；"
    "未提取字段为提取失败的诚实原因登记（文本层无证据等），不猜测不编造"
)
_STUB_NOTE = (
    "未检出 invoice 字段证据（invoice_fields 缺席）："
    "场景标签照登，发票字段全 null，不编造"
)


def _metadata_of(data: Any) -> dict:
    """IngestionResult / Document / metadata dict → metadata dict.

    IngestionResult yields document.metadata; Document yields
    metadata; a dict is passed through as-is (lightweight callers
    that already hold the metadata).
    """
    if isinstance(data, dict):
        return data
    document = getattr(data, "document", None)
    if document is not None and hasattr(document, "metadata"):
        return document.metadata
    if hasattr(data, "metadata"):
        return data.metadata
    raise TypeError(
        f"export_invoice 不支持的数据形态: {type(data).__name__}"
        "（接受 IngestionResult / Document / metadata dict）"
    )


def _party(value: Any) -> Optional[dict]:
    """buyer/seller envelope value → {名称, 税号} (None passes
    through)."""
    if not isinstance(value, dict):
        return None
    return {"名称": value.get("name"), "税号": value.get("tax_id")}


def _cn_items(value: Any) -> Optional[list]:
    if not isinstance(value, list):
        return None
    return [
        {
            "名称": it.get("项目名称"),
            "金额": it.get("金额"),
            "税率": it.get("税率"),
            "税额": it.get("税额"),
        }
        for it in value
    ]


def _western_items(value: Any) -> Optional[list]:
    if not isinstance(value, list):
        return None
    return [
        {
            "名称": it.get("description"),
            "金额": it.get("amount"),
            "数量": it.get("qty"),
            "单价": it.get("unit_price"),
        }
        for it in value
    ]


def _totals(value: Any, family: str) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    if family == "western_invoice":
        return {"金额": value.get("subtotal"), "税额": value.get("tax")}
    return {"金额": value.get("金额合计"), "税额": value.get("税额合计")}


def _grand_total(value: Any, family: str) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    if family == "western_invoice":
        return {"小写": value.get("total_due")}
    return {"大写": value.get("大写"), "小写": value.get("小写")}


def _unresolved_codes(metadata: dict) -> dict:
    """ocr_unresolved records → {field English key: reason_code}.

    Object IDs look like ``kie:invoice:invoice_number`` — the
    colon tail segment aligns with the ten field keys; only
    records whose field key matches are registered (other PDF
    objects are irrelevant to this framework).
    """
    registry = metadata.get("ocr_unresolved")
    if not isinstance(registry, dict):
        return {}
    out: dict = {}
    for record in registry.get("records", []):
        object_id = record.get("object_id") or ""
        field_key = object_id.split(":")[-1]
        if field_key in _FIELD_LABELS:
            out[field_key] = record.get("reason_code")
    return out


class InvoiceExporter(BaseExporter):
    """Invoice structural framework exporter (Chinese keys, directly
    human-readable, one file per invoice)."""

    def export(self, data, path) -> None:
        """Render the invoice scenario fields into a Chinese-key
        framework JSON and atomically write it to disk.

        Args:
            data: IngestionResult / Document / metadata dict.
            path: output file path (atomic write).
        """
        metadata = _metadata_of(data)
        invoice_fields = metadata.get("invoice_fields")
        scenario = metadata.get("scenario") or {}

        if not isinstance(invoice_fields, dict):
            # honest stub: no exception, no fabrication, no silent
            # skip
            framework: dict = {
                "场景": {
                    "标签": scenario.get("label"),
                    "依据": scenario.get("basis"),
                    "模板族": None,
                },
                "发票": {label: None for label in _FIELD_LABELS.values()},
                "校验": [],
                "未提取字段": [],
                "说明": _STUB_NOTE,
            }
            atomic_write_json(path, framework)
            return

        fields = invoice_fields.get("fields", {})
        evidence = invoice_fields.get("evidence", {})
        family = evidence.get("family") or "chinese_invoice"
        family_label = _FAMILY_LABELS.get(family, family)

        # items/totals/grand-total value shapes per family
        # (two-family mapping)
        if family == "western_invoice":
            items_fn, totals_fn, grand_fn = (
                _western_items, _totals, _grand_total
            )
        else:
            items_fn, totals_fn, grand_fn = (
                _cn_items, _totals, _grand_total
            )

        def _val(key: str) -> Any:
            envelope = fields.get(key) or {}
            return envelope.get("value")

        def _note(key: str) -> Optional[str]:
            envelope = fields.get(key) or {}
            return envelope.get("note")

        invoice = {
            "标题": _val("title"),
            "发票号码": _val("invoice_number"),
            "开票日期": _val("issue_date"),
            "购买方": _party(_val("buyer")),
            "销售方": _party(_val("seller")),
            "明细": items_fn(_val("items")),
            "合计": totals_fn(_val("totals"), family),
            "价税合计": grand_fn(_val("grand_total"), family),
            "开票人": _val("drawer"),
            "备注": _val("remark"),
        }

        checks = [
            {
                "项": check.get("check"),
                "通过": bool(check.get("pass", check.get("passed"))),
                "预期": check.get("expected"),
                "实际": check.get("actual"),
            }
            for check in invoice_fields.get("validation") or []
        ]

        codes = _unresolved_codes(metadata)
        unresolved = [
            {
                "字段": label,
                "说明": _note(key),
                "登记码": codes.get(key),
            }
            for key, label in _FIELD_LABELS.items()
            if _val(key) is None
        ]

        framework = {
            "场景": {
                "标签": scenario.get("label"),
                "依据": scenario.get("basis"),
                "模板族": f"{family_label} ({family})",
            },
            "发票": invoice,
            "校验": checks,
            "未提取字段": unresolved,
            "说明": _SOURCE_NOTE,
        }

        atomic_write_json(path, framework)


__all__ = ["InvoiceExporter"]
