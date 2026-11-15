# ============================================================
# Narrative Domain
# DOCX review-state evidence (tracked changes + comments)
# ============================================================
"""Review-state facts for DOCX packages.

Revision-marked content is split by view (docx_revision_text_prereg_v1
§3): the narrative text now carries the **final view** — ``w:ins``
run text included, ``w:delText`` excluded — while this module keeps
the count/author facts (unchanged keys), ``docx_revisions`` records
the per-element text evidence, and comment bodies never enter the
narrative text at all.  The point of this module remains making
document-state degradations *visible* instead of silent.

Facts recorded (``metadata["docx_review"]``):

- tracked changes: element occurrences of ``w:ins`` / ``w:del`` in the
  document body part (run-level, paragraph-level and table-row tracked
  edits all count), plus the distinct author set — these count/author
  keys are frozen; the per-element text records live in
  ``metadata["docx_revisions"]`` (see docx_revisions.py)
- comments: presence of the ``/word/comments.xml`` package part, its
  ``w:comment`` element count and author set, plus ``w:commentRangeStart``
  anchor occurrences in the body (inline evidence that commented ranges
  exist in the text)

Fact boundaries (documented, not guessed):

- only the document body part is scanned for tracked changes and anchors;
  header/footer parts are not scanned (noted in the summary)
- ``commentsExtended`` (w15) resolution state is not interpreted — the
  comment count is the raw ``w:comment`` element count
- comment bodies are never extracted into narrative text
"""
from __future__ import annotations

from docx.oxml.ns import qn

# w:ins / w:del element tags (tracked changes, ECMA-376 §17.13)
_TRACKED_TAGS = ("ins", "del")

_COMMENTS_PART_NAME = "/word/comments.xml"


def _distinct_authors(elements):
    """Distinct w:author attribute values, sorted; unauthored excluded."""
    authors = set()
    for element in elements:
        author = element.get(qn("w:author"))
        if author:
            authors.add(author)
    return sorted(authors)


def _tracked_changes_summary(body):
    marks = {
        tag: body.xpath(f".//w:{tag}") for tag in _TRACKED_TAGS
    }
    insertion_count = len(marks["ins"])
    deletion_count = len(marks["del"])
    authors = _distinct_authors(marks["ins"] + marks["del"])
    has_tracked_changes = bool(insertion_count or deletion_count)
    return {
        "has_tracked_changes": has_tracked_changes,
        "insertion_count": insertion_count,
        "deletion_count": deletion_count,
        "author_count": len(authors),
        "authors": authors,
        "note": (
            "the narrative text is the final view: tracked-insertion "
            "text (w:ins) is included, tracked-deletion text "
            "(w:del/w:delText) is excluded — deletion text is preserved "
            "verbatim in metadata['docx_revisions'] records; counts "
            "are w:ins/w:del element occurrences in the document body "
            "part only (header/footer parts are not scanned)"
        ),
    }


def _comments_summary(doc):
    """Comment-part facts; the part is never rendered into narrative."""
    part_present = False
    comment_count = 0
    authors = []
    note = "comment bodies never enter the narrative text; facts only"
    for part in doc.part.package.parts:
        if str(part.partname) != _COMMENTS_PART_NAME:
            continue
        part_present = True
        try:
            root = part._element if hasattr(part, "_element") else None
            if root is None:
                from lxml import etree

                root = etree.fromstring(part.blob)
            comment_elements = root.findall(qn("w:comment"))
            comment_count = len(comment_elements)
            authors = _distinct_authors(comment_elements)
        except Exception as exc:  # honest failure, never a guess
            note = f"comments part present but unreadable: {exc}"
        break
    # Inline anchor evidence lives in the body part regardless.
    try:
        anchor_count = len(doc.element.body.xpath(".//w:commentRangeStart"))
    except Exception:
        anchor_count = 0
    if not part_present and anchor_count:
        note = (
            "commentRangeStart anchors exist in the body but no "
            "/word/comments.xml part was found (orphaned anchors)"
        )
    return {
        "part_present": part_present,
        "comment_count": comment_count,
        "anchor_count": anchor_count,
        "author_count": len(authors),
        "authors": authors,
        "note": note,
    }


def extract_review_summary(doc) -> dict:
    """Review-state evidence: tracked changes + comments.

    Args:
        doc: an opened ``docx.Document`` instance.

    Returns:
        dict with ``tracked_changes`` and ``comments`` sub-summaries;
        every fact is read-only evidence, nothing is resolved.
    """
    body = doc.element.body
    return {
        "extractor": "docx_review_v1",
        "tracked_changes": _tracked_changes_summary(body),
        "comments": _comments_summary(doc),
    }
