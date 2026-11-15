# ============================================================
# Narrative Domain
# DOCX object-level revision records (docx_revisions_v1)
# ============================================================
"""Object-level tracked-change records for the DOCX body part.

Companion registry to the narrative final-view fix
(docx_revision_text_prereg_v1 §2/§3): ``w:ins`` text now joins the
narrative text (final view — insertions included, deletions
excluded), ``w:del`` text never
does — this module records both, element by element, so the
revision-marked state stays available as evidence without any view
being resolved or guessed downstream.

Facts (``metadata["docx_revisions"]``, written only when the body
actually has ``w:ins``/``w:del`` elements — write only when non-empty):

- ``records``: one record per ``w:ins``/``w:del`` element in body XML
  document order (``object_type`` ``revision_insertion``/
  ``revision_deletion``; ``revision_type``/``text``/``author``/
  ``date``/``paragraph_index``), text truncated at 200 chars,
  100 records max (overflow in ``truncated_count``)
- ``insertion_count``/``deletion_count``: full ``w:ins``/``w:del``
  element occurrence counts in the body part (same scope as
  ``docx_review.tracked_changes`` counts — those keys are untouched)
- ``authors``: distinct ``w:author`` values over all tracked elements

Fact boundaries (documented, not guessed):

- only the document body part is scanned (header/footer parts are
  not scanned — same boundary as ``docx_review``)
- row-level/paragraph-level revision marks carry no run text and stay
  count-only facts in ``docx_review``; only ``w:ins``/``w:del``
  elements are registered here
- move marks (r25 face-2 ①): ``w:moveFrom``/``w:moveTo`` are
  registered as object-level records in ``move_records``
  (``revision_type`` ``move_from``/``move_to``; same 200-char text
  cap and 100-record cap, overflow in ``move_truncated_count``) with
  ``move_from_count``/``move_to_count`` full occurrence counts.
  moveFrom text (``w:delText`` and legacy ``w:t`` runs) never enters
  the narrative final view — same exclusion rule as ``w:del`` in the
  narrative walk; moveTo text stays in the final view like ``w:ins``
"""
from __future__ import annotations

# lxml Clark notation: element tags and attribute names are
# "{{namespace-uri}}local" — the braces are part of the name.
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W_INS = f"{{{_W_NS}}}ins"
_W_DEL = f"{{{_W_NS}}}del"
_W_MOVE_FROM = f"{{{_W_NS}}}moveFrom"
_W_MOVE_TO = f"{{{_W_NS}}}moveTo"
_W_T = f"{{{_W_NS}}}t"
_W_DEL_TEXT = f"{{{_W_NS}}}delText"
_W_P = f"{{{_W_NS}}}p"

EXTRACTOR = "docx_revisions_v1"
_MAX_TEXT_CHARS = 200
_MAX_RECORDS = 100


def _direct_paragraph_indexes(body):
    """Map ``id()`` of direct body-child paragraphs to their index.

    Mirrors the parser's ``paragraph_index`` contract (direct ``w:p``
    children of the body only, same enumeration as
    ``doc.paragraphs``) so joins stay aligned.

    lxml proxy discipline: ``id()`` is only stable while the proxy is
    alive, so the paragraph elements themselves are returned too —
    the caller must keep them referenced for the whole lookup span
    (a fresh proxy for the same node could otherwise read a
    recycled id and misjoin).
    """
    paragraphs = [child for child in body if child.tag == _W_P]
    indexes = {
        id(child): index for index, child in enumerate(paragraphs)
    }
    return indexes, paragraphs


def _element_text(element, text_tag):
    """Join descendant text nodes of ``text_tag``, truncated at
    ``_MAX_TEXT_CHARS``; empty joins read as ``None`` (bad-entry
    defense: only ``str`` node text joins)."""
    parts = []
    for node in element.iter(text_tag):
        text = node.text
        if isinstance(text, str):
            parts.append(text)
    joined = "".join(parts)
    if not joined:
        return None
    return joined[:_MAX_TEXT_CHARS]


def _element_text_any(element, text_tags):
    """Join descendant text nodes of any tag in ``text_tags`` in
    document order (moveFrom runs carry ``w:delText`` in Word output
    but legacy producers emit plain ``w:t`` — both are registered
    verbatim); same truncation/empty contract as ``_element_text``."""
    parts = []
    for node in element.iter(*text_tags):
        text = node.text
        if isinstance(text, str):
            parts.append(text)
    joined = "".join(parts)
    if not joined:
        return None
    return joined[:_MAX_TEXT_CHARS]


