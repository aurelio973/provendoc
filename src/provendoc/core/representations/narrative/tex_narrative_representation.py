# ============================================================
# TEX REPRESENTATION
# ============================================================
"""
LaTeX narrative representation.

The parser keeps .tex text verbatim (evidence-first), so command
residue, preamble, and math markup all live in the document facts —
right for evidence preservation, wrong for narrative chunks. This
layer therefore renders the source into prose on the first build()
hop (preamble stripping + comment stripping + environment dispatch +
math transcription), then runs a shim Document through the Txt
narrative machine (paragraph split / furniture filter / coalesce /
three granularities), mirroring the container-aware docx/epub layers.

Math atomicity: display math blocks are tagged chunking="display_math"
(fine passes them through whole; coalesce treats them as merge
barriers).

Macros get deterministic structural expansion only — plain-text
substitution with a macro-body whitelist, no control flow or I/O,
bounded by step/depth/length guards; zero execution, zero evaluation.
Undecidable macros stay verbatim with disclosed counts. The LLM /
evidence side (raw json / representation json / tex_structure facts /
tex_parser metadata) keeps the source as-is and never sees the
expansion.

The macro engine, environment catalog, math transcription, and
renderer live in sibling modules (tex_macro_expansion /
tex_env_catalog / tex_math_transcription / tex_renderer); this file
keeps the orchestrator and the representation class, re-exporting the
public API (TexNarrativeRepresentation / render_tex_narrative /
transcribe_math).
"""

import re

from provendoc.base.models.document import Document
from provendoc.core.representations.narrative.tex_macro_expansion import (  # noqa: F401
    _collect_macro_defs,
    _normalize_env_aliases,
    _normalize_meta_aliases,
)
from provendoc.core.representations.narrative.tex_math_transcription import (  # noqa: F401
    RenderedTex,
    _clean_math_latex,
    transcribe_math,  # noqa: F401
)
from provendoc.core.representations.narrative.tex_renderer import (  # noqa: F401
    _TexRenderer,
)
from provendoc.core.representations.narrative.txt_narrative_representation import (
    TxtNarrativeRepresentation,
)
from provendoc.core.structure.heading_patterns import HeadingCandidate

# ============================================================
# Public render entry point
# ============================================================


def _collapse_blank_runs(text, candidates, blocks):
    """Collapse 3+ consecutive newlines into one blank line, remapping candidate/block offsets (defensive path)."""
    import bisect

    out = []
    remap = []  # remap[k] = old offset of the k-th char in the new text
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\n":
            j = i
            while j < n and text[j] == "\n":
                j += 1
            for _ in range(min(j - i, 2)):
                out.append("\n")
                remap.append(i)
            i = j
        else:
            out.append(text[i])
            remap.append(i)
            i += 1
    new_text = "".join(out)

    def _mp(off, side):
        k = (
            bisect.bisect_left(remap, off)
            if side == "l"
            else bisect.bisect_right(remap, off)
        )
        return min(k, len(remap))

    for c in candidates:
        c.start = _mp(c.start, "l")
        c.end = _mp(c.end, "r")
    new_blocks = [
        (_mp(a, "l"), _mp(b, "r"), t) for a, b, t in blocks
    ]
    blocks.clear()
    blocks.extend(new_blocks)
    return new_text


