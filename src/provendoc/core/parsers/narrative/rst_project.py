# ============================================================
# Narrative Domain
# reStructuredText project-mode aggregate facts
# (rst_project_mode_v1 / sphinx_conf_facts_v1 / rst_glossary_v1)
# ============================================================
"""reST project-mode aggregate facts (rst_project_mode_prereg_v1, D1-D7 all A).

Fact boundaries:
    - Aggregate-layer entry: ``collect_rst_project_facts(project_root, files,
      metadata)`` — takes a project root + a ``{relpath: text}`` file set
      (corpus/pipeline responsibility); the single-file parser (rst_structure.py)
      is never touched
    - All facts land in metadata keys; the text channel changes not one character
      (evidence-only, same contract as rst_structure_v1)
    - conf.py static facts: ast.parse + whitelist-key literal_eval,
      **never exec** (security red line; dynamic-expression values honestly
      register ``_nonliteral``)
    - Cross-file reference tri-state semantics (D3 A): resolved / not_in_corpus
      counts, no bad-judgement — misses are mostly Sphinx build-time std/domain
      labels and corpus sampling gaps (probe S2/S3 evidence); evidence-only
      fabricates no bad values
    - include root-walk reconciliation (D4 A): walks up the file's directory
      chain to the project root, paired with the single-file same-directory
      ``include_exists_check`` key (the semantic gap is probe S5: numpy 47/57
      vs same-directory ~0); the same-directory key stays frozen
    - glossary word list (D6 B, independent key ``rst_glossary_v1``): a term
      definition line = a non-empty line at the minimum indent level inside the
      glossary directive body (approximate parsing, docutils glossary semantics:
      term lines and definition bodies separated by indent level); :term:
      references reconciled as whole-word lowercase
    - Any failure silently degrades to ``rst_project_error``, never raises
"""
import ast
import posixpath
import re
from pathlib import Path

# ── Regex family (same family as rst_structure.py / harvester probes) ──
_LABEL_DEF_RE = re.compile(r"^\.\.\s+_([^:\s]+):\s*$", re.M)
_REF_RE = re.compile(r":ref:`([^`<]+?)(?:\s*<[^`>]*>)?`")
_DOC_RE = re.compile(r":doc:`([^`<]+?)(?:\s*<[^`>]*>)?`")
_TERM_RE = re.compile(r":term:`([^`<]+?)(?:\s*<[^`>]*>)?`")
_INCLUDE_TARGET_RE = re.compile(
    r"^\s*\.\.\s+(?:literalinclude|include)::\s*(\S+)", re.M)
_GLOSSARY_RE = re.compile(r"^\.\.\s+glossary::\s*$", re.M)

# conf.py whitelist (probe S1: key families all five projects expose as
# literals; dynamic-expression keys (version/release etc.) land in _nonliteral, unevaluated)
_CONF_WHITELIST = (
    "project", "author", "copyright", "version", "release",
    "master_doc", "root_doc", "language", "locale_dirs",
    "extensions", "source_suffix", "exclude_patterns",
    "templates_path", "nitpicky", "default_role",
)

# include root-walk ascent depth cap (deep-dir files referencing root-level assets;
# deepest observed corpus file path <8 levels)
_ROOT_WALK_MAX_DEPTH = 8

# toctree/:doc: target completion suffixes (Sphinx default source suffix family)
_DOC_SUFFIXES = (".rst", "", ".txt")


def parse_sphinx_conf(path) -> dict:
    """conf.py static-fact extraction (ast whitelist literal_eval, never exec).

    Returns the ``sphinx_conf_facts_v1`` payload: literal values of
    whitelist keys + the ``_nonliteral`` dynamic key-name list; parse
    failures land in ``_parse_error``. Never raises.
    """
    facts: dict = {}
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return {"_parse_error": str(exc)[:120]}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not (isinstance(tgt, ast.Name) and tgt.id in _CONF_WHITELIST):
                continue
            try:
                facts[tgt.id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError, TypeError):
                facts.setdefault("_nonliteral", []).append(tgt.id)
    return facts


def _glossary_terms(text: str) -> list[str]:
    """Term definition lines inside glossary directive bodies (lowercase whole word, approximate parsing).

    A term line = a non-empty line at the directive body's minimum indent level
    (definition bodies indent deeper); option lines / comment lines /
    subsequent directive lines are not collected.
    """
    terms: list[str] = []
    for m in _GLOSSARY_RE.finditer(text):
        seg = text[m.end():].splitlines()
        term_indent: int | None = None
        for line in seg:
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            if indent == 0 and stripped.startswith(".."):
                break  # next top-level directive: glossary body ends
            if term_indent is None:
                term_indent = indent  # first content line defines the term level
            if indent == term_indent and not stripped.startswith(
                    ("..", ":", "~")):
                terms.append(stripped.split("  ")[0].strip().lower())
    return terms