def _revision_paragraph_index(element, paragraph_indexes):
    """Index of the direct body-child paragraph owning ``element``.

    Revisions inside table cells / content controls / headers are
    anchored to non-direct paragraphs and read as ``None`` — an
    information fact, never a guess.
    """
    for ancestor in element.iterancestors():
        if ancestor.tag == _W_P:
            return paragraph_indexes.get(id(ancestor))
    return None


def extract_revision_summary(doc):
    """Object-level revision records for the body part (or ``None``).

    Args:
        doc: an opened ``docx.Document`` instance.

    Returns:
        dict for ``metadata["docx_revisions"]`` or ``None`` when the
        body has no ``w:ins``/``w:del`` elements (write only when
        non-empty).
    """
    body = doc.element.body
    paragraph_indexes, paragraphs_keepalive = _direct_paragraph_indexes(body)
    records = []
    move_records = []
    authors = set()
    truncated_count = 0
    move_truncated_count = 0
    insertion_count = 0
    deletion_count = 0
    move_from_count = 0
    move_to_count = 0
    for element in body.iter(_W_INS, _W_DEL, _W_MOVE_FROM, _W_MOVE_TO):
        is_insertion = element.tag == _W_INS
        is_move_from = element.tag == _W_MOVE_FROM
        is_move_to = element.tag == _W_MOVE_TO
        if is_insertion:
            insertion_count += 1
        elif is_move_from:
            move_from_count += 1
        elif is_move_to:
            move_to_count += 1
        else:
            deletion_count += 1
        author = element.get(f"{{{_W_NS}}}author") or None
        if author:
            authors.add(author)
        if is_move_from or is_move_to:
            # r25 face-2 (1): move markers get their own registry
            # chain (move_records), never entering records — the
            # "one record per w:ins/w:del" contract of records is
            # unchanged.
            if len(move_records) >= _MAX_RECORDS:
                move_truncated_count += 1
                continue
            try:
                move_records.append({
                    "object_type": (
                        "revision_move_from" if is_move_from
                        else "revision_move_to"
                    ),
                    "revision_type": (
                        "move_from" if is_move_from else "move_to"
                    ),
                    "text": (
                        _element_text_any(
                            element, (_W_T, _W_DEL_TEXT)
                        )
                        if is_move_from
                        else _element_text(element, _W_T)
                    ),
                    "author": author,
                    "date": element.get(f"{{{_W_NS}}}date") or None,
                    "paragraph_index": _revision_paragraph_index(
                        element,
                        paragraph_indexes,
                    ),
                })
            except Exception:
                # One bad record does not spoil the batch (prereg §2):
                # skip on a single record-construction exception.
                continue
            continue
        if len(records) >= _MAX_RECORDS:
            truncated_count += 1
            continue
        try:
            records.append({
                "object_type": (
                    "revision_insertion" if is_insertion
                    else "revision_deletion"
                ),
                "revision_type": (
                    "insertion" if is_insertion else "deletion"
                ),
                "text": _element_text(
                    element,
                    _W_T if is_insertion else _W_DEL_TEXT,
                ),
                "author": author,
                "date": element.get(f"{{{_W_NS}}}date") or None,
                "paragraph_index": _revision_paragraph_index(
                    element,
                    paragraph_indexes,
                ),
            })
        except Exception:
            # One bad record does not spoil the batch (prereg §2):
            # skip on a single record-construction exception.
            continue
    if not (
        insertion_count or deletion_count
        or move_from_count or move_to_count
    ):
        return None
    result = {
        "extractor": EXTRACTOR,
        "insertion_count": insertion_count,
        "deletion_count": deletion_count,
        "authors": sorted(authors),
        "truncated_count": truncated_count,
        "records": records,
        "note": (
            "records are object-level evidence: tracked-insertion "
            "text (w:ins) is included in the narrative final view; "
            "tracked-deletion text (w:del/w:delText) never enters "
            "the narrative and is preserved here only; body part "
            "scanned only (header/footer parts are not scanned); "
            "records capped at 100 (truncated_count), record text "
            "truncated at 200 chars"
        ),
    }
    # Write only when non-empty (additive-only): documents without
    # move markers get zero new keys.
    if move_from_count or move_to_count:
        result["move_from_count"] = move_from_count
        result["move_to_count"] = move_to_count
        result["move_truncated_count"] = move_truncated_count
        result["move_records"] = move_records
        result["move_note"] = (
            "move marks are object-level evidence: moved-away text "
            "(w:moveFrom, w:delText/legacy w:t runs) never enters the "
            "narrative final view and is preserved here only; "
            "moved-here text (w:moveTo) stays in the final view like "
            "w:ins; move_records capped at 100 (move_truncated_count)"
        )
    return result
