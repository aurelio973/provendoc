"""Evidence-only DOCX chart extraction (B11, gap register §8).

Parses the DrawingML chart parts (``word/charts/chart*.xml``) a DOCX package
carries: chart type, title, series names, cached category/value points,
legend and axes.  Series reference formulas (``c:f`` — including external
``[Book]!`` references), the chart's own relationships (``c:externalData``
external workbook links) and chartSpace extension lists (``c:extLst``) are
registered verbatim as evidence: links are never followed, formulas are
never evaluated and extension content is never parsed.  Each chart fact
uses the same key set as the spreadsheet chart contract
(``id`` / ``title`` / ``chart_type`` / ``anchor`` / ``series_count`` /
``source`` / ``inference`` — see
``parsers/table/excel/backends/openpyxl_backend.py``), so DOCX-embedded
charts and Excel workbook charts surface one comparable shape.

Everything is evidence: cached values are recorded as the producer wrote
them, never recomputed, and the chart image itself is never re-rendered.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_RELS_TYPE_CHART = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
)

# Normalized chart-type names aligned with the openpyxl chart classes the
# spreadsheet backend reports (``chart.__class__.__name__``).
_CHART_TYPE_BY_XML_NAME = {
    "areaChart": "AreaChart",
    "area3DChart": "Area3DChart",
    "barChart": "BarChart",
    "bar3DChart": "Bar3DChart",
    "bubbleChart": "BubbleChart",
    "doughnutChart": "DoughnutChart",
    "lineChart": "LineChart",
    "line3DChart": "Line3DChart",
    "ofPieChart": "OfPieChart",
    "pieChart": "PieChart",
    "pie3DChart": "Pie3DChart",
    "radarChart": "RadarChart",
    "scatterChart": "ScatterChart",
    "stockChart": "StockChart",
    "surfaceChart": "SurfaceChart",
    "surface3DChart": "Surface3DChart",
}

_MAX_SERIES_SAMPLES = 12
_MAX_CACHED_POINT_SAMPLES = 5
_MAX_CHARTS = 50
_MAX_EXT_SAMPLES = 10
_MAX_EXT_EXCERPT_CHARS = 200


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _text_of(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(
        node.text or "" for node in element.iter(f"{{{_A_NS}}}t")
    ).strip()


def _chart_references(document_xml: bytes) -> dict[str, dict[str, Any]]:
    """Map chart relationship ids to their placement in document.xml."""
    references: dict[str, dict[str, Any]] = {}
    root = ET.fromstring(document_xml)
    body = root.find(f"{{{_W_NS}}}body")
    paragraph_index = -1
    for element in (body if body is not None else root).iter():
        if element.tag == f"{{{_W_NS}}}p":
            paragraph_index += 1
        if element.tag not in {f"{{{_WP_NS}}}inline", f"{{{_WP_NS}}}anchor"}:
            continue
        placement = "inline" if element.tag.endswith("inline") else "anchored"
        doc_pr = element.find(f"{{{_WP_NS}}}docPr")
        for chart_ref in element.iter(f"{{{_C_NS}}}chart"):
            rel_id = chart_ref.get(f"{{{_R_NS}}}id")
            if not rel_id:
                continue
            references[rel_id] = {
                "placement": placement,
                "paragraph_index": paragraph_index,
                "name": doc_pr.get("name") if doc_pr is not None else None,
                "alt_text": doc_pr.get("descr") if doc_pr is not None else None,
            }
    return references


def _series_facts(serie: ET.Element) -> dict[str, Any]:
    """Series name and cached category/value points as the producer wrote them."""
    name = ""
    name_formula = ""
    tx = serie.find(f"{{{_C_NS}}}tx")
    if tx is not None:
        cached_values = tx.findall(
            f"{{{_C_NS}}}strRef/{{{_C_NS}}}strCache/{{{_C_NS}}}pt/{{{_C_NS}}}v"
        )
        if cached_values:
            name = (cached_values[0].text or "").strip()
        else:
            name = _text_of(tx) or (tx.findtext(f"{{{_C_NS}}}v") or "").strip()
        name_formula = (tx.findtext(f".//{{{_C_NS}}}f") or "").strip()

    categories = _cache_facts(serie.find(f"{{{_C_NS}}}cat"))
    values = _cache_facts(serie.find(f"{{{_C_NS}}}val"))
    return {
        "name": name,
        "name_formula": name_formula,
        "category_cache": categories,
        "value_cache": values,
    }


def _cache_facts(ref: ET.Element | None) -> dict[str, Any]:
    """Cached points under a strRef/numRef reference (evidence, not truth).

    ``formula`` is the ``c:f`` reference string the producer wrote (sheet,
    range or external ``[Book]!`` reference) — recorded verbatim, never
    evaluated.
    """
    formula = (ref.findtext(f".//{{{_C_NS}}}f") or "").strip() if ref is not None else ""
    if ref is None:
        return {"point_count": 0, "values": [], "cache_type": None, "formula": ""}
    for cache_name in ("strCache", "numCache", "multiLvlStrCache"):
        cache = ref.find(f".//{{{_C_NS}}}{cache_name}")
        if cache is None:
            continue
        points = [
            (point.findtext(f"{{{_C_NS}}}v") or "").strip()
            for point in cache.findall(f"{{{_C_NS}}}pt")
        ]
        return {
            "point_count": len(points),
            "values": points[:_MAX_CACHED_POINT_SAMPLES],
            "cache_type": cache_name,
            "formula": formula,
        }
    return {"point_count": 0, "values": [], "cache_type": None, "formula": formula}


def _ext_excerpt(ext: ET.Element) -> str:
    """Concatenated text nodes of one ``c:ext`` (foreign-namespace XML).

    The extension content is never parsed; visible text is recorded as
    evidence, hatted.
    """
    text = "".join(node.text or "" for node in ext.iter())
    return text.strip()[:_MAX_EXT_EXCERPT_CHARS]


def _chart_part_rels(
    package: zipfile.ZipFile, part_name: str
) -> dict[str, dict[str, str]]:
    """The chart part's own rels → rId → {target, target_mode}.

    ``word/charts/_rels/chartN.xml.rels`` holds the ``c:externalData``
    relationship (external workbook links).  Unreadable or absent rels
    return an empty map (the external_data fact then reports
    ``rel_resolved: False``).
    """
    directory, _, base = part_name.rpartition("/")
    rels_name = f"{directory}/_rels/{base}.rels"
    rels: dict[str, dict[str, str]] = {}
    try:
        if rels_name in package.namelist():
            root = ET.fromstring(package.read(rels_name))
            for rel in root:
                rels[rel.get("Id") or ""] = {
                    "target": rel.get("Target") or "",
                    "target_mode": rel.get("TargetMode") or "",
                }
    except (KeyError, ET.ParseError):
        pass
    return rels


def _chart_facts(
    chart_xml: bytes,
    part_name: str,
    chart_index: int,
    reference: dict[str, Any] | None,
    chart_rels: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    root = ET.fromstring(chart_xml)
    chart = root.find(f"{{{_C_NS}}}chart")
    plot_area = chart.find(f"{{{_C_NS}}}plotArea") if chart is not None else None

    xml_type_names: list[str] = []
    bar_direction = None
    if plot_area is not None:
        for child in plot_area:
            name = _local(child.tag)
            if not name.endswith("Chart"):
                continue
            xml_type_names.append(name)
            if name == "barChart":
                bar_dir = child.find(f"{{{_C_NS}}}barDir")
                bar_direction = bar_dir.get("val") if bar_dir is not None else None

    normalized = sorted({_CHART_TYPE_BY_XML_NAME.get(name, name) for name in xml_type_names})
    if not normalized:
        chart_type = "UnknownChart"
    elif len(normalized) == 1:
        chart_type = normalized[0]
    else:
        chart_type = "MixedChart"

    title = _text_of(chart.find(f"{{{_C_NS}}}title")) if chart is not None else ""

    series = [
        _series_facts(serie)
        for serie in (
            plot_area.findall(f".//{{{_C_NS}}}ser") if plot_area is not None else []
        )
    ]

    legend = chart.find(f"{{{_C_NS}}}legend") if chart is not None else None
    legend_position = None
    if legend is not None:
        legend_pos = legend.find(f"{{{_C_NS}}}legendPos")
        legend_position = legend_pos.get("val") if legend_pos is not None else None
    axes = {
        "category_axis_count": len(
            plot_area.findall(f"{{{_C_NS}}}catAx")
        )
        + len(plot_area.findall(f"{{{_C_NS}}}dateAx"))
        if plot_area is not None
        else 0,
        "value_axis_count": len(plot_area.findall(f"{{{_C_NS}}}valAx"))
        if plot_area is not None
        else 0,
        "series_axis_count": len(plot_area.findall(f"{{{_C_NS}}}serAx"))
        if plot_area is not None
        else 0,
    }
    axis_titles = []
    if plot_area is not None:
        for axis in list(plot_area.findall(f"{{{_C_NS}}}catAx")) + list(
            plot_area.findall(f"{{{_C_NS}}}valAx")
        ):
            axis_title = _text_of(axis.find(f"{{{_C_NS}}}title"))
            if axis_title:
                axis_titles.append(axis_title)

    inference: dict[str, Any] = {
        "part": part_name,
        "chart_types": normalized,
        "xml_type_names": xml_type_names,
        "bar_direction": bar_direction,
        "legend": (
            {
                "present": True,
                "position": legend_position,
            }
            if legend is not None
            else {"present": False, "position": None}
        ),
        "axes": {**axes, "axis_titles": axis_titles},
        "series": series[:_MAX_SERIES_SAMPLES],
        "referenced_from_document": reference is not None,
    }
    if reference is not None:
        inference.update(
            {
                "placement": reference["placement"],
                "paragraph_index": reference["paragraph_index"],
                "name": reference["name"],
                "alt_text": reference["alt_text"],
            }
        )

    # c:externalData — the chart's own relationship to its data workbook
    # (external links are the dropper-relevant face); resolved against the
    # chart part's own rels, recorded verbatim, never followed.
    external_data_el = root.find(f"{{{_C_NS}}}externalData")
    if external_data_el is not None:
        rel_id = external_data_el.get(f"{{{_R_NS}}}id") or ""
        rel = (chart_rels or {}).get(rel_id)
        auto_update = external_data_el.find(f"{{{_C_NS}}}autoUpdate")
        inference["external_data"] = {
            "rel_id": rel_id,
            "target": rel["target"] if rel else "",
            "target_mode": rel["target_mode"] if rel else "",
            "rel_resolved": rel is not None,
            "auto_update": (
                auto_update.get("val") if auto_update is not None else None
            ),
        }

    # c:extLst — chartSpace extension list (arbitrary foreign-namespace
    # XML); census of uris with hatted text excerpts, content never parsed.
    ext_lst = root.find(f"{{{_C_NS}}}extLst")
    if ext_lst is not None:
        extensions = ext_lst.findall(f"{{{_C_NS}}}ext")
        inference["extension_list"] = {
            "extension_count": len(extensions),
            "extensions": [
                {
                    "uri": extension.get("uri") or "",
                    "content_excerpt": _ext_excerpt(extension),
                }
                for extension in extensions[:_MAX_EXT_SAMPLES]
            ],
        }

    # Contract-aligned keys (same shape as the spreadsheet chart facts):
    # id / title / chart_type / anchor / series_count / source / inference.
    return {
        "id": f"chart:{chart_index}",
        "title": title,
        "chart_type": chart_type,
        "anchor": (
            f"paragraph:{reference['paragraph_index']}" if reference else None
        ),
        "series_count": len(series),
        "source": "docx_chart_xml",
        "inference": inference,
    }


def extract_chart_summary(doc, file_path: str | Path) -> dict[str, Any]:
    """Collect chart-part evidence from one docx package.

    ``doc`` is an opened python-docx Document (kept for interface symmetry
    with the other docx evidence modules); the chart parts are read straight
    from the package zip because python-docx does not model them.
    """
    path = Path(file_path)
    charts: list[dict[str, Any]] = []
    chart_part_error: str | None = None
    try:
        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            chart_parts = sorted(
                name for name in names if re.fullmatch(r"word/charts/chart\d+\.xml", name)
            )
            references: dict[str, dict[str, Any]] = {}
            by_target: dict[str, str] = {}
            if "word/document.xml" in names:
                references = _chart_references(package.read("word/document.xml"))
            if "word/_rels/document.xml.rels" in names:
                by_target = _chart_rels_by_target(package.read("word/_rels/document.xml.rels"))
            for index, part_name in enumerate(chart_parts[:_MAX_CHARTS], start=1):
                reference = None
                for rel_id, target in by_target.items():
                    if target == part_name or target == part_name.removeprefix("word/"):
                        reference = references.get(rel_id)
                        break
                charts.append(
                    _chart_facts(
                        package.read(part_name),
                        part_name,
                        index,
                        reference,
                        _chart_part_rels(package, part_name),
                    )
                )
    except (zipfile.BadZipFile, ET.ParseError, OSError) as error:
        chart_part_error = str(error)

    summary: dict[str, Any] = {
        "chart_count": len(charts),
        "charts_with_title": sum(1 for chart in charts if chart["title"]),
        "charts_with_legend": sum(
            1
            for chart in charts
            if chart["inference"]["legend"]["present"]
        ),
        "mixed_chart_count": sum(
            1 for chart in charts if chart["chart_type"] == "MixedChart"
        ),
        "series_total": sum(chart["series_count"] for chart in charts),
        "series_with_cached_values": sum(
            1
            for chart in charts
            for serie in chart["inference"]["series"]
            if serie["value_cache"]["point_count"]
        ),
        "unanchored_chart_count": sum(
            1 for chart in charts if chart["anchor"] is None
        ),
        "charts_with_external_data": sum(
            1 for chart in charts if "external_data" in chart["inference"]
        ),
        "samples": charts[:_MAX_CHARTS],
        "note": (
            "chart XML facts (types, series, cached category/value points, "
            "legend, axes) are recorded with the same key set as the "
            "spreadsheet chart contract; cached values are evidence of what "
            "the producer wrote and are never recomputed; series reference "
            "formulas, external-data workbook links and chartSpace extension "
            "lists are registered verbatim and never followed, evaluated or "
            "parsed"
        ),
    }
    if chart_part_error:
        summary["chart_part_error"] = chart_part_error
    return summary


def _chart_rels_by_target(rels_xml: bytes) -> dict[str, str]:
    """Map relationship id -> chart part target from document.xml.rels."""
    root = ET.fromstring(rels_xml)
    mapping: dict[str, str] = {}
    for rel in root:
        if rel.get("Type") != _RELS_TYPE_CHART:
            continue
        rel_id = rel.get("Id")
        target = rel.get("Target") or ""
        if not rel_id:
            continue
        # Targets are relative to word/ (e.g. "charts/chart1.xml").
        mapping[rel_id] = (
            f"word/{target.lstrip('/')}" if not target.startswith("word/") else target
        )
    return mapping


__all__ = ["extract_chart_summary"]
