# ============================================================
# CONTAINER NARRATIVE REPRESENTATION (shared base)
# ============================================================
"""Container-aware narrative representation base (pptx slides /
epub chapters).

Differences from the shared Txt representation layer:

1. Container locality: base sections are hard-clipped to the
   container spans the parser provides (pptx_slides /
   epub_chapter_spans) — a section never spills out of its slide or
   chapter; coarse merges never cross containers thanks to the
   CoarseGrainedConfig.container_locality barrier. Under the shared
   Txt layer, untitled-section content flows into the previous
   chapter's last section, and sibling merging can glue two
   chapters or two slides into one coarse pack — the container's
   native semantic boundary is lost there.
2. Per-unit container provenance: every base unit carries
   container_kind / container_index / slide_index|slide_title /
   chapter_index|chapter_href; the coarse layer aggregates
   container_span, the fine layer passes it through.
3. Honest degradation: when container span facts are missing
   (hand-built Documents / old parser output), fall back entirely
   to the shared Txt behaviour and record container_locality=False
   plus the reason in the result metadata — never fabricate
   container facts.

The section/paragraph splitting machinery (fences / bare code /
ASCII art / size hard limits) is fully reused from
TxtNarrativeRepresentation, not reimplemented here.
"""

from typing import Optional

from provendoc.base.models.representation_result import RepresentationResult
from provendoc.base.models.representation_unit import (
    RepresentationUnit,
    SourceContext,
    StructuralInfo,
    UnitType,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    TxtNarrativeRepresentation,
    _is_splittable_prose,
    _split_at_hard_fallback,
    _split_with_furniture,
)
from provendoc.core.structure.document_tree import NodeType


