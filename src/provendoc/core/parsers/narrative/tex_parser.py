# ============================================================
# Narrative Domain
# LaTeX (.tex) Parser
# ============================================================
import re
import time
from pathlib import Path

from provendoc.base.models.document import Document
from provendoc.base.parser import (
    DEFAULT_TEXT_MAX_SOURCE_BYTES,
    BaseParser,
)
from provendoc.core.foundation.metadata import _build_metadata
from provendoc.core.parsers.narrative.tex_structure import (
    _BEGIN_ENV_RE,
    _LABEL_RE,
    _MATH_ENVIRONMENTS,
    _RAW_TEX_LIMIT,
    collect_tex_structure_facts,
)
from provendoc.core.structure.domain_signals import detect_domain_signals
from provendoc.core.structure.equation_numbers import match_equation_number
from provendoc.core.structure.heading_patterns import HeadingCandidate
from provendoc.core.structure.special_modules import detect_special_modules

# Sectioning commands → heading levels (part/chapter appear only in book/report-like
# documents; mapped by declared level when mixed with section, no document-class guessing)
_SECTION_COMMANDS = {
    "part": 1,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}

# \cmd[short]{long} — position-captured title argument; starred variants (\section*) are synonymous
_SECTION_RE = re.compile(
    r"\\(?:" + "|".join(_SECTION_COMMANDS) + r")\*?\s*"
    r"(?:\[[^\]]*\])?\s*\{"
)

# Math environments and begin regexes imported from the tex_structure single source of truth
# (semantics frozen, identical to v0 — no parallel implementations)
# display delimiters \[ ... \] (literal backslash + brackets), with no other backslash allowed before
_BRACKET_DISPLAY_RE = re.compile(r"(?<!\\)\\\[")
# display blocks counted in pairs (open/close $$ is one pair; token counting would double)
_DOLLAR_DISPLAY_RE = re.compile(r"\$\$.+?\$\$", re.DOTALL)
# inline $...$: no unescaped $, no line crossing (conservative — prefer fewer to more)
_INLINE_DOLLAR_RE = re.compile(r"(?<![\\$])\$([^$\n]+?)\$(?!\d)")
_TAG_RE = re.compile(r"\\tag\{([^}]*)\}")
_INCLUDE_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")

_MAX_SAMPLES = 5


