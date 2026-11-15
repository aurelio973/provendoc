"""OMML (Office Math Markup Language) evidence extraction.

python-docx skips ``m:oMath`` elements entirely: ``paragraph.text`` joins only
``w:r``/``w:t`` runs, so a display equation paragraph appears empty and the
formula text silently disappears.  This module keeps the facts the OMML XML
actually carries:

- ``omml_linear_text`` — the verbatim concatenation of ``m:t`` runs in
  document order.  It is a projection of the XML, not a rendering; fraction
  "a over b" reads "ab" and the structure is reported separately.
- ``omml_structure_counts`` — how many fractions, superscripts, subscripts,
  matrices, radicals, and other constructs each formula contains.
- ``omml_to_mathml`` — a structural subset conversion to MathML for the
  common constructs.  Elements outside the subset are not guessed: their
  linear text is emitted as ``mtext`` and the element name is recorded in
  ``unsupported`` so consumers can see exactly what was not converted.

Nothing here invents values: no LaTeX, no rendered layout, no token typing
beyond what the OMML structure itself states.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

OMATH = f"{{{MATH_NS}}}oMath"
OMATH_PARA = f"{{{MATH_NS}}}oMathPara"
M_T = f"{{{MATH_NS}}}t"

# OMML structural elements worth counting as formula-internal structure.
_STRUCTURE_ELEMENTS: dict[str, str] = {
    f"{{{MATH_NS}}}f": "fraction",
    f"{{{MATH_NS}}}sSup": "superscript",
    f"{{{MATH_NS}}}sSub": "subscript",
    f"{{{MATH_NS}}}sSubSup": "sub_superscript",
    f"{{{MATH_NS}}}rad": "radical",
    f"{{{MATH_NS}}}m": "matrix",
    f"{{{MATH_NS}}}mr": "matrix_row",
    f"{{{MATH_NS}}}d": "delimiter",
    f"{{{MATH_NS}}}nary": "n_ary",
    f"{{{MATH_NS}}}limLow": "lower_limit",
    f"{{{MATH_NS}}}limUpp": "upper_limit",
    f"{{{MATH_NS}}}bar": "bar",
    f"{{{MATH_NS}}}func": "function",
    f"{{{MATH_NS}}}box": "box",
    f"{{{MATH_NS}}}acc": "accent",
    f"{{{MATH_NS}}}groupChr": "group_character",
    f"{{{MATH_NS}}}eqArr": "equation_array",
}

MATHML_NS = "http://www.w3.org/1998/Math/MathML"

# Characters that are unambiguously MathML operators; everything alphabetic
# becomes an identifier, digits become numbers, and anything else is kept as
# mtext rather than guessed.
_OPERATOR_CHARS = set("+-=<>≤≥≠≈×·÷±∓∑∏∫√∞∈∉⊂⊃∪∩∀∃→←↔⇒⇐⇔")


def omml_linear_text(element: Any) -> str:
    """Concatenate every ``m:t`` run under ``element`` in document order.

    ``element`` is usually an ``m:oMath`` node.  Structural elements (fraction
    bars, scripts, matrices) contribute no text of their own; their ``m:t``
    descendants appear in the order Word wrote them.
    """
    return "".join(
        (node.text or "") for node in element.iter(M_T)
    )


def omml_structure_counts(element: Any) -> dict[str, int]:
    """Count OMML structural constructs under ``element`` (matrix rows included).

    Counts are facts about the XML; they never claim how the formula renders.
    """
    counts: dict[str, int] = {}
    for node in element.iter():
        label = _STRUCTURE_ELEMENTS.get(node.tag)
        if label is not None:
            counts[label] = counts.get(label, 0) + 1
    return counts


def is_display_math(oMath_element: Any) -> bool:
    """Whether the ``m:oMath`` sits inside an ``m:oMathPara`` (display equation).

    Inline ``m:oMath`` lives directly in a ``w:p``; display equations are
    wrapped in ``m:oMathPara`` and typically get their own paragraph and a
    trailing equation number.

    Requires an lxml tree (``iterancestors``): stdlib ElementTree nodes
    carry no parent links, so callers on stdlib trees must compute the
    display fact themselves (e.g. via a parent map) and pass
    ``display=`` to :func:`omml_to_mathml`; here the answer degrades
    honestly to ``False`` rather than raising.
    """
    try:
        ancestors = oMath_element.iterancestors()
    except AttributeError:
        return False
    return any(ancestor.tag == OMATH_PARA for ancestor in ancestors)


def iter_omath(paragraph_element: Any) -> Iterator[Any]:
    """Yield ``m:oMath`` elements of a ``w:p`` in document order."""
    yield from paragraph_element.findall(f".//{OMATH}")


def omml_to_mathml(
    oMath_element: Any, display: bool | None = None
) -> tuple[str, list[str]]:
    """Convert an ``m:oMath`` to a MathML string (structural subset).

    Returns ``(mathml_string, unsupported_element_names)``.  Supported
    constructs map structurally: m:f→mfrac, m:sSup→msup, m:sSub→msub,
    m:sSubSup→msubsup, m:rad→msqrt/mroot, m:d→delimited mrow, m:m→mtable.
    Text runs map per character to mi/mn/mo, everything else to mtext.
    Unsupported structural elements degrade to their linear text inside an
    ``mtext`` node and are reported by name — never silently dropped, never
    guessed.

    ``display`` overrides the display-math detection for trees without
    parent links (stdlib ElementTree); ``None`` = detect via ancestors
    (lxml).  The pptx parser passes the value computed from its own
    parent map.
    """
    unsupported: list[str] = []
    if display is None:
        display = is_display_math(oMath_element)
    root = _E(
        "math",
        {"display": "block" if display else "inline"},
    )
    _convert_children(oMath_element, root, unsupported)
    ET.indent(root, space="  ")
    ET.register_namespace("", MATHML_NS)
    return ET.tostring(root, encoding="unicode"), sorted(set(unsupported))


def _E(tag: str, attrib: dict[str, str] | None = None) -> ET.Element:
    return ET.Element(f"{{{MATHML_NS}}}{tag}", attrib or {})


def _local_name(tag: Any) -> str:
    return str(tag).split("}", 1)[-1]


def _convert_children(source: Any, target: ET.Element, unsupported: list[str]) -> None:
    for child in source:
        _convert_node(child, target, unsupported)


def _convert_node(node: Any, target: ET.Element, unsupported: list[str]) -> None:
    name = _local_name(node.tag)
    if name in ("rPr", "ctrlPr", "dPr", "naryPr", "mPr", "radPr", "fPr", "sSupPr",
                "sSubPr", "sSubSupPr", "funcPr", "limLowPr", "limUppPr", "barPr",
                "accPr", "groupChrPr", "boxPr", "eqArrPr", "oMathParaPr", "argPr",
                "r", "t", "argStyle", "degHide"):
        # Property elements carry no math content.  Runs are handled by their
        # parent constructs; at top level they are emitted as text below.
        if name == "r":
            _emit_run_text(node, target)
        return

    if name == "f":
        frac = _E("mfrac")
        _wrap_children(node, frac, ("num", "den"), unsupported)
        target.append(frac)
        return
    if name == "sSup":
        el = _E("msup")
        _wrap_children(node, el, ("e", "sup"), unsupported)
        target.append(el)
        return
    if name == "sSub":
        el = _E("msub")
        _wrap_children(node, el, ("e", "sub"), unsupported)
        target.append(el)
        return
    if name == "sSubSup":
        el = _E("msubsup")
        _wrap_children(node, el, ("e", "sub", "sup"), unsupported)
        target.append(el)
        return
    if name == "limLow":
        el = _E("munder")
        _wrap_children(node, el, ("e", "lim"), unsupported)
        target.append(el)
        return
    if name == "limUpp":
        el = _E("mover")
        _wrap_children(node, el, ("e", "lim"), unsupported)
        target.append(el)
        return
    if name == "rad":
        deg = node.find(f"{{{MATH_NS}}}deg")
        body = _E("mroot" if deg is not None else "msqrt")
        if deg is not None and _has_content(deg):
            _convert_children(deg, body, unsupported)
        e_node = node.find(f"{{{MATH_NS}}}e")
        if e_node is not None:
            _convert_children(e_node, body, unsupported)
        target.append(body)
        return
    if name == "d":
        row = _E("mrow")
        open_mo = _E("mo")
        open_mo.text = "("
        close_mo = _E("mo")
        close_mo.text = ")"
        beg, end = _delimiter_characters(node)
        open_mo.text = beg
        close_mo.text = end
        row.append(open_mo)
        _convert_children(node, row, unsupported)
        row.append(close_mo)
        target.append(row)
        return
    if name == "m":
        table = _E("mtable")
        for mr in node.findall(f"{{{MATH_NS}}}mr"):
            tr = _E("mtr")
            _convert_children(mr, tr, unsupported)
            table.append(tr)
        target.append(table)
        return
    if name == "mr":
        # Standalone row (should not occur outside m:m).
        tr = _E("mtr")
        _convert_children(node, tr, unsupported)
        target.append(tr)
        return
    if name in ("e", "num", "den", "sup", "sub", "deg", "lim"):
        _convert_children(node, target, unsupported)
        return
    if name in ("oMath", "oMathPara"):
        _convert_children(node, target, unsupported)
        return

    # Unsupported structural element: keep its linear text verbatim, report
    # the element name.  No structure is invented.
    unsupported.append(name)
    text = omml_linear_text(node)
    if text:
        mt = _E("mtext")
        mt.text = text
        target.append(mt)


def _wrap_children(
    node: Any,
    target: ET.Element,
    child_names: tuple[str, ...],
    unsupported: list[str],
) -> None:
    """Convert named child constructs into mrow-wrapped MathML arguments."""
    for child_name in child_names:
        child = node.find(f"{{{MATH_NS}}}{child_name}")
        row = _E("mrow")
        if child is not None:
            _convert_children(child, row, unsupported)
        target.append(row)


def _delimiter_characters(node: Any) -> tuple[str, str]:
    """Read begChr/endChr from ``m:dPr``; default to round parentheses."""
    beg, end = "(", ")"
    dPr = node.find(f"{{{MATH_NS}}}dPr")
    if dPr is not None:
        beg_node = dPr.find(f"{{{MATH_NS}}}begChr")
        end_node = dPr.find(f"{{{MATH_NS}}}endChr")
        if beg_node is not None:
            beg = _attribute_value(beg_node) or "("
        if end_node is not None:
            end = _attribute_value(end_node) or ")"
    return beg, end


def _attribute_value(node: Any) -> str | None:
    """Read a ``val`` attribute across producer conventions.

    Word writes ``m:val`` (math namespace); some producers emit the attribute
    unqualified or with the word namespace.  All three spellings are read.
    """
    for key in (f"{{{MATH_NS}}}val", f"{{{WORD_NS}}}val", "val"):
        value = node.get(key)
        if value:
            return value
    return None


def _has_content(node: Any) -> bool:
    return bool(omml_linear_text(node).strip())


def _emit_run_text(run_node: Any, target: ET.Element) -> None:
    """Emit an ``m:r`` run's ``m:t`` text, classified per character.

    Classification is a heuristic projection stated openly: alphabetic
    single characters (including Greek) become ``mi``, digits and decimal
    points inside digit runs become ``mn``, known operator characters become
    ``mo``, and anything else is preserved as ``mtext``.
    """
    for text_node in run_node.findall(M_T):
        text = text_node.text or ""
        for chunk in _tokenize_run_text(text):
            kind, value = chunk
            element = _E(kind)
            element.text = value
            target.append(element)


def _tokenize_run_text(text: str) -> Iterator[tuple[str, str]]:
    digits = re.compile(r"\d+(?:\.\d+)?")
    position = 0
    while position < len(text):
        char = text[position]
        if char.isdecimal():
            # isdecimal (category Nd) matches the decision domain of
            # \d below; isdigit would additionally admit superscript/
            # circled digits (e.g. '²'), which \d does not match — match
            # becomes None and .group(0) raises AttributeError.
            match = digits.match(text, position)
            if match is not None:
                yield ("mn", match.group(0))
                position = match.end()
            else:  # pragma: no cover — isdecimal guarantees Nd, \d matches Nd
                yield ("mtext", char)
                position += 1
            continue
        if char in _OPERATOR_CHARS:
            yield ("mo", char)
            position += 1
            continue
        if char.isalpha():
            yield ("mi", char)
            position += 1
            continue
        if char.isspace():
            position += 1
            continue
        yield ("mtext", char)
        position += 1
