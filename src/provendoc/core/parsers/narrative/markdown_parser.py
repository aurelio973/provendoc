# ============================================================
# Narrative Domain
# Markdown Semantic Structure Parser
# ============================================================
import time

from provendoc.base.models.document import Document
from provendoc.base.parser import (
    DEFAULT_TEXT_MAX_SOURCE_BYTES,
    BaseParser,
)
from provendoc.core.parsers.code.contracts import ParseStatus
from provendoc.core.parsers.code.language_map import language_for_fence_tag
from provendoc.core.parsers.narrative.markdown_node_tree import (
    derive_markdown_nodes,
)
from provendoc.core.parsers.narrative.markdown_structure_facts import (
    collect_markdown_structure_facts,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.structure.heading_patterns import PATTERNS, extract_heading_candidates
from provendoc.core.structure.special_modules import detect_special_modules
from provendoc.core.text.markdown_blocks import content_digest, extract_fenced_code_blocks

# Main markdown regex (shared with candidate extraction): derives the
# compatible metadata["headings"] — {level, title} dict, title without the # prefix.
_MARKDOWN_PATTERN = dict(PATTERNS)["markdown"]

# Cap on the symbol-name list in embedded-block evidence (full fact is symbol_count)
_EMBEDDED_SYMBOL_NAME_LIMIT = 20

# Backend statuses acceptable for embedded-code-block parsing (upgrade gate);
# degraded (grammar unavailable/load failed) and empty never upgrade.
_EMBEDDED_UPGRADE_STATUSES = {ParseStatus.OK.value, ParseStatus.RECOVERED.value}


class MarkdownParser(BaseParser):
    """Markdown parser — structure detection adopts the shared modules (single source of truth).

    Same shape as TxtParser: heading_candidates (with positions) +
    special_modules all come from the core.structure shared extractors,
    automatically inheriting fence awareness, no-space heading recovery,
    numbered-list disambiguation, etc.; no local regex copies.

    Second-pass parsing of embedded languages (mirroring the Vue SFC
    <script>/<style> precedent): fenced code blocks with a detected
    language tag go to the code-domain tree-sitter backend; facts
    (language/status/symbols/diagnostics/spans) land in
    metadata["embedded_code_blocks"]; the representation layer upgrades
    parseable blocks to code units with chunking=structural_ast (fence
    rendering on the export side reuses them automatically). Blocks with
    unknown language or unavailable grammar honestly degrade to plain
    narrative text — no guessing, no fabrication.

    Compatible field: metadata["headings"] derived from candidates,
    structure {level, title} unchanged (consumers: narrative_quality /
    narrative_invariants / run_experiment).
    """

    # Source-size guard (text_source_limits_v1): default 32 MiB, see base.parser.
    max_source_bytes = DEFAULT_TEXT_MAX_SOURCE_BYTES

    def __init__(
        self,
        encoding="utf-8",
        tolerant=True,
    ):
        """Initialize with encoding and tolerant switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    @staticmethod
    def _derive_headings(candidates):
        """Derive the compatible headings field from shared candidates.

        Only markdown-pattern candidates enter (numeric/CJK-numbered
        candidates are a heading system under txt semantics, but under md
        semantics the hash mark is the authoritative marker); title strips
        the # prefix, level is the hash count, in document order
        (candidates are already sorted by start).
        """
        headings = []
        for c in candidates:
            if c.pattern != "markdown":
                continue
            match = _MARKDOWN_PATTERN.match(c.raw)
            if not match:
                # No-space candidates produced by the recovery scan ("###2.5chunker"): strip the prefix
                headings.append({
                    "level": c.depth_hint,
                    "title": c.raw.lstrip("#").strip(),
                })
                continue
            headings.append({
                "level": c.depth_hint,
                "title": match.group(2).strip(),
            })
        return headings

    def parse(self, file_path):
        """Parse a Markdown file into a Document (shared structure detection + second-pass embedded code parsing)."""
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "markdown", self.encoding, self.tolerant
            )
            text = ctx.text
            metadata = ctx.metadata

            # Shared structure detection (same source as TxtParser, inherits all fixes)
            candidates = extract_heading_candidates(text)
            metadata["heading_candidates"] = candidates
            metadata["special_modules"] = detect_special_modules(text)
            # Domain document signals: same fact discipline as txt (count, don't interpret).
            metadata["domain_signals"] = detect_domain_signals(text)

            # Compatible field: {level, title} dict (zero consumer changes)
            metadata["headings"] = self._derive_headings(candidates)
            metadata["heading_count"] = len(metadata["headings"])

            # Second-pass embedded-language parsing: fenced blocks → code-domain backend,
            # facts recorded in metadata (representation layer upgrades code units accordingly)
            self._parse_embedded_code_blocks(text, metadata)

            # Block-structure evidence registration (markdown_block_structure_prereg_v1, decided):
            # evidence-only — front matter/tables/task lists/link-images/footnotes/
            # inline code/meta directives/template expressions census + tri-state
            # derivation, all additive; text untouched, existing keys untouched
            collect_markdown_structure_facts(text, metadata)

            # Unified node set P0 (markdown_unified_nodes_p0_prereg_v1, decided):
            # line-level splitter → flat node tree metadata["markdown_nodes"].
            # Additive surface, zero chunk-layer consumption (D-U5); node-tree
            # failure never blocks ingestion.
            try:
                derive_markdown_nodes(text, metadata)
            except Exception as exc:  # pragma: no cover - last-resort silent degradation
                metadata["markdown_nodes_error"] = f"derive_markdown_nodes: {exc}"

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            metadata["parse_duration_ms"] = round(elapsed_ms, 3)

            return Document(
                text=text,
                metadata=metadata,
                source_text=ctx.source_text,
            )

        except Exception as e:
            metadata = {"parse_failed": True, "parse_error": str(e)}
            return Document(text="", metadata=metadata)

    # ----------------------------------------
    # embedded code blocks (second-pass embedded-language parsing)
    # ----------------------------------------

    @staticmethod
    def _content_digest(content: str) -> str:
        """Compatible entry: digest algorithm adopted into the shared module (markdown_blocks.content_digest)."""
        return content_digest(content)

    def _parse_embedded_code_blocks(self, text: str, metadata: dict) -> None:
        """Extract fenced code blocks and run tree-sitter second-pass parsing on parseable languages.

        Facts go into metadata (never raises; whole-pipeline failure just records embedded_code_error):
            embedded_code_blocks — per-block evidence (language/status/symbols/diagnostics/
                spans/content_sha1); content_sha1 lets the representation layer verify alignment
                with discourse code blocks without carrying full text in metadata
            embedded_code_total / _parsed / _skipped — aggregate counts
            embedded_{language}_blocks — per-language counts (Vue-precedent naming)
        """
        try:
            blocks = extract_fenced_code_blocks(text)
            if not blocks:
                return

            # Same stateless tolerant backend as embedded.py, lazy-loaded
            # (missing packs take the degraded path, recording skip_reason instead of failing)
            backend = None
            evidence = []
            per_language: dict[str, int] = {}

            for block in blocks:
                entry = {
                    "index": block["index"],
                    "fence_info": block["fence_info"],
                    "closed": block["closed"],
                    "line_start": block["line_start"],
                    "line_end": block["line_end"],
                    "byte_start": block["byte_start"],
                    "byte_end": block["byte_end"],
                    "content_sha1": self._content_digest(block["content"]),
                    "language": None,
                    "status": "skipped",
                    "skip_reason": None,
                    "symbol_count": 0,
                    "symbols": [],
                    "diagnostic_count": 0,
                }

                content = block["content"]
                language = language_for_fence_tag(block["fence_info"])
                if language is None:
                    entry["skip_reason"] = (
                        "no language tag" if not block["fence_info"]
                        else "presentation fence tag"
                    )
                elif not content.strip():
                    entry["skip_reason"] = "empty code block"
                else:
                    if backend is None:
                        from provendoc.core.parsers.code.generic_backend import (
                            TreeSitterCodeBackend,
                        )
                        backend = TreeSitterCodeBackend()
                    try:
                        structure = backend.parse(content, language).structure
                    except Exception as exc:  # defensive: unexpected backend errors never block ingestion
                        entry["skip_reason"] = f"embedded parse failed: {exc}"
                    else:
                        entry["language"] = language
                        entry["status"] = structure.status
                        if structure.status in _EMBEDDED_UPGRADE_STATUSES:
                            entry["symbol_count"] = len(structure.symbols)
                            entry["symbols"] = [
                                s.name for s in structure.symbols if s.name
                            ][:_EMBEDDED_SYMBOL_NAME_LIMIT]
                            entry["diagnostic_count"] = len(structure.diagnostics)
                            per_language[language] = per_language.get(language, 0) + 1
                        else:
                            entry["skip_reason"] = (
                                structure.diagnostics[0].message
                                if structure.diagnostics
                                else f"backend status: {structure.status}"
                            )

                evidence.append(entry)

            metadata["embedded_code_blocks"] = evidence
            metadata["embedded_code_total"] = len(evidence)
            metadata["embedded_code_parsed"] = sum(
                1 for e in evidence if e["status"] in _EMBEDDED_UPGRADE_STATUSES
            )
            metadata["embedded_code_skipped"] = sum(
                1 for e in evidence
                if e["status"] not in _EMBEDDED_UPGRADE_STATUSES
            )
            for language, count in per_language.items():
                metadata[f"embedded_{language}_blocks"] = count

        except Exception as e:  # whole embedded-parse failure never blocks ingestion
            metadata["embedded_code_error"] = str(e)