def render_tex_narrative(source, math_mode=None):
    """Render .tex source into narrative text.

    Returns a RenderedTex (text / heading_candidates /
    display_math_blocks / evidence / fallback). On a render error,
    fallback=True and text is the original source (degraded to the
    status quo, zero content loss).

    math_mode: None uses _TEX_MATH_MODE_DEFAULT (currently "latex" —
    $/$$-wrapped LaTeX passthrough); "unicode" is the Unicode
    transcription mode.
    """
    renderer = _TexRenderer()
    if math_mode is not None:
        renderer.math_mode = math_mode
    try:
        # Normalize meta-alias macros (collection and rendering share
        # the same normalized source, so \nc{..}{..} definition lines
        # cannot leak verbatim into _scan)
        source, _ma_rounds = _normalize_meta_aliases(source)
        if _ma_rounds:
            renderer.ev["meta_aliases_normalized"] = _ma_rounds
        # Normalize environment open/close aliases (\bea becomes the
        # literal \begin{eqnarray}; the environment dispatcher takes over)
        source, _ea_rounds = _normalize_env_aliases(source)
        if _ea_rounds:
            renderer.ev["env_aliases_normalized"] = _ea_rounds
        # Collect macro definitions (whole file; deterministic whitelist check)
        macros, mstats = _collect_macro_defs(source)
        renderer.macros = macros
        renderer.ev["macros_defined"] = mstats["defined"]
        renderer.ev["macros_redefined"] = mstats["redefined"]
        renderer.ev["macros_unexpandable"] = mstats["unexpandable"]
        renderer.ev["macros_unexpandable_samples"] = (
            mstats["unexpandable_samples"]
        )
        body, title_meta = renderer._split_preamble(source)
        renderer._pending_title = title_meta
        renderer._scan(body, 0)
        # Safety net: if no \maketitle appears, emit the pending title block at the end
        if not renderer._title_emitted and renderer._pending_title:
            renderer._emit_title_block(renderer._pending_title)
        text = "".join(renderer.parts)
        # Leading newlines (the inline stream starts with \n) → shift all offsets
        lead = len(text) - len(text.lstrip("\n"))
        if lead:
            text = text[lead:]
            for c in renderer.candidates:
                c.start = max(0, c.start - lead)
                c.end = max(0, c.end - lead)
            renderer.display_math_blocks = [
                (max(0, a - lead), max(0, b - lead), t)
                for a, b, t in renderer.display_math_blocks
            ]
        text = text.rstrip("\n")
        # Defensive: break-aware emit should not leave 3+ consecutive
        # newlines (\par right at a block boundary is already handled by
        # _at_paragraph_break); if they still appear, collapse and remap
        # offsets
        if "\n\n\n" in text:
            text = _collapse_blank_runs(
                text, renderer.candidates, renderer.display_math_blocks
            )
        # \textbf{Abstract} is not a section command and yields no
        # declared candidate. Post-pass: if a standalone Abstract line
        # exists before the first declared heading, add a word_section
        # candidate (depth_hint=2, section-level, via the "abstract"
        # word list in heading_patterns). Documents with no candidates
        # scan only the first 2000 chars to avoid body false hits.
        _first_head = (
            min(c.start for c in renderer.candidates)
            if renderer.candidates
            else min(len(text), 2000)
        )
        if _first_head > 0 and not any(
            c.raw.strip() == "Abstract" for c in renderer.candidates
        ):
            _am = re.search(
                r"(?m)^[ \t]*Abstract[ \t]*$", text[:_first_head]
            )
            if _am:
                renderer.candidates.append(
                    HeadingCandidate(
                        raw="Abstract",
                        pattern="word_section",
                        depth_hint=2,
                        start=_am.start(),
                        end=_am.end(),
                        provenance="derived",
                    )
                )
                renderer.candidates.sort(key=lambda c: c.start)
                renderer.ev["abstract_heading_derived"] = True
        rendered = RenderedTex(
            text=text,
            heading_candidates=renderer.candidates,
            display_math_blocks=renderer.display_math_blocks,
            evidence=renderer.ev,
        )
        if not text.strip() and source.strip():
            # Whole-source render came back empty (extreme) → fall back
            # to the raw source to keep content
            rendered.text = source
            rendered.fallback = True
            rendered.evidence["fallback_to_source"] = True
        return rendered
    except Exception as e:  # a render bug must not eat content
        return RenderedTex(
            text=source,
            evidence={
                "extractor": "tex_render_v1",
                "render_error": str(e),
                "fallback_to_source": True,
            },
            fallback=True,
        )


# ============================================================
# Representation layer
# ============================================================


class TexNarrativeRepresentation(TxtNarrativeRepresentation):
    """LaTeX narrative: source → rendered prose → Txt machine.

    The default math mode is "latex": math passes through as
    $/$$-wrapped LaTeX (rendered downstream by MathJax/KaTeX); the
    Unicode transcription mode can be restored via
    render_tex_narrative(math_mode="unicode").

    The only difference from TxtNarrativeRepresentation is the first
    build() hop: the real Document is rendered into a shim Document
    (rendered text + fresh heading candidates + tex_render_v1
    evidence); everything after reuses the Txt paragraph/merge/
    granularity machinery. Display math blocks gain atomicity via
    the base unit tag metadata["chunking"]="display_math" (passed
    through whole at fine; a barrier under coalesce).
    """

    _SHIM_FLAG = "_tex_rendered_shim"

    def build(self, document):
        """First hop: render the real Document into a tex_render_v1 shim, then reuse Txt build()."""
        md = document.metadata or {}
        if md.get(self._SHIM_FLAG):
            return super().build(document)
        rendered = render_tex_narrative(document.text)
        shim_text = rendered.text if rendered.text.strip() else document.text
        shim = Document(
            text=shim_text,
            metadata={
                **md,
                "heading_candidates": rendered.heading_candidates,
                self._SHIM_FLAG: True,
                "tex_render": rendered.evidence,
                "_tex_display_math_blocks": rendered.display_math_blocks,
            },
            original_text=document.original_text,
            source_text=document.source_text,
        )
        result = super().build(shim)
        if isinstance(result.metadata, dict):
            result.metadata["tex_render_v1"] = rendered.evidence
        return result

    def _build_base_units(self, document):
        units = super()._build_base_units(document)
        md = document.metadata or {}
        blocks = md.get("_tex_display_math_blocks") or []
        if blocks and units:
            self._mark_display_math(units, blocks)
        return units

    @staticmethod
    def _mark_display_math(units, blocks):
        """Tag display math blocks as unsplittable atoms, in order.

        Sequential cursor matching (units and blocks both in document
        order); equal text is a hit. An over-limit slice gets only its
        first piece tagged (same fallback as the oversized family).
        """
        texts = [b[2].strip() for b in blocks]
        bi = 0
        for unit in units:
            if bi >= len(texts):
                break
            t = (unit.text or "").strip()
            if not t:
                continue
            if t == texts[bi]:
                md = unit.metadata if unit.metadata is not None else {}
                md["chunking"] = "display_math"
                unit.metadata = md
                bi += 1
