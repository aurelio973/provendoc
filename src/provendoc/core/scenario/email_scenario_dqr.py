r"""Email scenario → DQR payload projection (v1).

Prereg: ``specs/email_scenario_prereg_v1_draft.md``. The email
scenario chain v1 is a LEDGER, not a repair — like the
academic-paper chain, an email's extraction pain is structure
location, so v1 records line-level coordinates for the structural
conventions of RFC 822 mail and stamps them on
``document.metadata["email_scenario"]``:

* E1 header-field facts — the 13 known fields (From/To/Cc/Bcc/
  Subject/Date/Message-ID/Reply-To/In-Reply-To/References/
  Content-Type/Content-Disposition/MIME-Version), first occurrence
  per field: (line, canonical name, value truncated, folded-
  continuation count), plus the total header-line count;
* E2 header/body boundary — the first blank line (None when the
  body is never separated: an honest absence, never a guess);
* E3 quote blocks — runs of ``>``-prefixed lines (leading
  whitespace allowed, depth = ``>`` count in the prefix) with
  adjacent attribution hats (``On ... wrote:`` / bare
  ``Name wrote:`` / ``在 ... 写道：`` / ``From: ... Sent:``
  forward headers) merged into the same block;
* E4 signature block — the RFC 3676 ``-- `` separator line (the
  ONLY token on the line, one trailing space, CRLF tolerated);
  a prose ``--`` mid-sentence never matches;
* E5 attachments — ``Content-Disposition: ... attachment`` lines
  with their ``filename=``/``name=`` parameter values (folded
  continuations searched), plus MIME boundary marker lines.

Chunk integration: the header block, every quote block and the
signature block are protected regions handed to
``email_chunking.chunk_email_text`` (the invoice/academic greedy
skeleton) so they are never split across chunk boundaries; the
shared ``check_chunk_invariants`` verdict (coverage + byte
integrity) is reported as ``chunk_invariants``.

E0 — independence from the identification layer: the email
markers in ``domain_signals`` (header_from/to, header_subject/
date, message_id, quoted_reply) are CALIBRATION-FROZEN assets
whose shapes are vote-optimized; this ledger implements its own
locator regexes with full ledger semantics (the academic-line
precedent — the two layers own their own correctness, and the
marker family stays uncalibrated-locked regardless of this
chain). A real-corpus consequence baked into E1: mbox-style files
carry a ``From sender date`` separator line (no colon) on line 1
— it is recorded as ``mbox_from_line``, never mistaken for the
From: header field.

Contract discipline (mirrors invoice/academic chains):

* verbatim projection — every located line number and bounded
  text is present; nothing summarized away;
* line numbers refer to ``original_text`` lines (1-based) — no
  salvage, no line_map, no second coordinate space;
* DQR surfaces the payload VERBATIM and never folds it into the
  four dimension scores;
* attach-only — document text fields are never touched.

Honest boundaries:

* no MIME content decoding (base64/quoted-printable bodies are a
  representation-layer job; the ledger records source-text facts
  only);
* no thread-tree reconstruction (In-Reply-To/References are
  facts, not edges);
* no spam/phishing classification, no sender authenticity (same
  boundary as the invoice chain);
* attribution shapes cover the known forms (western On...wrote:,
  Chinese 在…写道：, From:...Sent: forward headers) — an
  uncovered shape is an honest miss, never a misrecord;
* quote blocks, attachments, attribution lines and boundary
  markers are capped in the payload with capped flags — the
  chunker is fed the UNCAPPED region list so protection never
  depends on the sample cap;
* the resolver's label decision is upstream (frameworks registry
  dispatch); this module is the pure projection primitive.

Dispatch (frameworks registry): the chain is registered under
``email_scenario`` in ``core/scenario/frameworks.py`` and invoked
by ``universal_parser`` after the L2 resolver labels the text
``email``.
"""

from __future__ import annotations

import re
from typing import Any

from provendoc.core.scenario.email_chunking import chunk_email_text
from provendoc.core.scenario.invoice_chunking import check_chunk_invariants

SCENARIO_METADATA_KEY = "email_scenario"

