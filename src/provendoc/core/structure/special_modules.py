import re

# ============================================================
# core/structure/special_modules.py
# ============================================================


CODE_FENCE_RE = re.compile(r"^(```|~~~)")

TABLE_RE = re.compile(r"^\|")

# ASCII table border line ("+----+----+" / "+====+"): pure +-= chars
# with leading/trailing + — border skeleton of graphviz / preformatted tables.
ASCII_TABLE_RE = re.compile(r"^\+[-=+]{4,}\+$")

# TSV data row: >= 2 tabs in the line (a single tab is usually indent; skip).
TSV_TABLE_RE = re.compile(r"^\S.*(\t.*\t)")

LEGAL_RE = re.compile(r"^第[一二三四五六七八九十百千万\d]+条")

INSTRUCTION_RE = re.compile(r"^【.+?】")

# Inline instruction form: bracket followed by body text ("【注意】本品...").
# A line that is exactly 【XX】 goes to heading detection (bracket_cn in
# heading_patterns), not a duplicate instruction module.
INSTRUCTION_INLINE_RE = re.compile(r"^【.+?】\s*\S")

# QA form: "Q:/A:/问：/答：" plus numbered FAQ forms "Q1:/A12.".
# Tightened: single letter + dot ("A. First") is an alpha heading form —
# numbered forms require >= 1 digit; unnumbered forms only accept a colon.
QA_RE = re.compile(r"^(?:(?:Q|A)\d+\s*[.:：]|[QA]\s*[：:]|问：|答：)")

DIALOG_RE = re.compile(r"^(system:|user:|assistant:)")

SEPARATOR_RE = re.compile(r"^(=+|-+|\*+)$")


def detect_special_modules(text):
    """Detect special modules in body text (code blocks, tables, lists, QA,
    dialogs, separators).

    Args:
        text: full document text.

    Returns:
        list: type and char-range info for each module, in occurrence order.
    """
    modules = []
    lines = text.splitlines()
    offset = 0
    in_code = False
    code_start = 0

    for line in lines:
        stripped = line.strip()

        # code block
        if CODE_FENCE_RE.match(stripped):
            if not in_code:
                in_code = True
                code_start = offset
            else:
                modules.append({
                    "type": "code",
                    "start": code_start,
                    "end": offset + len(line),
                })
                in_code = False

        # No other module detection inside fences — | / tabs / Q: inside a
        # code body are code content, not document structure (same
        # fence-awareness rule as heading_patterns).
        elif in_code:
            pass

        # table
        elif TABLE_RE.match(stripped) or ASCII_TABLE_RE.match(stripped):
            modules.append({
                "type": "table",
                "start": offset,
                "end": offset + len(line),
            })

        # tsv data row (≥2 tabs)
        elif TSV_TABLE_RE.match(line):
            modules.append({
                "type": "table",
                "start": offset,
                "end": offset + len(line),
            })

        # legal clause
        elif LEGAL_RE.match(stripped):
            modules.append({
                "type": "legal_clause",
                "start": offset,
                "end": offset + len(line),
            })

        # instruction block (inline marker: bracket followed by body text;
        # a whole-line 【XX】 is a bracket_cn heading, not registered here)
        elif INSTRUCTION_INLINE_RE.match(stripped):
            modules.append({
                "type": "instruction",
                "start": offset,
                "end": offset + len(line),
            })

        # QA
        elif QA_RE.match(stripped):
            modules.append({
                "type": "qa",
                "start": offset,
                "end": offset + len(line),
            })

        # dialog
        elif DIALOG_RE.match(stripped):
            modules.append({
                "type": "dialog",
                "start": offset,
                "end": offset + len(line),
            })

        # separators
        elif SEPARATOR_RE.match(stripped):
            modules.append({
                "type": "separator",
                "start": offset,
                "end": offset + len(line),
            })

        offset += len(line) + 1

    return modules
