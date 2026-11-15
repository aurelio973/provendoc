"""Navigation-backed heading refinement over reconstructed PDF blocks.

The bookmark tree is author-declared structure, so it outranks font-size
heuristics wherever an anchor and a block provably refer to the same title.
This reconstructor never invents headings:

* a heading block is re-levelled only when a bookmark on the same page
  matches its text (the previous level survives in ``navigation_ref``);
* a short text block is promoted to a heading only when a bookmark on the
  same page matches its text (candidate-grade confidence, source recorded);
* printed TOC lines (dot leaders, trailing page numbers) are *annotated*,
  never removed — their agreement with bookmark anchors is reported as
  statistics, and the trailing number is resolved through declared page
  labels when the PDF defines them.
"""

from __future__ import annotations

import re
from typing import Any

from provendoc.core.parsers.narrative.pdf.evidence.navigation import (
    NavigationEvidence,
)
from provendoc.core.structure.heading_patterns import MAX_HEADING_LEN
from provendoc.core.structure.title_matching import (
    normalize_title_text,
    strip_trailing_page_number,
    title_similarity,
    titles_equivalent,
)

# Printed TOC line: title, then dot leaders (or wide gaps), then a page
# reference that may be arabic or roman digits.
_TOC_LINE_RE = re.compile(
    r"^(?P<title>.{2,120}?)"
    r"(?P<leader>\s(?:[.·•⋯\u2026]{2,}|\s{3,})\s*)"
    r"(?P<page>\d{1,4}|[ivxlcdmIVXLCDM]{1,7})$"
)

_PROMOTE_MAX_TEXT_LEN = MAX_HEADING_LEN


def _strictly_equivalent(text: str, title: str) -> bool:
    """Near-exact title agreement for type-changing decisions.

    Accepts normalized equality or fuzzy similarity at the shared threshold,
    but rejects prefix containment: a bookmark titled "1.1 Architecture" is
    not sufficient evidence to promote the body sentence "Architecture body
    text." into a heading, even though the bookmark title is contained in it.
    """

    left = normalize_title_text(strip_trailing_page_number(text))
    right = normalize_title_text(strip_trailing_page_number(title))
    if not left or not right:
        return False
    return left == right or title_similarity(left, right) >= 0.85


