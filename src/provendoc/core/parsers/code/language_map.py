# ============================================================
# Code Domain
# Suffix → language mapping (single source of truth for code)
# ============================================================
"""Suffix → tree-sitter language name mapping.

Division of labor with domain_registry.py:
    domain_registry decides "which ingestion domain this suffix belongs to";
    this module decides "which language grammar parses it within the code domain".

.tree-sitter injected languages (embedded grammar):
    the inside of Vue SFC <script lang="ts"> is automatically delegated to the
    typescript grammar by the vue grammar's injection mechanism — no handling
    needed here; vue_backend only splits top-level SFC blocks.
"""

from typing import Dict, Optional

# suffix (lowercase, with dot) → tree-sitter language name
SUFFIX_TO_LANGUAGE: Dict[str, str] = {
    # python
    ".py": "python",
    ".pyi": "python",
    # javascript / typescript (React: .jsx / .tsx)
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    # Frontend markup / styles
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    # Component frameworks
    ".vue": "vue",
    # Backend / systems languages
    ".java": "java",
    ".go": "go",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    # Other common languages (supported when a grammar is available)
    ".rs": "rust",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    # SQL schema / query definitions
    ".sql": "sql",
    # Scripting / functional / systems languages (supplemental set from tree-sitter-language-pack)
    ".lua": "lua",
    ".dart": "dart",
    ".r": "r",
    ".pl": "perl",
    ".pm": "perl",
    ".zig": "zig",
    # Style supplements
    ".less": "less",
    # Infrastructure-as-code (HCL syntax family): .hcl general syntax stays in the code domain;
    # .tf/.tfvars moved to the config domain (config_hcl_core_p0_prereg_v1 D1,
    # pinned by the reconciliation lock test_domain_invariants)
    ".hcl": "hcl",
    # Windows scripts / build systems / interface definitions
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    ".dockerfile": "dockerfile",  # incl. bare filenames Dockerfile (entry normalization)
    ".makefile": "make",  # incl. bare filenames Makefile/GNUmakefile (entry normalization)
    ".mk": "make",
    ".mak": "make",
    ".proto": "proto",
    ".cmake": "cmake",  # CMakeLists.txt has a .txt suffix — different mechanism, not covered for now
}

# Human-readable language names (for report display)
LANGUAGE_DISPLAY: Dict[str, str] = {
    "csharp": "C#",
    "cpp": "C++",
    "scss": "SCSS",
    "hcl": "HCL",
}

# ------------------------------------------
# markdown fence info string → tree-sitter language
# ------------------------------------------

# Fence-tag first token (lowercase) → tree-sitter language name.
# Fence tags differ from file suffixes (author claim vs filesystem fact), hence a separate table;
# only high-frequency aliases are listed — unregistered tags are returned as-is for the caller
# to probe via the backend; probe failure degrades honestly, no guessing, no fabrication.
FENCE_TAG_ALIASES: Dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "yml": "yaml",
    "rb": "ruby",
    "golang": "go",
    "c++": "cpp",
    "cs": "csharp",
    "rs": "rust",
    "htm": "html",
}

# Presentational (non-code) fence tags: explicitly skipped, never sent to backend probing —
# aligned with embedded.py's under-report principle (better to miss unknown languages than create noisy diagnostics).
FENCE_SKIP_TAGS = {
    "", "text", "txt", "plain", "plaintext",
    "console", "output", "result", "terminal",
    "mermaid", "math",
}


def language_for_fence_tag(info: str) -> Optional[str]:
    """markdown fence info string → candidate tree-sitter language name.

    Take the first whitespace-delimited token of the info string (CommonMark: the
    first word is the language tag, the rest are meta parameters), lowercase it, look up the alias table.

    Returns:
        str   — candidate language name (may still lack grammar support; caller probes)
        None  — explicit skip (no tag / presentational tag); parsing should not be attempted
    """
    tokens = (info or "").strip().lower().split(None, 1)
    tag = tokens[0] if tokens else ""
    if tag in FENCE_SKIP_TAGS:
        return None
    return FENCE_TAG_ALIASES.get(tag, tag)


# Code suffixes registered into UniversalParser from this table by default
CODE_SUFFIXES = sorted(SUFFIX_TO_LANGUAGE)


def language_for_suffix(suffix: str) -> Optional[str]:
    """Suffix → tree-sitter language name; unknown suffixes return None."""
    return SUFFIX_TO_LANGUAGE.get(suffix.lower())


def display_name(language: str) -> str:
    """Language name → display name; unknown languages are returned as-is."""
    return LANGUAGE_DISPLAY.get(language, language)