_MAX_QUOTE_BLOCKS = 200
_MAX_ATTACHMENTS = 100
_MAX_BOUNDARY_LINES = 200
_MAX_ATTRIBUTION_LINES = 200
_MAX_TEXT_CHARS = 80
_MAX_FOLD_SCAN = 4  # continuation lines searched for filename params

# E1 — header-field locator. A field line is ``Name: value`` with
# the colon immediately after a printable-name token; lines
# starting with whitespace are folded continuations of the last
# field. Only the 13 known names enter the ledger (a body line
# like ``Note: something`` in a degenerate no-boundary file is
# not a mail fact). Case-insensitive, canonicalized to the known
# spelling; first occurrence wins.
_HEADER_FIELD_NAMES = (
    "From", "To", "Cc", "Bcc", "Subject", "Date", "Message-ID",
    "Reply-To", "In-Reply-To", "References", "Content-Type",
    "Content-Disposition", "MIME-Version",
)
_HEADER_FIELD_RE = re.compile(r"^(?P<name>[A-Za-z0-9-]+):(?=[ \t]|$)")
# mbox separator: ``From sender date`` — space after "From", then
# a token WITHOUT a colon (a real header writes ``From:``). Only
# line 1 is checked (the separator's only legal position).
_MBOX_FROM_RE = re.compile(r"^From[ \t]+[^:\s]+")

# E3 — quote-line locator: leading run of [ \t>] containing at
# least one ``>``; depth = ``>`` count in the prefix.
_QUOTE_PREFIX_RE = re.compile(r"^[ \t>]*")

# attribution hats (recorded as facts wherever they appear; a hat
# directly above a quote run merges into that run's block)
_ATTRIB_ON_WROTE_RE = re.compile(
    r"(?i)^[ \t]*On\b[^\n]{0,200}\bwrote:[ \t>]*$"
)
# bare "Name wrote:" — the DOMINANT shape in the real corpus audit
# (ham_0008 L47 "Martin Adamson wrote:", ham_0026 L44 "John P.
# Looney wrote:"); first non-ws char must not be ``>`` so quoted
# attributions stay part of their quote run (D1 decision line)
_ATTRIB_NAME_WROTE_RE = re.compile(
    r"(?i)^[ \t]*[^\s>][^\n]{0,80}wrote:[ \t>]*$"
)
_ATTRIB_CHINESE_RE = re.compile(
    r"^[ \t]*在[^\n]{0,200}写道[:：][ \t>]*$"
)
_ATTRIB_FROM_LINE_RE = re.compile(r"(?i)^[ \t]*From:[ \t]*\S")
_ATTRIB_SENT_LINE_RE = re.compile(r"(?i)^[ \t]*Sent:")
_ATTRIB_FORWARD_WINDOW = 4  # Sent: within this many lines after From:

# E4 — RFC 3676 signature separator: exactly ``-- `` and nothing
# else on the line (one trailing space; \r stripped beforehand).
_SIG_SEPARATOR = "-- "

# E5 — attachment + MIME-boundary locators (whole-file scan: part
# headers live in the body region of multipart mails)
_ATTACH_LINE_RE = re.compile(
    r"(?i)^Content-Disposition:[^\r\n]*\battachment\b"
)
_FILENAME_PARAM_RE = re.compile(
    r'(?i)filename[ \t]*=[ \t]*"?([^";\r\n]+)"?'
)
_NAME_PARAM_RE = re.compile(
    r'(?i)(?<![A-Za-z])name[ \t]*=[ \t]*"?([^";\r\n]+)"?'
)
_BOUNDARY_PARAM_RE = re.compile(
    r'(?i)boundary[ \t]*=[ \t]*"?([^";\r\n]+)"?'
)
_CONTINUATION_RE = re.compile(r"^[ \t]+\S")


def _quote_depth(line: str) -> int | None:
    """``>`` count of the leading [ \t>] prefix, or None if no ``>``."""
    prefix = _QUOTE_PREFIX_RE.match(line).group(0)  # type: ignore[union-attr]
    depth = prefix.count(">")
    return depth if depth >= 1 else None


