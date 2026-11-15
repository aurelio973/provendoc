# ============================================================
# Shared Text Utilities
# Markdown fenced code block extraction
# ============================================================
"""Markdown fenced-code-block extraction — the shared truth source
for the parse and representation layers.

Extraction semantics are strictly isomorphic to the fence rules of
markdown_narrative_representation._extract_discourse_units:
    - opener: a line that, after strip, starts with ``` or ~~~ (the
      info string is whatever follows the fence characters after
      strip)
    - closer: the first line after strip starting with the same
      fence character (included in the block)
    - unclosed-fence tolerance: the block extends to end of document

The isomorphism is part of the contract: the representation layer
aligns code blocks in document order and verifies block text
verbatim (sha1); on mismatch it degrades honestly to plain narrative
handling — if the two scan paths ever diverge, the degradation path
keeps behavior predictable instead of misaligned.
"""

from typing import Any, Dict, List


def content_digest(content: str) -> str:
    """sha1 prefix of the fenced inner code — the alignment-verification
    key between parse-layer evidence and representation-layer units.

    Both layers derive the inner code text independently and compare
    the same digest to confirm verbatim equality — no need to carry
    the full text around in metadata.
    """
    import hashlib

    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]


def extract_fenced_code_blocks(text: str) -> List[Dict[str, Any]]:
    """Extract all fenced code blocks (``` and ~~~) in document order.

    Returns:
        list of dict:
            index        — code-block document ordinal (0-based, code
                           blocks only)
            fence_char   — "```" or "~~~"
            fence_info   — info string of the opener line (post-strip,
                           may be "")
            closed       — whether a closing fence exists
            content      — inner code (bare lines joined with "\n")
            block_text   — full block text including fences (isomorphic
                           with discourse unit.text)
            line_start / line_end — 1-based line numbers (fence lines
                           included)
            byte_start / byte_end — UTF-8 byte span of the block in the
                           source text (byte_end is exclusive, pointing
                           before the newline of the closing fence line)
    """
    lines = text.splitlines(keepends=True)
    bare = [line.rstrip("\r\n") for line in lines]
    # Byte offset of each line start (running prefix sum), for span
    # facts
    line_byte_offsets = []
    offset = 0
    for line in lines:
        line_byte_offsets.append(offset)
        offset += len(line.encode("utf-8"))
    total_bytes = offset

    blocks: List[Dict[str, Any]] = []
    i = 0
    n = len(bare)
    while i < n:
        stripped = bare[i].strip()
        if not (stripped.startswith("```") or stripped.startswith("~~~")):
            i += 1
            continue

        fence_char = "```" if stripped.startswith("```") else "~~~"
        fence_info = stripped[len(fence_char):].strip()

        j = i + 1
        while j < n and not bare[j].strip().startswith(fence_char):
            j += 1
        closed = j < n
        inner_end = j if closed else n  # exclusive

        content = "\n".join(bare[i + 1:inner_end])
        block_text = "\n".join(bare[i:inner_end + 1 if closed else n])
        byte_start = line_byte_offsets[i]
        byte_end = (
            line_byte_offsets[inner_end] + len(bare[inner_end].encode("utf-8"))
            if closed
            else total_bytes
        )

        blocks.append({
            "index": len(blocks),
            "fence_char": fence_char,
            "fence_info": fence_info,
            "closed": closed,
            "content": content,
            "block_text": block_text,
            "line_start": i + 1,
            "line_end": (inner_end if closed else n - 1) + 1,
            "byte_start": byte_start,
            "byte_end": byte_end,
        })

        i = inner_end + 1 if closed else n

    return blocks