class TexParser(BaseParser):
    """LaTeX parser: source-evidence mode.

    Fact boundaries:
        - text keeps the LaTeX source verbatim (evidence-first: commands/math/
          environment markers are document facts, never stripped at the parser layer)
        - heading candidates come only from real sectioning commands (\\section
          etc., incl. starred and [short] variants), never guessed from
          typographic commands like \\textbf
        - \\title in the preamble is not a section (it is a metadata title)
    """

    # Source-size guard (text_source_limits_v1): default 32 MiB, see base.parser.
    max_source_bytes = DEFAULT_TEXT_MAX_SOURCE_BYTES

    def __init__(self, encoding="utf-8", tolerant=True):
        """Initialize with encoding and tolerant switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    def parse(self, file_path):
        """Parse .tex into a Document (source kept verbatim + heading candidates from sectioning commands)."""
        metadata = _build_metadata(file_path)
        metadata["file_type"] = "tex"
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "tex", self.encoding, self.tolerant
            )
            text = ctx.text
            metadata = ctx.metadata

            metadata["heading_candidates"] = self._section_candidates(text)
            metadata["special_modules"] = detect_special_modules(text)
            metadata["domain_signals"] = detect_domain_signals(text)
            metadata["section_count"] = len(metadata["heading_candidates"])

            # Math evidence (TEX is this library's only math-dense narrative format):
            # environments and delimiters only counted/sampled; equation numbering reuses
            # the DOCX/PDF shared trailing-bracket matcher — the source (commands/math
            # included) is not altered by one character.
            metadata["tex_formulas"] = self._formula_evidence(text)
            # Multi-file references are recorded, never expanded (source-evidence
            # contract: text is this file's source; expansion would fabricate a
            # combined document that does not exist).
            # Static-structure census + tri-state derivation (tex_static_structure_prereg_v1);
            # include local reconciliation is assembled after the structure facts
            # (v2 superset: exists/circular).
            structure = collect_tex_structure_facts(text, metadata)
            metadata["tex_includes"] = self._include_evidence(
                text, file_path, structure,
            )

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(
                text=text,
                metadata=metadata,
                source_text=ctx.source_text,
            )
        except Exception as e:
            metadata["parse_failed"] = True
            metadata["parse_error"] = str(e)
            # Tri-state alignment family precedent: failed is always written; no fabricated reasons
            metadata["tex_parse_status"] = {
                "status": "failed", "reasons": ["PARSE_FAILED"],
            }
            return Document(text="", metadata=metadata)

    # ========================================================
    # math / include evidence (counts and bounded samples)
    # ========================================================

    @staticmethod
    def _formula_evidence(text):
        """Math facts: environment and delimiter counts + numbered samples (source untouched)."""
        env_counts = {}
        display_env_total = 0
        for m in _BEGIN_ENV_RE.finditer(text):
            env = m.group(1)
            if env.rstrip("*") not in _MATH_ENVIRONMENTS:
                continue
            env_counts[env] = env_counts.get(env, 0) + 1
            display_env_total += 1

        tag_samples = []
        numbered_by_tail = 0
        for m in _TAG_RE.finditer(text):
            if len(tag_samples) < _MAX_SAMPLES:
                tag_samples.append(m.group(1).strip())
        # Explicit numbering outside \tag: trailing (N) brackets at the end of a
        # display environment body, reusing the DOCX/PDF shared equation_numbers matcher.
        for m in _BEGIN_ENV_RE.finditer(text):
            env = m.group(1)
            if env.rstrip("*") not in _MATH_ENVIRONMENTS:
                continue
            body = TexParser._environment_body(text, m.start())
            if body is None:
                continue
            if match_equation_number(body) is not None:
                numbered_by_tail += 1

        inline_math = sum(1 for _ in _INLINE_DOLLAR_RE.finditer(text))

        # v2 superset: display environment body raw_tex samples (≤5, linked to the
        # environment name and label; unclosed bodies fabricate no sample) — the
        # source is not altered by one character; samples are registration only.
        display_samples = []
        for m in _BEGIN_ENV_RE.finditer(text):
            env = m.group(1)
            if env.rstrip("*") not in _MATH_ENVIRONMENTS:
                continue
            if len(display_samples) >= _MAX_SAMPLES:
                break
            body = TexParser._environment_body(text, m.start())
            if body is None:
                continue
            label_match = _LABEL_RE.search(body)
            display_samples.append({
                "environment": env,
                "label": label_match.group(1).strip() if label_match else None,
                "raw_tex": body.strip()[:_RAW_TEX_LIMIT],
            })

        return {
            "extractor": "tex_formulas_v2",
            "display_environment_counts": env_counts,
            "display_environment_total": display_env_total,
            "bracket_display_count": len(
                _BRACKET_DISPLAY_RE.findall(text)
            ),
            "dollar_display_count": len(
                _DOLLAR_DISPLAY_RE.findall(text)
            ),
            "inline_math_count": inline_math,
            "tag_count": sum(1 for _ in _TAG_RE.finditer(text)),
            "tag_samples": tag_samples,
            "numbered_by_trailing_parentheses": numbered_by_tail,
            "display_samples": display_samples,
            "note": (
                "counts and bounded samples only; the LaTeX source "
                "stays in the narrative text verbatim (evidence-first, "
                "no stripping of commands or math)"
            ),
        }

    @staticmethod
    def _environment_body(text, begin_start):
        """Read the source between the begin and its paired \\end{same env}; None when unclosed."""
        end_marker = None
        m = _BEGIN_ENV_RE.match(text, begin_start)
        if m is None:
            return None
        env_name = m.group(1)
        end_marker = "\\end{" + env_name + "}"
        end_index = text.find(end_marker, m.end())
        if end_index < 0:
            return None
        return text[m.end():end_index]

    @staticmethod
    def _include_evidence(text, file_path=None, structure=None):
        """Multi-file reference facts: targets recorded, never expanded (source-evidence contract).

        v2 superset (tex_static_structure_prereg_v1): local exists reconciliation
        of targets (with ``.tex`` suffix completion) plus direct self-reference
        cycle detection — register only, never expand; MISSING_INPUT_FILE /
        CIRCULAR_INPUT are recorded facts, not parse degradation (include is
        by contract never expanded, so they never enter tex_parse_status reasons).
        """
        targets = []
        for m in _INCLUDE_RE.finditer(text):
            if len(targets) < _MAX_SAMPLES * 4:
                targets.append(m.group(1).strip())
        evidence = {
            "extractor": "tex_includes_v2",
            "include_count": len(targets),
            "targets": targets[:_MAX_SAMPLES * 4],
            "note": (
                "referenced files are recorded but never expanded: the "
                "narrative text is this file's source as authored"
            ),
        }
        if isinstance(structure, dict) and structure:
            # includegraphics target-count cross-link (prereg §2 P1 item 5)
            evidence["includegraphics_count"] = structure.get(
                "includegraphics_count", 0,
            )
        if file_path is not None:
            base_dir = Path(file_path).resolve().parent
            self_path = Path(file_path).resolve()
            exists_check = {}
            missing_targets = []
            circular = False
            for target in targets:
                resolved = None
                for candidate in (target, target + ".tex"):
                    probe = (base_dir / candidate).resolve()
                    if probe.is_file():
                        resolved = probe
                        break
                exists_check[target] = resolved is not None
                if resolved is not None and resolved == self_path:
                    circular = True  # CIRCULAR_INPUT: direct self-reference cycle
                elif resolved is None and len(missing_targets) < _MAX_SAMPLES:
                    missing_targets.append(target)  # MISSING_INPUT_FILE
            evidence["exists_check"] = exists_check
            evidence["missing_targets"] = missing_targets
            evidence["circular_self_reference"] = circular
        return evidence

    # ========================================================
    # section candidates (trusted positional facts)
    # ========================================================

    @staticmethod
    def _section_candidates(text):
        """Extract HeadingCandidates from sectioning commands (with absolute offsets)."""
        candidates = []
        for m in _SECTION_RE.finditer(text):
            level = None
            for cmd, lvl in _SECTION_COMMANDS.items():
                if m.group(0).startswith("\\" + cmd):
                    level = lvl
                    break
            if level is None:
                continue
            # Title = the following {...} argument
            brace_start = m.end() - 1
            title, brace_end = TexParser._read_braced(text, brace_start)
            if title is None:
                continue
            clean = TexParser._clean_markup(title)
            if not clean:
                continue
            candidates.append(
                HeadingCandidate(
                    raw=clean,
                    pattern="markdown",
                    depth_hint=level,
                    start=m.start(),
                    end=brace_end,
                )
            )
        return candidates

    @staticmethod
    def _read_braced(text, start):
        """Read the {...} content at start (nesting-aware); returns (content, end)."""
        if start >= len(text) or text[start] != "{":
            return None, start
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "\\" and i + 1 < len(text):
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1:i], i + 1
        return text[start + 1:], len(text)

    @staticmethod
    def _clean_markup(title):
        """LaTeX inline markup in a title → plain text (conservative rendering)."""
        title = re.sub(r"\\(?:textbf|textit|texttt|emph|text|textrm)\{([^{}]*)\}", r"\1", title)
        title = re.sub(r"\\\\[a-zA-Z]+", " ", title)
        title = re.sub(r"[{}$]", "", title)
        title = re.sub(r"\s+", " ", title)
        return title.strip()
