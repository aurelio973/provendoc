"""Single source of truth for suffix -> representation class.

UniversalParser (the full pipeline), the SDK client
(representation construction for chunk/evaluate) and
StrategyOptimizer all look up this table instead of each keeping a
hand-maintained mapping — the previous three copies plus a fourth
if-chain inside the client were kept in step only by comments and
had measurably drifted (uneven cargo.lock / .envrc coverage,
inconsistent strategy passing for the docx and config families).
Adding a new format now means registering one entry in
REPRESENTATION_SPECS (the parser side still registers in
parser_registry, the domain membership in domain_registry — three
registries, each with its own job).

strategy passing contract ("capability-based passing"):

- The ``accepts_strategy`` flag records whether the representation
  class's constructor takes a ``strategy`` parameter.
  tests/test_representation_registry.py pins every entry with
  inspect.signature — a flag/signature mismatch fails the test —
  structural drift protection instead of comment convention.
- Consumers always pass their own policy/strategy to
  accepts_strategy=True representations and never to False ones
  (log/jsonl/code). No consumer keeps a private list of "which
  suffixes take strategy" anymore.

Dependency-availability semantics (consistent with
parser_registry and the old implementation):

- unknown suffix -> get_representation_class /
  create_representation return None (the caller decides the
  fallback: universal passthrough, client Txt fallback, optimizer
  Txt fallback);
- registered but with a missing optional dependency (e.g. pymupdf
  / the tree-sitter chain) -> ImportError raised, and the caller
  decides to skip or propagate.
"""

import importlib
import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepresentationSpec:
    """One suffix's representation registration entry.

    module/class_name use lazy import: a missing optional
    dependency affects only that suffix, never the whole registry.
    accepts_strategy follows the strategy passing contract in the
    module docstring; default_kwargs is reserved for constructor
    required parameters (no entry currently uses it — the .log
    grouping parameters are run-level config passed by
    UniversalParser via overrides, not a static fact).
    """

    module: str
    class_name: str
    accepts_strategy: bool = False
    note: str = ""


_NARRATIVE = "provendoc.core.representations.narrative"
_CONFIG = "provendoc.core.representations.config.config_representation"
_TABLE = "provendoc.core.representations.table"
_EVENT = "provendoc.core.representations.event"

_TXT = (f"{_NARRATIVE}.txt_narrative_representation", "TxtNarrativeRepresentation")
_MARKDOWN = (
    f"{_NARRATIVE}.markdown_narrative_representation",
    "MarkdownNarrativeRepresentation",
)