class ContainerNarrativeRepresentation(TxtNarrativeRepresentation):
    """Container-aware narrative representation base — span-driven
    section/paragraph units.

    Subclass contract (class attributes):
        container_kind: container kind name ("slide" / "chapter").
        container_spans_key: metadata key of the span list the
            parser provides (each item has at least start/end plus
            an index field).
        span_index_field: field name of the container index inside
            a span item.
        container_index_meta_key: unit-metadata key for the
            container index (slide_index / chapter_index).
        span_label_field / container_label_meta_key: optional
            container label fields (slide title / chapter href),
            None skips labelling.
    """

    # Subclasses must provide the first four hooks; the two label
    # fields are optional (None skips label provenance). Annotation
    # only, no assignment: a subclass lacking full hooks fails with
    # AttributeError on first access instead of silently writing
    # None into the output.
    container_kind: str
    container_spans_key: str
    span_index_field: str
    container_index_meta_key: str
    span_label_field: Optional[str] = None
    container_label_meta_key: Optional[str] = None

    # Whole-container aggregation switch: when True, one container
    # span yields one atomic base unit (title line / body / notes
    # all inside the span slice); splittable prose over
    # size_hard_limit degrades to line-boundary splitting (same
    # contract as paragraphs); each unit carries container_compact
    # provenance. Default False — subclasses that do not enable it
    # (epub/odp) see zero behaviour change.
    compact_container: bool = False

    # Section aggregation switch: when True, every heading section
    # inside a container (preamble included) yields one atomic base
    # unit — a verbatim interval slice (title line included), no
    # longer paragraphised on blank lines. Meant for long-form
    # containers (epub chapters): a chapter can run to hundreds of
    # thousands of characters, whole-container aggregation would
    # just hit the size fallback, so the aggregation atom is the
    # section. Over-limit degradation follows the same contract as
    # pptx compact (line-boundary splitting, hard fallback for
    # giant single lines). Each unit carries section_compact
    # provenance. Default False — subclasses that do not enable it
    # see zero behaviour change.
    compact_sections: bool = False

    # ========================================================
    # public
    # ========================================================

    def build(self, document):
        """Build narrative three-granularity units from container
        spans + the structure tree.

        With container spans missing, honestly degrade to the
        shared Txt behaviour (no fabricated container facts);
        otherwise produce base units with container provenance and
        enable the container_locality barrier on the coarse layer.

        Args:
            document: Document (metadata carries container spans
                and trusted heading_candidates).

        Returns:
            RepresentationResult: representation_type="narrative".
        """
        spans = (document.metadata or {}).get(self.container_spans_key)
        if not spans:
            result = super().build(document)
            result.metadata["container_kind"] = self.container_kind
            result.metadata["container_locality"] = False
            result.metadata["container_locality_reason"] = (
                f"missing {self.container_spans_key} metadata — "
                "degraded to shared txt narrative behaviour"
            )
            return result

        units = self._build_container_base_units(document, spans)
        output = self._run_granularity(units)

        return RepresentationResult(
            representation_type="narrative",
            content=output,
            metadata={
                "base_units": len(output.base),
                "coarse_units": len(output.coarse),
                "fine_units": len(output.fine),
                "structure_runtime": True,
                "container_kind": self.container_kind,
                "container_count": len(spans),
                "container_locality": True,
            },
        )

    # ========================================================
    # subclass hooks
    # ========================================================

    def _coarse_config(self):
        """Coarse config for container-aware layers: enable the container-locality barrier."""
        config = super()._coarse_config()
        config.container_locality = True
        return config

    # ========================================================
    # span-driven base units
    # ========================================================

    def _container_extra(self, span):
        """span → per-unit container provenance metadata."""
        index = span.get(self.span_index_field)
        extra = {
            "container_kind": self.container_kind,
            "container_index": index,
            self.container_index_meta_key: index,
        }
        if self.span_label_field is not None:
            label = span.get(self.span_label_field)
            if label is not None:
                extra[self.container_label_meta_key] = label
        return extra

    def _build_container_base_units(self, document, spans):
        """Build base units clipped to container spans.

        Same machinery as the parent _build_base_units; the only
        differences: iterate container spans (not the whole
        document), hard-clip sections at container boundaries,
        body text before the first heading inside a container
        belongs to that container (untitled sections no longer flow
        into the previous container), and every unit carries
        container provenance.
        """
        tree = self.structure_analyzer.analyze(document)
        text = document.text
        units = []
        unit_index = 0

        parser_candidates = bool(
            (document.metadata or {}).get("heading_candidates")
        )
        heading_nodes = sorted(
            (
                n
                for n in tree.nodes.values()
                if n.node_type == NodeType.HEADING and n.level > 0
            ),
            key=lambda n: n.text_start,
        )
        src_ctx = SourceContext(
            file_name=document.metadata.get("file_name"),
            file_type=document.metadata.get("file_type"),
        )
        size_hard_limit = self.policy.packaging.size_hard_limit

        for span in spans:
            span_start = max(0, min(int(span.get("start", 0)), len(text)))
            span_end = max(
                span_start, min(int(span.get("end", len(text))), len(text))
            )
            extra = self._container_extra(span)

            nodes_in = [
                n for n in heading_nodes
                if span_start <= n.text_start < span_end
            ]

            if self.compact_container:
                # Whole-container aggregation: one container, one
                # atomic unit — no more paragraph-level fragmentation.
                unit_index = self._add_compact_container_unit(
                    text, span, span_start, span_end, nodes_in, tree,
                    units, unit_index, src_ctx, size_hard_limit, extra,
                )
                continue

            if self.compact_sections:
                # Section aggregation: one atomic unit per heading
                # section inside the container.
                unit_index = self._add_compact_section_units(
                    text, span_start, span_end, nodes_in, tree,
                    units, unit_index, src_ctx, parser_candidates,
                    size_hard_limit, extra,
                )
                continue

            # Container preamble: from the container start to its
            # first heading — belongs to this container (heading
            # levels cannot claim across containers).
            pre_end = nodes_in[0].text_start if nodes_in else span_end
            preamble_text = text[span_start:pre_end].strip()
            if preamble_text:
                unit_index = self._add_paragraph_units(
                    preamble_text, units, unit_index,
                    heading="", heading_level=0,
                    heading_path=[],
                    src_ctx=src_ctx,
                    size_hard_limit=size_hard_limit,
                    extra_metadata=extra,
                )

            for idx, node in enumerate(nodes_in):
                start = node.text_end
                if idx + 1 < len(nodes_in):
                    end = nodes_in[idx + 1].text_start
                else:
                    end = span_end
                # Container barrier: a section never spills out of
                # its container.
                end = min(end, span_end)

                section_text = text[start:end].strip()
                heading_path = [
                    h.title
                    for h in tree.get_heading_ancestors(node.node_id)
                    if h.level > 0
                ] + [node.title]

                if not section_text:
                    unit_index = self._add_outline_or_fallback_unit(
                        node, heading_path, units, unit_index,
                        src_ctx, parser_candidates, extra,
                    )
                    continue

                # The heading line joins the section head (same c4
                # line-integrity contract as the parent).
                heading_line = (
                    text[node.text_start:node.text_end].strip()
                    or node.title
                )
                if heading_line:
                    section_text = heading_line + "\n" + section_text

                unit_index = self._add_paragraph_units(
                    section_text, units, unit_index,
                    heading=node.title,
                    heading_level=node.level,
                    heading_path=heading_path,
                    src_ctx=src_ctx,
                    structure_provenance=node.metadata.get(
                        "structure_provenance"
                    ),
                    size_hard_limit=size_hard_limit,
                    extra_metadata=extra,
                )

        # Defensive: regions the spans do not cover (hand-built
        # metadata / future parser changes) — content must not
        # evaporate in the representation layer; emit them as
        # paragraph units without container identity, honestly
        # tagged container_gap.
        unit_index = self._add_gap_units(
            text, spans, units, unit_index, src_ctx, size_hard_limit,
        )
        return units

    # ========================================================
    # compact container units
    # ========================================================

    def _add_compact_container_unit(
        self, text, span, span_start, span_end, nodes_in, tree,
        units, unit_index, src_ctx, size_hard_limit, extra,
    ):
        """Whole-container aggregation: one container span, one
        atomic base unit.

        A slide's native semantic boundary is the page (one item
        per page; pages carry only a page title), and the span
        slice is the whole page text (title line / body / notes
        inside the span) — paragraphising on blank lines would
        shatter each text-box paragraph into its own unit, so the
        page stays atomic here. Splittable prose over
        size_hard_limit degrades to line-boundary splitting (same
        contract as paragraphs; the size limit is not voided by
        aggregation); integrity blocks (fences/tables) are not cut
        and are tagged oversized.
        """
        container_text = text[span_start:span_end].strip()
        if not container_text:
            # Empty-text container: nothing to lose, no
            # outline_only unit.
            return unit_index

        if nodes_in:
            # Heading provenance: the first heading node inside the
            # span (one item per page; when multiple headings occur,
            # take the first — the rest stay in the unit text).
            node = nodes_in[0]
            heading = node.title
            heading_level = node.level
            heading_path = [
                h.title
                for h in tree.get_heading_ancestors(node.node_id)
                if h.level > 0
            ] + [node.title]
            provenance = node.metadata.get("structure_provenance")
        else:
            # No heading node: fall back to the span label (slide
            # title, never fabricated from parser facts); carry no
            # structure_provenance (no evidence source).
            label = (
                span.get(self.span_label_field)
                if self.span_label_field is not None
                else None
            )
            heading = label or ""
            heading_level = 0
            heading_path = [label] if label else []
            provenance = None

        return self._emit_compact_units(
            container_text, heading, heading_level, heading_path,
            provenance, units, unit_index, src_ctx, size_hard_limit,
            extra, "container_compact",
        )

    def _add_compact_section_units(
        self, text, span_start, span_end, nodes_in, tree,
        units, unit_index, src_ctx, parser_candidates,
        size_hard_limit, extra,
    ):
        """Section aggregation: one atomic unit per heading section
        inside a container.

        Section partitioning is identical to the non-compact path
        (preamble + per-heading intervals); the only difference is
        emission: a verbatim interval slice (title line included,
        naturally satisfying the "heading line joins the section
        head" contract) instead of blank-line paragraphisation —
        laying a long novel out as paragraph atoms would produce
        thousands of fragments, so the aggregation atom is the
        section. Empty sections with a node → outline/fallback per
        the existing contract.
        """
        # (node, start, end) sequence: preamble + per-heading
        # intervals.
        boundaries = [(None, span_start)]
        boundaries.extend((n, n.text_start) for n in nodes_in)

        def _section_end(k):
            return (
                boundaries[k + 1][1] if k + 1 < len(boundaries)
                else span_end
            )

        # Front-matter folding: a run of ≥2 consecutive
        # heading-only sections anywhere in the document (no body
        # between the headings — the shape signature of a title
        # page with book title / subtitle / author) is merged into
        # a single front-matter unit. Runs of length ≤1 cause zero
        # drift (no folding, no extra key); the preamble and empty
        # sections (outline/fallback contract) never join a run;
        # compact_sections is enabled only by the epub subclass,
        # pptx/odp untouched.
        heading_only = [False]  # index 0 = preamble, never folded
        for k, (node, start) in enumerate(boundaries[1:], start=1):
            section_text = text[start:_section_end(k)].strip()
            if not section_text:
                heading_only.append(False)
                continue
            heading_line = (
                text[node.text_start:node.text_end].strip()
                or node.title
            )
            heading_only.append(self._is_heading_only_section(
                section_text, heading_line
            ))

        k = 0
        while k < len(boundaries):
            node, start = boundaries[k]
            section_text = text[start:_section_end(k)].strip()

            if node is None:
                # preamble: container start to the first heading —
                # belongs to this container.
                if section_text:
                    unit_index = self._emit_compact_units(
                        section_text, "", 0, [], None,
                        units, unit_index, src_ctx, size_hard_limit,
                        extra, "section_compact",
                    )
                k += 1
                continue

            if heading_only[k] and k + 1 < len(boundaries) and heading_only[k + 1]:
                # Run start (≥2 consecutive heading-only): fold into
                # a single front-matter unit. Heading takes the
                # first node's title; heading_path takes the last
                # node's full chain (book-title/author chain
                # preserved).
                run_end = k
                while run_end < len(boundaries) and heading_only[run_end]:
                    run_end += 1
                first_node = boundaries[k][0]
                last_node = boundaries[run_end - 1][0]
                merged_text = "\n\n".join(
                    text[boundaries[j][1]:_section_end(j)].strip()
                    for j in range(k, run_end)
                )
                fm_heading_path = [
                    h.title
                    for h in tree.get_heading_ancestors(last_node.node_id)
                    if h.level > 0
                ] + [last_node.title]
                unit_index = self._emit_compact_units(
                    merged_text, first_node.title, first_node.level,
                    fm_heading_path,
                    first_node.metadata.get("structure_provenance"),
                    units, unit_index, src_ctx, size_hard_limit,
                    {**extra, "front_matter": True}, "section_compact",
                )
                k = run_end
                continue

            heading_path = [
                h.title
                for h in tree.get_heading_ancestors(node.node_id)
                if h.level > 0
            ] + [node.title]
            if not section_text:
                # Empty heading section: outline_only / fallback
                # per the same contract.
                unit_index = self._add_outline_or_fallback_unit(
                    node, heading_path, units, unit_index,
                    src_ctx, parser_candidates, extra,
                )
            else:
                unit_index = self._emit_compact_units(
                    section_text, node.title, node.level, heading_path,
                    node.metadata.get("structure_provenance"),
                    units, unit_index, src_ctx, size_hard_limit,
                    extra, "section_compact",
                )
            k += 1
        return unit_index

    @staticmethod
    def _is_heading_only_section(section_text, heading_line):
        """Front-matter folding criterion: whether every non-empty
        line of the interval slice is the heading line itself —
        the observed shape signature of a title-page section
        (book title / author). Conservative failure: any
        non-heading line disables folding.
        """
        if not heading_line:
            return False
        lines = [ln.strip() for ln in section_text.splitlines() if ln.strip()]
        return bool(lines) and all(ln == heading_line for ln in lines)

    def _emit_compact_units(
        self, section_text, heading, heading_level, heading_path,
        provenance, units, unit_index, src_ctx, size_hard_limit,
        extra, compact_key,
    ):
        """Compact emission machinery (shared by pptx whole-container
        and epub section aggregation).

        Size degradation: same machinery as the paragraph contract —
        splittable prose over the hard limit is split at line
        boundaries (never mid-line), with a hard fallback for
        giant single lines; integrity blocks are not cut and are
        tagged oversized. Per-piece provenance is isomorphic;
        every unit carries the compact key.
        """
        # Size degradation: same machinery as the paragraph
        # contract, per-piece provenance isomorphic.
        oversize = size_hard_limit > 0 and len(section_text) > size_hard_limit
        if oversize and _is_splittable_prose(section_text):
            pieces = []
            hard_fallback = False
            for piece in _split_with_furniture(
                section_text, size_hard_limit, None
            ):
                if len(piece) > size_hard_limit:
                    # Giant single line: line boundaries cannot
                    # split it — last-resort forced cut.
                    pieces.extend(
                        _split_at_hard_fallback(piece, size_hard_limit)
                    )
                    hard_fallback = True
                else:
                    pieces.append(piece)
        else:
            pieces = [section_text]
            hard_fallback = False

        # Blank-piece guard: line-boundary / hard-fallback splitting
        # can leave whitespace-only pieces at the cut points
        # (interior consecutive blank lines landing in one piece) —
        # drop them; if everything ends up empty, emit the whole
        # section unchanged.
        pieces = [p for p in pieces if p.strip()]
        if not pieces:
            pieces = [section_text]
            hard_fallback = False

        for piece in pieces:
            unit_metadata = {
                "heading": heading,
                "heading_level": heading_level,
                "structure_preserved": True,
                compact_key: True,
            }
            if extra:
                # Container provenance (slide_index/chapter_index)
                # carried as-is.
                unit_metadata.update(extra)
            if len(pieces) > 1:
                # Size-degradation split provenance — isomorphic
                # to the paragraph contract.
                unit_metadata["size_split"] = True
                if hard_fallback:
                    unit_metadata["split_reason"] = "hard_limit_fallback"
            if oversize and not _is_splittable_prose(section_text):
                # Integrity blocks (fenced code/tables) are not
                # cut; record the over-limit fact honestly.
                unit_metadata["oversized"] = True
                unit_metadata["oversized_reason"] = (
                    "structural_block exceeds hard_limit"
                )
            if provenance is not None:
                unit_metadata["structure_provenance"] = provenance
            units.append(
                RepresentationUnit(
                    text=piece,
                    unit_type=UnitType.NARRATIVE,
                    sequence_index=unit_index,
                    heading=heading,
                    heading_path=list(heading_path),
                    structural_info=StructuralInfo(
                        heading_level=heading_level,
                        heading_path=list(heading_path),
                        parent_section=(
                            heading_path[-1] if heading_path else None
                        ),
                    ),
                    source_context=src_ctx,
                    metadata=unit_metadata,
                )
            )
            unit_index += 1
        return unit_index

    def _add_outline_or_fallback_unit(
        self, node, heading_path, units, unit_index,
        src_ctx, parser_candidates, extra,
    ):
        """Heading node of an empty section: a trusted tree emits an
        outline_only unit; a fallback tree emits the misjudged
        heading line as a body unit (same contract as the parent).
        Both carry container provenance."""
        if parser_candidates:
            units.append(
                RepresentationUnit(
                    text=node.title,
                    unit_type=UnitType.NARRATIVE,
                    sequence_index=unit_index,
                    heading=node.title,
                    heading_path=list(heading_path),
                    structural_info=StructuralInfo(
                        heading_level=node.level,
                        heading_path=list(heading_path),
                        parent_section=(
                            heading_path[-1] if heading_path else None
                        ),
                    ),
                    source_context=src_ctx,
                    metadata={
                        "heading": node.title,
                        "heading_level": node.level,
                        "outline_only": True,
                        "structure_preserved": True,
                        "structure_provenance": node.metadata.get(
                            "structure_provenance"
                        ),
                        **extra,
                    },
                )
            )
            return unit_index + 1

        # Fallback-tree misjudgement: content is not lost —
        # emitted as body units; the size hard limit still applies
        # (same fallback as the parent).
        line_limit = self.policy.packaging.size_hard_limit
        line_pieces = [node.title]
        if line_limit > 0 and len(node.title) > line_limit:
            line_pieces = _split_at_hard_fallback(node.title, line_limit)
        for piece in line_pieces:
            piece_metadata = {
                "heading": "",
                "heading_level": 0,
                "fallback_heading_line": True,
                "structure_preserved": False,
                **extra,
            }
            if len(line_pieces) > 1:
                piece_metadata["size_split"] = True
                piece_metadata["split_reason"] = "hard_limit_fallback"
            units.append(
                RepresentationUnit(
                    text=piece,
                    unit_type=UnitType.NARRATIVE,
                    sequence_index=unit_index,
                    heading="",
                    heading_path=[],
                    structural_info=StructuralInfo(
                        heading_level=0,
                        heading_path=[],
                        parent_section=None,
                    ),
                    source_context=src_ctx,
                    metadata=piece_metadata,
                )
            )
            unit_index += 1
        return unit_index

    def _add_gap_units(
        self, text, spans, units, unit_index, src_ctx, size_hard_limit,
    ):
        """Text regions not covered by spans → paragraph units
        without container identity.

        The normal path (parser spans tile the whole document)
        never triggers this; it is a content-preservation safety
        net, not regular behaviour.
        """
        intervals = sorted(
            (
                max(0, min(int(s.get("start", 0)), len(text))),
                max(0, min(int(s.get("end", len(text))), len(text))),
            )
            for s in spans
        )
        gaps = []
        cursor = 0
        for start, end in intervals:
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < len(text):
            gaps.append((cursor, len(text)))

        for gap_start, gap_end in gaps:
            gap_text = text[gap_start:gap_end].strip()
            if not gap_text:
                continue
            unit_index = self._add_paragraph_units(
                gap_text, units, unit_index,
                heading="", heading_level=0,
                heading_path=[],
                src_ctx=src_ctx,
                size_hard_limit=size_hard_limit,
                extra_metadata={"container_gap": True},
            )
        # Units from the defensive path are appended at the end;
        # keeping sequence_index ordered suffices (the normal path
        # has no gaps and body order stays intact).
        return unit_index