def collect_rst_project_facts(project_root, files: dict, metadata: dict) -> None:
    """Project-mode aggregate main entry (evidence-only; never raises, never rewrites text).

    ``files``: ``{relpath: text}`` (relpath relative to project_root,
    posix style). Produces three metadata keys:
    ``rst_project_mode_v1`` / ``sphinx_conf_facts_v1`` / ``rst_glossary_v1``.
    Scan failures silently degrade to ``rst_project_error``.
    """
    try:
        root = Path(project_root)
        rels_lower = {str(k).replace("\\", "/").lower() for k in files}
        labels: set[str] = set()
        glossary_terms: set[str] = set()
        for rel, text in files.items():
            labels.update(_LABEL_DEF_RE.findall(text))
            glossary_terms.update(_glossary_terms(text))

        ref_total = ref_resolved = 0
        doc_total = doc_resolved = 0
        toc_total = toc_resolved = 0
        inc_total = inc_resolved = 0
        term_total = term_resolved = 0
        for rel, text in files.items():
            rel = str(rel).replace("\\", "/")
            base = posixpath.dirname(rel)
            for ref in _REF_RE.findall(text):
                ref_total += 1
                if ref in labels:
                    ref_resolved += 1
            for doc in _DOC_RE.findall(text):
                doc_total += 1
                cand = posixpath.normpath(
                    posixpath.join(base, doc)).lstrip("/")
                if any((cand + s).lower() in rels_lower
                       for s in _DOC_SUFFIXES):
                    doc_resolved += 1
            for term in _TERM_RE.findall(text):
                term_total += 1
                if term.strip().lower() in glossary_terms:
                    term_resolved += 1
            toc_hits_total, toc_res_hits = _toctree_resolution(
                text, base, rels_lower)
            toc_total += toc_hits_total
            toc_resolved += toc_res_hits
            for target in _INCLUDE_TARGET_RE.findall(text):
                if target.startswith(("http://", "https://", "/")):
                    continue
                inc_total += 1
                if _root_walk_hit(root, base, target):
                    inc_resolved += 1

        mode = {
            "extractor": "rst_project_mode_v1",
            "project_name": root.name if str(project_root) else "",
            "root": str(project_root),
            "file_count": len(files),
            "label_set_size": len(labels),
            "ref": {"references": ref_total, "resolved": ref_resolved,
                    "not_in_corpus": ref_total - ref_resolved},
            "doc": {"references": doc_total, "resolved": doc_resolved,
                    "not_in_corpus": doc_total - doc_resolved},
            "toctree": {"entries": toc_total, "resolved": toc_resolved,
                        "not_in_corpus": toc_total - toc_resolved},
            "include_root_walk": {
                "targets": inc_total, "resolved": inc_resolved},
        }
        conf_path = root / "conf.py"
        conf_facts = (parse_sphinx_conf(conf_path)
                      if conf_path.is_file() else {})
        if isinstance(conf_facts.get("project"), str):
            mode["project_name"] = conf_facts["project"]
        metadata["rst_project_mode_v1"] = mode
        metadata["sphinx_conf_facts_v1"] = conf_facts
        metadata["rst_glossary_v1"] = {
            "glossary_terms_defined": len(glossary_terms),
            "term_references": term_total,
            "term_resolved": term_resolved,
            "term_not_in_glossary": term_total - term_resolved,
            "note": "approximate term-line extraction (min-indent rule); "
                    "multi-word terms matched lowercase whole-line",
        }
    except Exception as exc:  # pragma: no cover - last-resort silent degradation
        metadata["rst_project_error"] = str(exc)[:200]


def _toctree_resolution(text: str, base: str,
                        rels_lower: set[str]) -> tuple[int, int]:
    """toctree entries → project-file resolution counts (replicating the P2 corpus batch-scan approximation)."""
    total = resolved = 0
    lines = text.splitlines()
    i = 0
    optline = re.compile(r"^\s*:\w")
    entry = re.compile(r"^\s+(\S.*)$")
    while i < len(lines):
        if re.match(r"^\s*\.\.\s+toctree::", lines[i]):
            i += 1
            while i < len(lines) and (not lines[i].strip()
                                      or optline.match(lines[i])):
                i += 1
            while i < len(lines):
                line = lines[i]
                if not line.strip():
                    i += 1
                    continue
                m = entry.match(line)
                if not m or line.lstrip().startswith(".."):
                    break
                item = m.group(1).split(" ")[0].strip("<>")
                if not item.startswith(("http", "/", ":")):
                    total += 1
                    cand = posixpath.normpath(
                        posixpath.join(base, item)).lower()
                    if any((cand + s) in rels_lower
                           for s in _DOC_SUFFIXES):
                        resolved += 1
                i += 1
        else:
            i += 1
    return total, resolved


def _root_walk_hit(root: Path, base: str, target: str) -> bool:
    """Disk reconciliation of include targets walked up the directory chain to the project root (D4 A semantics,
    probe S5 style: includes non-rst assets, disk existence = hit; a ``../``
    prefix is a file-relative-parent notation that may still resolve inside
    the root after normpath — true escapes are judged by cand inside the loop,
    D7 counts belong to the harvester layer)."""
    d = base
    for _ in range(_ROOT_WALK_MAX_DEPTH):
        cand = posixpath.normpath(posixpath.join(d, target))
        if not cand.startswith("../") and (root / cand).is_file():
            return True
        if not d:
            return False
        d = posixpath.dirname(d)
    return False