def _locate_attribution(lines: list[str]) -> dict[int, str]:
    """Line number → attribution form, for every attribution line.

    Forms: ``on_wrote`` (On ... wrote:), ``chinese_wrote``
    (在 ... 写道：), ``forward_header`` (a From: line with a Sent:
    line within the following window — the forwarded-mail header
    block). Pure facts; merging into quote blocks happens later.
    """
    found: dict[int, str] = {}
    for idx, line in enumerate(lines):
        if _ATTRIB_ON_WROTE_RE.match(line):
            found[idx + 1] = "on_wrote"
        elif _ATTRIB_CHINESE_RE.match(line):
            found[idx + 1] = "chinese_wrote"
        elif _ATTRIB_NAME_WROTE_RE.match(line):
            found[idx + 1] = "name_wrote"
    # forward headers need the From/Sent pair, so a second pass
    for idx, line in enumerate(lines):
        if _ATTRIB_FROM_LINE_RE.match(line) and (idx + 1) not in found:
            window = lines[idx + 1 : idx + 1 + _ATTRIB_FORWARD_WINDOW]
            if any(_ATTRIB_SENT_LINE_RE.match(w) for w in window):
                found[idx + 1] = "forward_header"
    return found


def _locate_quote_blocks(
    lines: list[str],
    attributions: dict[int, str],
) -> list[dict[str, Any]]:
    """Quote runs with adjacent attribution hats merged (E3).

    A run is a maximal sequence of consecutive ``>``-prefixed
    lines; ``max_depth`` is the deepest prefix in the run. An
    attribution line DIRECTLY above the run start is the run's
    hat — the block starts at the hat. Uncapped: the chunker needs
    the full list; the payload caps its own sample.
    """
    blocks: list[dict[str, Any]] = []
    idx = 0
    n = len(lines)
    while idx < n:
        depth = _quote_depth(lines[idx])
        if depth is None:
            idx += 1
            continue
        run_start = idx
        max_depth = depth
        while idx < n:
            d = _quote_depth(lines[idx])
            if d is None:
                break
            max_depth = max(max_depth, d)
            idx += 1
        block_start = run_start + 1  # 1-based
        attribution_line = None
        attribution_form = None
        if run_start >= 1 and (run_start) in attributions:
            # 0-based run_start-1 is the line above the run
            above = run_start  # 1-based line number of the line above
            if above in attributions:
                attribution_line = above
                attribution_form = attributions[above]
                block_start = above
        blocks.append({
            "start_line": block_start,
            "end_line": idx,  # 1-based last run line
            "max_depth": max_depth,
            "attribution_line": attribution_line,
            "attribution_form": attribution_form,
        })
    return blocks


def _locate_attachments(lines: list[str]) -> list[dict[str, Any]]:
    """Content-Disposition attachment lines + filename params (E5).

    The filename/name parameter may sit on the attachment line or
    on its folded continuations (searched within a small window);
    a missing name is an honest None, never a guess. Uncapped —
    the payload caps its own sample.
    """
    attachments: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        if not _ATTACH_LINE_RE.match(line):
            continue
        scan = [line] + [
            lines[j]
            for j in range(
                idx + 1, min(idx + 1 + _MAX_FOLD_SCAN, len(lines))
            )
            if _CONTINUATION_RE.match(lines[j])
        ]
        filename: str | None = None
        for text in scan:
            m = _FILENAME_PARAM_RE.search(text)
            if m:
                filename = m.group(1).strip()[:_MAX_TEXT_CHARS]
                break
        if filename is None:
            for text in scan:
                m = _NAME_PARAM_RE.search(text)
                if m:
                    filename = m.group(1).strip()[:_MAX_TEXT_CHARS]
                    break
        attachments.append({"line": idx + 1, "filename": filename})
    return attachments


def _locate_boundary(
    lines: list[str],
) -> tuple[str | None, list[int]]:
    """MIME boundary parameter + its marker line numbers (E5).

    The boundary is taken from the first Content-Type line that
    carries one; marker lines are ``--boundary`` / ``--boundary--``
    anchored at line start. Uncapped — the payload caps its own
    sample.
    """
    boundary: str | None = None
    for line in lines:
        m = _BOUNDARY_PARAM_RE.search(line)
        if m:
            boundary = m.group(1).strip()
            break
    if not boundary:
        return None, []
    marker = re.compile(
        r"^--" + re.escape(boundary) + r"(--)?[ \t]*$"
    )
    marker_lines = [idx + 1 for idx, line in enumerate(lines)
                    if marker.match(line)]
    return boundary, marker_lines