class NavigationReconstructor:
    """Apply bookmark evidence to reconstructed blocks, conservatively."""

    def apply(
        self,
        blocks: list[dict[str, Any]],
        navigation: NavigationEvidence | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Refine blocks in place; return them with a navigation report."""

        info: dict[str, Any] = {
            "bookmark_count": 0,
            "matched_heading_count": 0,
            "level_adjusted_count": 0,
            "promoted_text_block_count": 0,
            "toc_entry_block_count": 0,
            "toc_line_count": 0,
            "toc_bookmark_agreement_count": 0,
            "matched_bookmark_ids": [],
            "unmatched_bookmark_ids": [],
            "links": {"enabled": False, "reason": "not evaluated"},
            "enabled": False,
        }
        if navigation is None:
            info["reason"] = "navigation evidence unavailable"
            return blocks, info

        # Link evidence is independent of the bookmark tree: report it even
        # when no bookmarks exist (URI-only PDFs are common).
        info["links"] = self._link_report(navigation)

        flat = [node for node in navigation.flat_bookmarks() if node.page_number >= 1]
        info["bookmark_count"] = len(flat)
        if not flat:
            info["reason"] = "no bookmark anchors with valid pages"
            self._annotate_toc_lines(blocks, navigation, info)
            info["enabled"] = bool(navigation.page_label_ranges)
            return blocks, info

        # Bookmark levels are 1-based as stored; normalize so the shallowest
        # bookmark maps to heading level 1 (trees that start at 2 are common
        # authoring defects and would otherwise orphan every heading).
        min_level = min(node.level for node in flat)

        by_page: dict[int, list[Any]] = {}
        for node in flat:
            by_page.setdefault(node.page_number, []).append(node)

        matched_ids: set[str] = set()
        for block in blocks:
            block_type = block.get("type")
            if block_type not in {"heading", "text"}:
                continue
            text = str(block.get("text") or "").strip()
            if not text:
                continue

            page = block.get("page")
            candidates = by_page.get(page) if isinstance(page, int) else None
            if candidates:
                # Promotion changes the block type, so it demands a strong
                # match; re-levelling an existing heading accepts the looser
                # bookmark-truncation tolerance.
                node = self._best_match(
                    text,
                    candidates,
                    block.get("bbox"),
                    strict=(block_type != "heading"),
                )
                if node is not None:
                    normalized_level = max(1, node.level - min_level + 1)
                    reference = {
                        "bookmark_id": node.bookmark_id,
                        "title": node.title,
                        "level": node.level,
                        "normalized_level": normalized_level,
                        "similarity": round(
                            title_similarity(text, node.title), 3
                        ),
                        "source": "bookmark_tree",
                        "anchor_page": node.page_number,
                    }
                    matched_ids.add(node.bookmark_id)
                    if block_type == "heading":
                        previous_level = block.get("heading_level")
                        block["heading_level"] = normalized_level
                        block["confidence"] = max(
                            float(block.get("confidence", 0.0)), 0.9
                        )
                        block["heading_level_source"] = "bookmark"
                        block["navigation_ref"] = reference
                        info["matched_heading_count"] += 1
                        if previous_level != normalized_level:
                            reference["previous_level"] = previous_level
                            info["level_adjusted_count"] += 1
                    elif len(text) <= _PROMOTE_MAX_TEXT_LEN:
                        # A bookmark on this page whose title matches this
                        # short text block: the font heuristic missed a real
                        # heading.  Promote at candidate grade, provenance
                        # recorded — never presented as font-derived.
                        block["type"] = "heading"
                        block["heading_level"] = normalized_level
                        block["confidence"] = 0.7
                        block["heading_level_source"] = "bookmark_promotion"
                        block["navigation_ref"] = reference
                        info["promoted_text_block_count"] += 1

        self._annotate_toc_lines(
            [block for block in blocks if block.get("type") in {"text", "heading"}],
            navigation,
            info,
        )

        info["matched_bookmark_ids"] = sorted(matched_ids)
        info["unmatched_bookmark_ids"] = [
            node.bookmark_id for node in flat if node.bookmark_id not in matched_ids
        ]
        info["enabled"] = True
        info["min_bookmark_level"] = min_level
        return blocks, info

    _LINK_SAMPLE_CAP = 200

    @staticmethod
    def _link_report(navigation: Any) -> dict[str, Any]:
        """Resolve link evidence against named destinations — report only.

        Internal GOTO targets are range-checked against the page count;
        named-destination links are resolved via the recorded table (a name
        that does not resolve is reported broken, never guessed).  Nothing
        here rewrites blocks: links are navigation facts.
        """
        links = getattr(navigation, "links", None) or []
        page_count = getattr(navigation, "page_count", 0) or 0
        named_destinations = getattr(navigation, "named_destinations", None) or {}

        kind_counts = {
            "internal": 0,
            "external": 0,
            "named": 0,
            "launch": 0,
            "remote": 0,
            "other": 0,
        }
        internal_links: list[dict[str, Any]] = []
        broken_internal_links: list[dict[str, Any]] = []
        unresolved_named_links: list[dict[str, Any]] = []
        external_uris: set[str] = set()
        resolved_named_count = 0
        valid_internal_count = 0

        for link in links:
            kind = link.get("kind", "other")
            if kind not in kind_counts:
                kind = "other"
            kind_counts[kind] += 1
            anchor_text = link.get("anchor_text", "")

            if kind == "internal":
                entry = {
                    "page": link.get("page"),
                    "target_page": link.get("target_page"),
                    "anchor_text": anchor_text,
                }
                target = link.get("target_page")
                if isinstance(target, int) and 1 <= target <= page_count:
                    valid_internal_count += 1
                    if len(internal_links) < NavigationReconstructor._LINK_SAMPLE_CAP:
                        internal_links.append(entry)
                else:
                    entry["reason"] = "target page missing or out of range"
                    if len(broken_internal_links) < NavigationReconstructor._LINK_SAMPLE_CAP:
                        broken_internal_links.append(entry)
            elif kind == "named":
                name = link.get("nameddest")
                destination = named_destinations.get(name) if name else None
                target = destination.get("page") if isinstance(destination, dict) else None
                if isinstance(target, int) and 1 <= target <= page_count:
                    resolved_named_count += 1
                    if len(internal_links) < NavigationReconstructor._LINK_SAMPLE_CAP:
                        internal_links.append(
                            {
                                "page": link.get("page"),
                                "target_page": target,
                                "nameddest": name,
                                "anchor_text": anchor_text,
                            }
                        )
                else:
                    if len(unresolved_named_links) < NavigationReconstructor._LINK_SAMPLE_CAP:
                        unresolved_named_links.append(
                            {
                                "page": link.get("page"),
                                "nameddest": name,
                                "anchor_text": anchor_text,
                            }
                        )
            elif kind == "external":
                uri = link.get("uri")
                if uri:
                    external_uris.add(uri)

        return {
            "enabled": True,
            "link_count": len(links),
            "kind_counts": kind_counts,
            "internal_link_count": valid_internal_count + resolved_named_count,
            "resolved_named_count": resolved_named_count,
            "external_uri_count": len(external_uris),
            "external_uris": sorted(external_uris)[
                : NavigationReconstructor._LINK_SAMPLE_CAP
            ],
            "internal_links": internal_links,
            "broken_internal_link_count": len(broken_internal_links),
            "broken_internal_links": broken_internal_links,
            "unresolved_named_link_count": len(unresolved_named_links),
            "unresolved_named_links": unresolved_named_links,
        }

    def _best_match(
        self,
        text: str,
        candidates: list[Any],
        bbox: Any,
        *,
        strict: bool = False,
    ) -> Any:
        """Pick the bookmark on this page that most plausibly is this title.

        ``strict=True`` rejects prefix-containment matches: a bookmark titled
        "1.1 Architecture" must not promote the body sentence "Architecture
        body text." on the same page, because promotion rewrites the block
        type and therefore requires near-exact agreement.
        """

        best_node = None
        best_score = -1.0
        for node in candidates:
            if strict:
                if not _strictly_equivalent(text, node.title):
                    continue
            elif not titles_equivalent(text, node.title):
                continue
            score = title_similarity(text, node.title)
            # Break ties by anchor proximity to the block's top edge.
            dest_y = (node.dest or {}).get("to")
            if isinstance(dest_y, list) and len(dest_y) == 2 and bbox:
                try:
                    proximity = 1.0 / (
                        1.0 + abs(float(dest_y[1]) - float(bbox[1])) / 100.0
                    )
                    score += proximity * 0.01
                except (TypeError, ValueError):
                    pass
            if score > best_score:
                best_score = score
                best_node = node
        return best_node

    def _annotate_toc_lines(
        self,
        blocks: list[dict[str, Any]],
        navigation: NavigationEvidence,
        info: dict[str, Any],
    ) -> None:
        """Annotate printed TOC lines and measure bookmark agreement."""

        label_pages = navigation.label_page_map() if navigation.page_label_ranges else {}
        flat_titles: dict[int, list[str]] = {}
        for node in navigation.flat_bookmarks():
            if node.page_number >= 1:
                flat_titles.setdefault(node.page_number, []).append(node.title)

        for block in blocks:
            text = str(block.get("text") or "")
            if not text or len(text) > 2000:
                continue
            lines = text.splitlines()
            matches = [
                match
                for match in (_TOC_LINE_RE.match(line.strip()) for line in lines)
                if match is not None
            ]
            if not matches:
                continue

            annotations: list[dict[str, Any]] = []
            for match in matches:
                page_ref = match.group("page")
                resolved = self._resolve_page_reference(page_ref, label_pages)
                annotation: dict[str, Any] = {
                    "title": normalize_title_text(match.group("title")),
                    "page_reference": page_ref,
                    "resolved_page": resolved,
                }
                if resolved is not None:
                    agreed = any(
                        titles_equivalent(
                            strip_trailing_page_number(match.group("title")), title
                        )
                        for title in flat_titles.get(resolved, [])
                    )
                    annotation["bookmark_agreement"] = agreed
                    if agreed:
                        info["toc_bookmark_agreement_count"] += 1
                annotations.append(annotation)

            payload = block.setdefault("raw_payload", {})
            if not isinstance(payload, dict):
                continue
            payload["toc_entry_lines"] = annotations
            info["toc_entry_block_count"] += 1
            info["toc_line_count"] += len(annotations)

    @staticmethod
    def _resolve_page_reference(
        page_ref: str,
        label_pages: dict[str, int],
    ) -> int | None:
        """Resolve a printed page reference to a physical page number.

        Uses the PDF's declared label map first (roman prefaces resolve
        through it); a bare arabic number falls back to identity only when
        no labels are declared at all, mirroring how unlabeled documents
        print physical numbering.
        """

        if label_pages:
            if page_ref in label_pages:
                return label_pages[page_ref]
            lowered = page_ref.lower()
            for label, page in label_pages.items():
                if label.lower() == lowered:
                    return page
            return None
        if page_ref.isdigit():
            return int(page_ref)
        return None