# Kept one-to-one with parser_registry.PARSER_SPECS suffixes (the
# count is not hand-written; tests/test_representation_registry.py
# reconciles it).
REPRESENTATION_SPECS = {
    # ── narrative ────────────────────────────────────────────
    ".txt": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".md": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    ".markdown": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    # markdown-family unification: eight extensions, one family,
    # one parser, one representation; the extension only affects
    # the syntax_profile evidence
    ".mdown": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    ".mkdn": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    ".mdwn": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    ".mdx": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    ".rmd": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    ".qmd": RepresentationSpec(*_MARKDOWN, accepts_strategy=True),
    # narrative containers
    ".pdf": RepresentationSpec(
        f"{_NARRATIVE}.pdf_narrative_representation",
        "PdfNarrativeRepresentation",
        accepts_strategy=True,
        note="惰性别名模块（PDFRepresentationRuntime 的公共名），"
        "pymupdf 缺失时 import 失败 → 调用方跳过/上抛",
    ),
    ".docx": RepresentationSpec(
        f"{_NARRATIVE}.docx_narrative_representation",
        "DocxNarrativeRepresentation",
        accepts_strategy=True,
        note="docx 容器感知表示；旧 universal 裸构造（不传 strategy）"
        "属漂移，本注册表按能力即传递统一（prereg B2）",
    ),
    # Word-family macro/template variants: the same
    # WordprocessingML schema as .docx — same chain, same
    # representation
    ".docm": RepresentationSpec(
        f"{_NARRATIVE}.docx_narrative_representation",
        "DocxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".dotx": RepresentationSpec(
        f"{_NARRATIVE}.docx_narrative_representation",
        "DocxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".dotm": RepresentationSpec(
        f"{_NARRATIVE}.docx_narrative_representation",
        "DocxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    # legacy Word/PowerPoint binaries and the RTF family: the
    # parser emits plain text with no container structure — all
    # routed through the Txt channel
    ".doc": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".dot": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".rtf": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".rtx": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".rtfd": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".ppt": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".pps": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".pot": RepresentationSpec(*_TXT, accepts_strategy=True),
    # .tex math-transcription representation: source verbatim ->
    # Unicode-transcribed narrative text, split from the Txt
    # channel
    ".tex": RepresentationSpec(
        f"{_NARRATIVE}.tex_narrative_representation",
        "TexNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".rst": RepresentationSpec(
        *_TXT,
        accepts_strategy=True,
        note="reStructuredText 无容器语义，复用 Txt 叙事表示",
    ),
    ".pptx": RepresentationSpec(
        f"{_NARRATIVE}.pptx_narrative_representation",
        "PptxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    # PowerPoint-family OpenXML five variants share one chain
    ".pptm": RepresentationSpec(
        f"{_NARRATIVE}.pptx_narrative_representation",
        "PptxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".ppsx": RepresentationSpec(
        f"{_NARRATIVE}.pptx_narrative_representation",
        "PptxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".ppsm": RepresentationSpec(
        f"{_NARRATIVE}.pptx_narrative_representation",
        "PptxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".potx": RepresentationSpec(
        f"{_NARRATIVE}.pptx_narrative_representation",
        "PptxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".potm": RepresentationSpec(
        f"{_NARRATIVE}.pptx_narrative_representation",
        "PptxNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".epub": RepresentationSpec(
        f"{_NARRATIVE}.epub_narrative_representation",
        "EpubNarrativeRepresentation",
        accepts_strategy=True,
    ),
    # ODP adjacent route + family deepening: four suffixes, one
    # chain
    ".odp": RepresentationSpec(
        f"{_NARRATIVE}.odp_narrative_representation",
        "OdpNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".fodp": RepresentationSpec(
        f"{_NARRATIVE}.odp_narrative_representation",
        "OdpNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".otp": RepresentationSpec(
        f"{_NARRATIVE}.odp_narrative_representation",
        "OdpNarrativeRepresentation",
        accepts_strategy=True,
    ),
    ".sti": RepresentationSpec(
        f"{_NARRATIVE}.odp_narrative_representation",
        "OdpNarrativeRepresentation",
        accepts_strategy=True,
    ),
    # image inputs: the OCR success exit emits text; the three
    # failure exits are permanently empty body — no container
    # semantics, routed through the Txt channel
    ".png": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".jpg": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".jpeg": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".gif": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".bmp": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".tif": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".tiff": RepresentationSpec(*_TXT, accepts_strategy=True),
    ".webp": RepresentationSpec(*_TXT, accepts_strategy=True),
    # ── structured_config ────────────────────────────────────
    # all subclasses inherit ConfigRepresentation.__init__
    # (strategy=None first parameter)
    ".toml": RepresentationSpec(_CONFIG, "TomlConfigRepresentation", True),
    # Cargo.lock full-name canonical routing (keys start without a
    # dot, same key shape as parser_registry); the legacy
    # universal missing this mapping (passthrough) was drift, fixed
    # here
    "cargo.lock": RepresentationSpec(_CONFIG, "TomlConfigRepresentation", True),
    ".yaml": RepresentationSpec(_CONFIG, "YamlConfigRepresentation", True),
    ".yml": RepresentationSpec(_CONFIG, "YamlConfigRepresentation", True),
    ".json": RepresentationSpec(_CONFIG, "JsonConfigRepresentation", True),
    ".jsonc": RepresentationSpec(_CONFIG, "JsonConfigRepresentation", True),
    # .json5 -> a standalone Json5ConfigRepresentation (fmt="json5",
    # three formats kept distinct)
    ".json5": RepresentationSpec(
        _CONFIG, "Json5ConfigRepresentation", True,
    ),
    # eslintrc/babelrc dotfile family, eight suffixes -> one
    # DotfileConfigRepresentation (fmt="dotfile_json_yaml" — the
    # representation-layer identity follows the parsing-layer route;
    # the content format is disclosed by dotfile_identity, not by
    # the Representation fmt)
    ".eslintrc": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".eslintrc.json": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".eslintrc.yml": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".eslintrc.yaml": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".babelrc": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".babelrc.json": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".babelrc.yml": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".babelrc.yaml": RepresentationSpec(
        _CONFIG, "DotfileConfigRepresentation", True,
    ),
    ".xml": RepresentationSpec(_CONFIG, "XmlConfigRepresentation", True),
    ".ini": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    # INI dialects: the real .cfg suffix + six extensionless
    # dotfiles' canonical suffixes
    ".cfg": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    ".gitconfig": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    ".gitmodules": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    ".editorconfig": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    ".npmrc": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    ".coveragerc": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    ".pylintrc": RepresentationSpec(_CONFIG, "IniConfigRepresentation", True),
    ".properties": RepresentationSpec(
        _CONFIG, "PropertiesConfigRepresentation", True
    ),
    ".env": RepresentationSpec(_CONFIG, "EnvConfigRepresentation", True),
    # direnv .envrc; the legacy client missing this mapping (Txt
    # fallback) was drift, fixed here
    ".envrc": RepresentationSpec(_CONFIG, "EnvrcConfigRepresentation", True),
    ".plist": RepresentationSpec(_CONFIG, "PlistConfigRepresentation", True),
    # .tf/.tfvars migrated to the config domain; the generic .hcl
    # syntax stays in the code domain (the code-family channel
    # below)
    ".tf": RepresentationSpec(_CONFIG, "HclConfigRepresentation", True),
    ".tfvars": RepresentationSpec(_CONFIG, "HclConfigRepresentation", True),
    # compound suffixes
    ".tf.json": RepresentationSpec(_CONFIG, "HclJsonConfigRepresentation", True),
    ".tfvars.json": RepresentationSpec(
        _CONFIG, "HclJsonConfigRepresentation", True
    ),
    ".tftest.hcl": RepresentationSpec(_CONFIG, "HclConfigRepresentation", True),
    ".tftest.json": RepresentationSpec(
        _CONFIG, "HclJsonConfigRepresentation", True
    ),
    # content-gated generic formats (conf/cnf/dotconfig/systemd)
    ".conf": RepresentationSpec(_CONFIG, "ConfConfigRepresentation", True),
    ".cnf": RepresentationSpec(_CONFIG, "CnfConfigRepresentation", True),
    ".config": RepresentationSpec(_CONFIG, "DotConfigConfigRepresentation", True),
    ".service": RepresentationSpec(_CONFIG, "SystemdConfigRepresentation", True),
    ".timer": RepresentationSpec(_CONFIG, "SystemdConfigRepresentation", True),
    # ── spreadsheet ──────────────────────────────────────────
    ".csv": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".tsv": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".psv": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".xlsx": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".xls": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".xlsm": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".xlsb": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".ods": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    ".parquet": RepresentationSpec(_TABLE, "SpreadsheetRepresentation", True),
    # ── record_stream ────────────────────────────────────────
    # event-grouping representations do not take strategy (the
    # grouping knobs are a separate parameter face); the .log
    # run-level grouping parameters come from UniversalParser via
    # create_representation overrides
    ".log": RepresentationSpec(
        f"{_EVENT}.log_event_representation", "LogEventRepresentation"
    ),
    ".jsonl": RepresentationSpec(
        f"{_EVENT}.jsonl_event_representation", "JsonlEventRepresentation"
    ),
    ".ndjson": RepresentationSpec(
        f"{_EVENT}.jsonl_event_representation", "JsonlEventRepresentation"
    ),
}

# The code domain's single family spec: all code suffixes share
# CodeRepresentation (AST/component structural facts -> locatable
# retrieval units; takes no strategy). The single source of truth
# for the suffix list is language_map.CODE_SUFFIXES (shared with
# the parser side), loaded lazily — the code package __init__
# depends on tree-sitter (optional), and a module-level import
# would sink the whole pipeline when it is missing.
CODE_FAMILY_SPEC = RepresentationSpec(
    "provendoc.core.representations.code",
    "CodeRepresentation",
    note="code 域统一表示；后缀名单惰性取自 language_map.CODE_SUFFIXES",
)


def registered_suffixes():
    """All statically registered suffixes (code family excluded; the count is pinned by tests)."""
    return tuple(REPRESENTATION_SPECS)


def code_family_suffixes():
    """The code-domain suffix list (language_map.CODE_SUFFIXES, lazily loaded).

    Raises ImportError when optional dependencies like
    tree-sitter are missing (same semantics as the legacy
    universal's local import); the caller decides to skip or
    propagate.
    """
    from provendoc.core.parsers.code.language_map import (
        CODE_SUFFIXES,
    )

    return CODE_SUFFIXES


def _spec_of(suffix):
    """Static table first; then the code family; None when neither hits.

    A failed code-family list load (missing optional dependency)
    counts as a miss — same semantics as the legacy universal: the
    code representation is wholly absent when its dependency is
    missing, and other suffix lookups are never blocked.
    """
    spec = REPRESENTATION_SPECS.get(suffix)
    if spec is not None:
        return spec
    try:
        if suffix in code_family_suffixes():
            return CODE_FAMILY_SPEC
    except ImportError:
        pass
    return None


def create_code_family_representation():
    """Instantiate the shared code-domain representation (singleton semantics: one instance for all code suffixes).

    CodeRepresentation has no instance state, and UniversalParser
    shares one instance across all suffixes; it is constructed
    here so callers need not borrow a suffix to look it up via
    code_family_suffixes(). Raises ImportError when optional
    dependencies like tree-sitter are missing (same semantics as
    the legacy universal's local import); the caller decides to
    skip or propagate.
    """
    module = importlib.import_module(CODE_FAMILY_SPEC.module)
    return getattr(module, CODE_FAMILY_SPEC.class_name)()


def get_representation_class(suffix):
    """Return the representation class for a suffix.

    None for unknown suffixes; ImportError for registered entries
    whose optional dependency is missing (the caller decides to
    skip or propagate).
    """
    spec = _spec_of(suffix)
    if spec is None:
        return None
    module = importlib.import_module(spec.module)
    return getattr(module, spec.class_name)


def create_representation(suffix, strategy=None, **overrides):
    """Instantiate the representation for a suffix.

    strategy passing contract (capability-based): ``strategy=``
    is injected only when spec.accepts_strategy and strategy is
    not None; overrides (e.g. the .log run-level grouping
    parameters) pass through as-is. None for unknown suffixes;
    ImportError when the optional dependency is missing.
    """
    spec = _spec_of(suffix)
    if spec is None:
        return None
    kwargs = dict(overrides)
    if spec.accepts_strategy and strategy is not None:
        kwargs["strategy"] = strategy
    return get_representation_class(suffix)(**kwargs)


def representation_factory(suffix):
    """Return a unified-signature factory ``factory(strategy=None)`` -> representation instance.

    StrategyOptimizer's search/evaluation chain calls
    ``repr_cls(strategy=...)``; classes that take no strategy
    (log/jsonl/code) become callable with the same call shape
    through this factory adapter (the strategy argument is
    ignored). None for unknown suffixes; ImportError when the
    optional dependency is missing.
    """
    spec = _spec_of(suffix)
    if spec is None:
        return None
    cls = get_representation_class(suffix)
    if spec.accepts_strategy:
        return cls

    def factory(strategy=None):
        return cls()

    factory.__name__ = spec.class_name
    factory.__qualname__ = spec.class_name
    factory.__wrapped__ = cls
    return factory