def build_email_payload(original_text: str) -> dict[str, Any]:
    """Deterministic structure-fact ledger + atomic chunks (v1).

    Pure function; no I/O; never raises on empty text (an empty
    ledger with zero stats is a legal, honest outcome).
    """
    lines = original_text.split("\n")
    # CRLF tolerance: matching happens on \r-stripped lines, while
    # line numbers (the ledger's coordinates) are unaffected
    stripped = [line[:-1] if line.endswith("\r") else line
                for line in lines]
    n = len(lines)

    # --- E2: header/body boundary ---------------------------------
    # a blank line that is the LAST split entry is the trailing-
    # newline artifact, not a separator — it is not a boundary
    blank_idx: int | None = None
    for idx, line in enumerate(stripped):
        if not line.strip():
            blank_idx = idx
            break
    if blank_idx is not None and blank_idx == len(stripped) - 1:
        blank_idx = None
    header_end_line = (blank_idx + 1) if blank_idx is not None else None

    # --- mbox separator (real-corpus fact, not a header field) ---
    mbox_from_line: int | None = None
    if stripped and _MBOX_FROM_RE.match(stripped[0]):
        mbox_from_line = 1
    header_start = 2 if mbox_from_line else 1

    # --- E1: header fields ----------------------------------------
    known = {name.lower(): name for name in _HEADER_FIELD_NAMES}
    header_fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    header_lines_total = 0
    if n >= header_start:
        region_end = (header_end_line - 1) if header_end_line else n
        last_field: dict[str, Any] | None = None
        for idx in range(header_start - 1, region_end):
            line = stripped[idx]
            if not line.strip():
                continue  # blank artifact inside a degenerate region
            header_lines_total += 1
            m = _HEADER_FIELD_RE.match(line)
            if m:
                canonical = known.get(m.group("name").lower())
                if canonical is not None and canonical not in seen:
                    seen.add(canonical)
                    value = line[m.end("name") + 1 :].lstrip()
                    entry = {
                        "field": canonical,
                        "line": idx + 1,
                        "value": value[:_MAX_TEXT_CHARS],
                        "fold_lines": 0,
                    }
                    header_fields.append(entry)
                    last_field = entry
                else:
                    last_field = None
            elif _CONTINUATION_RE.match(line) and last_field is not None:
                last_field["fold_lines"] += 1
            else:
                last_field = None

    # --- E3: quote blocks + attributions ---------------------------
    attributions = _locate_attribution(stripped)
    quote_blocks = _locate_quote_blocks(stripped, attributions)
    attribution_lines = [
        {"line": line, "form": form}
        for line, form in sorted(attributions.items())
    ]

    # --- E4: signature block ---------------------------------------
    # block end = EOF or the next quote block start; "EOF" is the
    # last content line — trailing blank lines are the trailing-
    # newline artifact, not signature content
    effective_end = n
    while (
        effective_end > 0
        and not stripped[effective_end - 1].strip()
    ):
        effective_end -= 1
    if effective_end == 0:
        effective_end = n
    signature: dict[str, Any] | None = None
    for idx, line in enumerate(stripped):
        if line == _SIG_SEPARATOR:
            separator_line = idx + 1
            later_blocks = [
                b["start_line"] for b in quote_blocks
                if b["start_line"] > separator_line
            ]
            block_end = (
                min(later_blocks) - 1 if later_blocks else effective_end
            )
            signature = {
                "separator_line": separator_line,
                "block_start_line": separator_line,
                "block_end_line": block_end,
            }
            break

    # --- E5: attachments + MIME boundaries -------------------------
    attachments = _locate_attachments(stripped)
    mime_boundary, boundary_lines_all = _locate_boundary(stripped)

    # --- chunking: protected regions are LEDGER FACTS ---------------
    protected: list[tuple[int, int]] = []
    if n >= header_start:
        if header_end_line is not None:
            if header_end_line - 1 >= header_start:
                protected.append((header_start, header_end_line - 1))
        else:
            # degenerate: body never separated — the whole file is
            # the header region, protected conservatively
            protected.append((header_start, n))
    for block in quote_blocks:
        protected.append((block["start_line"], block["end_line"]))
    if signature is not None:
        protected.append(
            (signature["block_start_line"], signature["block_end_line"])
        )
    chunk_result = chunk_email_text(original_text, protected)
    chunk_dicts = [
        {"start_line": c.start_line, "end_line": c.end_line, "text": c.text}
        for c in chunk_result.chunks
    ]
    chunk_inv = check_chunk_invariants(original_text, chunk_result.chunks)

    return {
        "extractor": "email_scenario_v1",
        "total_lines": n,
        "mbox_from_line": mbox_from_line,
        "header_end_line": header_end_line,
        "header_fields": header_fields,
        "header_lines_total": header_lines_total,
        "quote_blocks": quote_blocks[:_MAX_QUOTE_BLOCKS],
        "attribution_lines": attribution_lines[:_MAX_ATTRIBUTION_LINES],
        "signature": signature,
        "attachments": attachments[:_MAX_ATTACHMENTS],
        "mime_boundary": mime_boundary,
        "mime_boundary_lines": boundary_lines_all[:_MAX_BOUNDARY_LINES],
        "chunks": {
            "chunks": chunk_dicts,
            "stats": chunk_result.stats,
        },
        "chunk_invariants": {
            "verdict": chunk_inv.verdict,
            "missing_lines": list(chunk_inv.missing_lines),
            "integrity_violations": list(chunk_inv.integrity_violations),
            "duplicate_lines": list(chunk_inv.duplicate_lines),
            "stats": chunk_inv.stats,
        },
        "stats": {
            "header_fields": len(header_fields),
            "header_lines": header_lines_total,
            "quote_blocks": len(quote_blocks),
            "attributions": len(attribution_lines),
            "attachments": len(attachments),
            "mime_boundary_lines": len(boundary_lines_all),
            "quote_blocks_capped": (
                len(quote_blocks) > _MAX_QUOTE_BLOCKS
            ),
            "attribution_lines_capped": (
                len(attribution_lines) > _MAX_ATTRIBUTION_LINES
            ),
            "attachments_capped": len(attachments) > _MAX_ATTACHMENTS,
            "mime_boundary_lines_capped": (
                len(boundary_lines_all) > _MAX_BOUNDARY_LINES
            ),
        },
        # honest boundary, machine-readable
        "note": (
            "structure-fact ledger + atomic chunks: header fields are "
            "first-occurrence facts of the 13 known names (folded "
            "continuations counted, never split); the header/body "
            "boundary is the first blank line (None = body never "
            "separated, honest absence); quote blocks are >-prefixed "
            "runs with adjacent attribution hats merged "
            "(On...wrote: / bare Name wrote: / 在...写道： / "
            "From:...Sent: forward headers — unknown shapes are "
            "honest misses); the signature is the RFC 3676 '-- ' "
            "separator token; "
            "attachments record Content-Disposition lines with raw "
            "filename/name parameters, no MIME decoding, no "
            "thread-tree reconstruction, no spam/sender "
            "classification; the mbox 'From sender date' separator "
            "line is recorded as mbox_from_line, never a header "
            "field; capped lists are sample bounds — the chunker is "
            "fed the uncapped region list; chunks never split the "
            "header block, quote blocks or the signature block and "
            "never modify text"
        ),
    }


def attach_email_scenario(document: Any) -> None:
    """Stamp the ledger on ``metadata[SCENARIO_METADATA_KEY]``.

    Attach-only: mutates ``document.metadata`` in place, mirroring
    the runtime stamping pattern; document text fields are never
    touched.
    """
    original_text = getattr(document, "original_text", "") or ""
    document.metadata[SCENARIO_METADATA_KEY] = build_email_payload(
        original_text
    )


__all__ = [
    "SCENARIO_METADATA_KEY",
    "attach_email_scenario",
    "build_email_payload",
]
