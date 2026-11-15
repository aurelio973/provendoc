"""Single source of truth for suffix → parser class mapping.

UniversalParser (full pipeline) and the SDK client (parse-only) both
look up this table instead of each maintaining its own copy — adding
a format requires only:

  1. register the domain in domain_registry.py
  2. register a PARSER_SPECS entry here (with construction notes)

Special cases are declared centrally, not duplicated in two places:

- ``.xlsb``: default_kwargs injects engine="pyxlsb" (openpyxl cannot
  read binary xlsb);
- ``.xls``: default_kwargs injects engine="xlrd" (openpyxl likewise
  cannot read legacy binary xls);
- ``.doc``/``.dot``: routed to DocCfbParser — [MS-DOCMD] CFB
  piece table main-text extraction (olefile); non-CFB / truncated /
  encrypted / Word6-95 / corrupted each degrade via honest failure,
  never guessing text heuristically from the binary;
- the literal name ".env" is a hidden file with no suffix
  (Path.suffix is the empty string); name-based normalization
  happens at the caller entry points (UniversalParser.parse /
  sdk._suffix_of), not in this registry. INI-dialect dotfiles
  (.gitconfig etc.) likewise — see DOTFILE_CANONICAL_NAMES below.

Dependency availability semantics (unchanged from the old
implementation):

- unknown suffix → get_parser_class / create_parser return None;
- registered but optional dependency missing (e.g.
  tree-sitter-language-pack) → raises ImportError; the caller
  decides the degradation strategy: UniversalParser skips the
  suffix (parse reports unsupported), the SDK re-raises as-is.
"""

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

# Extensionless dotfiles → canonical suffix (the filename itself).
# Path(".env").suffix is the empty string; caller entry points
# (UniversalParser.parse / sdk._suffix_of) normalize by full
# filename before the table lookup. Only key-value-syntax names are
# listed; line-list formats (.gitignore) and drifters (.yarnrc) are
# honestly excluded. "cargo.lock" is not a dotfile (Path.suffix is
# ".lock", non-empty) but is included as a TOML-family full-name
# canonical routing exception — caller entry points must match this
# list by full filename **before** the suffix check. ".envrc"
# (direnv shell dialect) routes to a dedicated EnvrcParser
# (identify-only: key-value lines + directive lines registered,
# zero execution zero extra IO); not pure key-value syntax, but the
# routing is self-justifying.
DOTFILE_CANONICAL_NAMES = frozenset({
    ".env",
    ".envrc",
    ".gitconfig",
    ".gitmodules",
    ".editorconfig",
    ".npmrc",
    ".coveragerc",
    ".pylintrc",
    "cargo.lock",
    # Extensionless .eslintrc/.babelrc (content may be
    # JSON/JSONC/YAML — content-detection routing, not a fixed
    # grammar); extensioned variants (.eslintrc.json etc.) go
    # through COMPOUND_SUFFIX_CANONICALS
    ".eslintrc",
    ".babelrc",
})

# Extensionless bare filenames (non-dotfiles) → canonical suffix.
# Dockerfile/Makefile are the most common suffix-less code files in
# repos; variant names (Dockerfile.dev etc., where Path.suffix is
# non-empty) are not listed. CMakeLists.txt carries a .txt suffix;
# its filename-priority routing is a separate mechanism, honestly
# not covered here.
BARE_NAME_CANONICAL_SUFFIXES = {
    "dockerfile": ".dockerfile",
    "makefile": ".makefile",
    "gnumakefile": ".makefile",
}

# dotenv family variant-name prefix: the Path.suffix of
# .env.local / .env.production / .env.example is
# ".local"/".production"/".example" (non-empty and unregistered,
# falling to the TxtParser fallback). Names starting with ".env."
# (dot-separated) normalize to canonical ".env" — dotenv syntax
# is isomorphic, so example/template files parse as the family.
# ".envrc" has no dot separation (startswith(".env.") is False)
# and is unaffected: it is a direnv shell script, not parsed as
# dotenv — it matches DOTFILE_CANONICAL_NAMES by full name and
# routes to the dedicated EnvrcParser (identify-only).
ENV_VARIANT_NAME_PREFIX = ".env."


# Compound-suffix canonical list: HCL JSON variants and tftest
# files have Path.suffix of just ".json"/".hcl", so single-suffix
# routing would drop them into the generic JsonParser (losing
# Terraform semantics — a red line) or the code-domain .hcl thin
# shell (wrong domain). Normalization rule: filename (lowercased)
# ending in a listed compound suffix → that compound suffix is the
# canonical suffix, returned **before** the generic single suffix
# (same entry-point pattern as the Cargo.lock full-name match and
# the .env. prefix normalization; both caller entry points share
# this single source of truth). Plain .json/.hcl/.tf/.tfvars
# routing is unchanged (pinned by tests).
COMPOUND_SUFFIX_CANONICALS = (
    ".tf.json",
    ".tfvars.json",
    ".tftest.hcl",
    ".tftest.json",
    # eslintrc/babelrc extensioned variants (Path.suffix is only
    # .json/.yml/.yaml — direct single-suffix routing would land in
    # the generic Json/Yaml parser, losing dotfile identity and tool
    # Profile semantics; compound-suffix normalization precedes the
    # generic single suffix, same as the .tf.json precedent). These
    # must go through the compound list rather than being folded
    # into DOTFILE_CANONICAL_NAMES (which would let ".eslintrc"
    # absorb its own extensioned variants).
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".babelrc.json",
    ".babelrc.yml",
    ".babelrc.yaml",
)


def compound_suffix_of(name: str):
    """Return the canonical compound suffix if the (lowercased) filename ends with one.

    Otherwise None. Matches in descending length order (prevents
    prefix shadowing, e.g. hypothetical nested ``.a.b.c`` shapes);
    the current entries have no suffix overlap, the defensive sort
    is only for robustness in future list extensions.
    """
    for suffix in sorted(
        COMPOUND_SUFFIX_CANONICALS, key=len, reverse=True,
    ):
        if name.endswith(suffix):
            return suffix
    return None


def is_env_variant_name(name: str) -> bool:
    """True for ``.env.<variant>`` dotenv-family names (not ``.envrc``).

    Caller entry points (UniversalParser.parse / sdk._suffix_of)
    normalize to canonical ``.env`` after matching the lowercased
    filename, then look up the table.
    """
    return name.startswith(ENV_VARIANT_NAME_PREFIX)


# systemd drop-in parent-directory gating. The Path.suffix of
# `example.service.d/override.conf` is ".conf" (dropping into
# ConfParser and losing systemd semantics); it must be normalized
# by parent-directory name to canonical ".service"/".timer" before
# the table lookup — same entry-point pattern as compound_suffix_of
# and the DOTFILE full-name match; both caller entry points share
# this single source of truth.
_DROP_IN_UNIT_DIRS = (".service.d", ".timer.d")


def drop_in_suffix_of(file_path):
    """systemd drop-in parent-directory normalization: canonical suffix or None.

    File suffix is ``.conf`` and the parent directory name
    (case-insensitive) ends with ``.service.d``/``.timer.d`` →
    returns ``.service``/``.timer``. Files inside ``.d`` dirs with
    a non-``.conf`` suffix are not gated (systemd ignores them
    too); other unit-type ``.d`` directories (.socket.d etc.) are
    not gated — future unit batches. The parser side builds
    drop-in semantics from the parent-directory path evidence.
    """
    p = Path(file_path)
    if p.suffix.lower() != ".conf":
        return None
    parent = p.parent.name.lower()
    for unit_dir in _DROP_IN_UNIT_DIRS:
        if parent.endswith(unit_dir):
            return unit_dir[:-2]  # ".service" / ".timer"
    return None


@dataclass(frozen=True)
class ParserSpec:
    """Registration entry for one suffix.

    module/class_name use lazy import: a missing optional
    dependency affects only its own suffix, never the whole
    registry. default_kwargs are the parameters required to
    construct the parser (e.g. the .xlsb engine injection),
    kept separate from runtime configuration passed by the caller
    (limits/ocr/image switches).
    """

    module: str
    class_name: str
    default_kwargs: dict = field(default_factory=dict)
    note: str = ""


_NARRATIVE = "provendoc.core.parsers.narrative"
_CONFIG = "provendoc.core.parsers.config"
_EVENT = "provendoc.core.parsers.event"
_TABLE = "provendoc.core.parsers.table"
_CODE = "provendoc.core.parsers.code"

# Kept one-to-one with domain_registry.py suffixes (count is not
# hand-written; tests/test_domain_invariants.py reconciles it).
PARSER_SPECS = {
    # ── narrative ────────────────────────────────────────────
    ".txt": ParserSpec(f"{_NARRATIVE}.txt_parser", "TxtParser"),
    ".md": ParserSpec(f"{_NARRATIVE}.markdown_parser", "MarkdownParser"),
    ".markdown": ParserSpec(f"{_NARRATIVE}.markdown_parser", "MarkdownParser"),
    # markdown-family unification: one family, one parser; the
    # extension only affects the syntax_profile evidence
    ".mdown": ParserSpec(
        f"{_NARRATIVE}.markdown_parser", "MarkdownParser",
        note="markdown-family 普通变体扩展名",
    ),
    ".mkdn": ParserSpec(
        f"{_NARRATIVE}.markdown_parser", "MarkdownParser",
        note="markdown-family 普通变体扩展名",
    ),
    ".mdwn": ParserSpec(
        f"{_NARRATIVE}.markdown_parser", "MarkdownParser",
        note="markdown-family 普通变体扩展名",
    ),
    ".mdx": ParserSpec(
        f"{_NARRATIVE}.markdown_parser", "MarkdownParser",
        note="markdown-family；MDX 含 JSX——只登记不执行",
    ),
    ".rmd": ParserSpec(
        f"{_NARRATIVE}.markdown_parser", "MarkdownParser",
        note="markdown-family；R Markdown——chunk 语法只按围栏登记",
    ),
    ".qmd": ParserSpec(
        f"{_NARRATIVE}.markdown_parser", "MarkdownParser",
        note="markdown-family；Quarto——execution cell 只按围栏登记",
    ),
    # narrative containers
    ".pdf": ParserSpec(f"{_NARRATIVE}.pdf.pdf_parser", "PdfParser"),
    ".docx": ParserSpec(f"{_NARRATIVE}.docx_parser", "DocxParser"),
    ".doc": ParserSpec(
        f"{_NARRATIVE}.doc_cfb_parser", "DocCfbParser",
        note="legacy Word 二进制（OLE/CFB）：Word97+ piece table "
        "提取主文域正文（[MS-DOCMD] 结构定位，绝不猜文本）；"
        "Word 6/95/加密/结构损坏各档 honest failure（结构事实仍登记），"
        "prereg docx_word_cfb_prereg_v1",
    ),
    # Word-family macro/template variants: same WordprocessingML
    # schema as .docx, same extraction chain; macros are registered,
    # never executed (docx_security.macros)
    ".docm": ParserSpec(
        f"{_NARRATIVE}.docx_parser", "DocxParser",
        note="Word 宏文档（OOXML macroEnabled 变体，同 docx 链提取）",
    ),
    ".dot": ParserSpec(
        f"{_NARRATIVE}.doc_cfb_parser", "DocCfbParser",
        note="legacy Word 模板二进制，同 .doc 走 CFB piece table "
        "提取（prereg docx_word_cfb_prereg_v1）；同后缀的 "
        "Graphviz DOT 语言文本不是 CFB 字节——CFB 头校验诚实"
        "失败，不猜内容语言",
    ),
    ".dotx": ParserSpec(
        f"{_NARRATIVE}.docx_parser", "DocxParser",
        note="Word 模板（OOXML template 变体，同 docx 链提取）",
    ),
    ".dotm": ParserSpec(
        f"{_NARRATIVE}.docx_parser", "DocxParser",
        note="Word 宏模板（OOXML template+macroEnabled 变体，同 docx 链提取）",
    ),
    # narrative extensions (stdlib-only state machines / zipfile,
    # markdown-ified text output)
    ".tex": ParserSpec(f"{_NARRATIVE}.tex_parser", "TexParser"),
    ".rst": ParserSpec(
        f"{_NARRATIVE}.rst_parser", "RstParser",
        note="reStructuredText（Python/Sphinx 文档标准格式）；"
        "source-evidence 契约同 TexParser",
    ),
    ".rtf": ParserSpec(
        f"{_NARRATIVE}.rtf_parser", "RtfParser",
        note="RTF 纯文本交换格式（Word-family）；纯标准库状态机"
        "提取渲染正文（控制字不进正文）；结构证据落 rtf_structure，"
        "源码存证走 source_text",
    ),
    # .rtx disguised suffix: content-sniffing routing — files whose
    # head matches {\rtf parse as RTF, others parse as text with
    # format_mismatch registered (RtxParser delegates).
    ".rtx": ParserSpec(
        f"{_NARRATIVE}.rtf_parser", "RtxParser",
        note="RTF 伪装后缀：内容嗅探路由（D3 A，prereg "
        "rtf_family_prereg_v1）——{\rtf 头命中按 RTF 解析，"
        "未命中按 text 处理并登记 format_mismatch",
    ),
    # .rtfd directory bundle: the TXT.rtf main document goes through
    # the full RtfParser channel; other files are reconciled by
    # sha256 ↔ asset_id as attachment/orphan (bundle_noise
    # registered).
    ".rtfd": ParserSpec(
        f"{_NARRATIVE}.rtf_parser", "RtfdParser",
        note="RTF 目录包（NeXT/TextEdit bundle）：TXT.rtf 主文档"
        "走 RTF 全量通道；附件 sha256 与主文档图片资产对账，"
        "未引用文件登记 orphan（D19 A）",
    ),
    ".pptx": ParserSpec(f"{_NARRATIVE}.pptx_parser", "PptxParser"),
    # PowerPoint-family extension: five OpenXML variants —
    # PresentationML part structure is fully isomorphic to .pptx
    # (main-chain filename regex + declaration-order relations, no
    # content-type guard), same extraction chain; macros
    # (pptm/ppsm/potm) go through the existing pptx_security.macros
    # registered-only path. Slide-show/template identity is carried
    # by the pptx_profile evidence key.
    ".pptm": ParserSpec(
        f"{_NARRATIVE}.pptx_parser", "PptxParser",
        note="PowerPoint macroEnabled 变体（PresentationML 同链提取；"
             "宏只登记不执行）",
    ),
    ".ppsx": ParserSpec(
        f"{_NARRATIVE}.pptx_parser", "PptxParser",
        note="PowerPoint 放映模式变体（结构同 .pptx；"
             "presentation_mode=slide_show 登记）",
    ),
    ".ppsm": ParserSpec(
        f"{_NARRATIVE}.pptx_parser", "PptxParser",
        note="PowerPoint 放映+macroEnabled 变体（同链提取；"
             "宏只登记不执行）",
    ),
    ".potx": ParserSpec(
        f"{_NARRATIVE}.pptx_parser", "PptxParser",
        note="PowerPoint 模板变体（PresentationML 同链提取；"
             "is_template 登记）",
    ),
    ".potm": ParserSpec(
        f"{_NARRATIVE}.pptx_parser", "PptxParser",
        note="PowerPoint 模板+macroEnabled 变体（同链提取；"
             "宏只登记不执行）",
    ),
    # legacy CFB three-suffix — text extraction via [MS-PPT] record
    # walk (Current User → UEA chain → persist directory →
    # DocumentContainer → SLWT0 inline text; TextChars UTF-16 /
    # TextBytes cp1252+high_byte_count disclosure). The container
    # evidence tier upgrades to a superset of structural facts; the
    # key failure phrase ("no PowerPoint Document stream") is kept
    # for compatibility.
    ".ppt": ParserSpec(
        f"{_NARRATIVE}.ppt_cfb_parser", "PptCfbParser",
        note="legacy PowerPoint 二进制（OLE/CFB）：[MS-PPT] "
             "record walk 提取 SlideListWithText 幻灯片正文"
             "（结构定位，绝不猜文本）；无流/PPT95 token/"
             "结构损坏/空文本各档 honest failure"
             "（已达结构事实仍登记），prereg ppt_cfb_text_prereg_v1",
    ),
    ".pps": ParserSpec(
        f"{_NARRATIVE}.ppt_cfb_parser", "PptCfbParser",
        note="legacy PowerPoint 放映文件（CFB 容器；[MS-PPT] "
             "同链提取，file_type=pps 登记），prereg "
             "ppt_cfb_text_prereg_v1",
    ),
    ".pot": ParserSpec(
        f"{_NARRATIVE}.ppt_cfb_parser", "PptCfbParser",
        note="legacy PowerPoint 模板二进制（CFB 容器；[MS-PPT] "
             "同链提取）；gettext 同后缀歧义由 CFB 头校验"
             "诚实失败自证（同 .dot×Graphviz 先例），prereg "
             "ppt_cfb_text_prereg_v1",
    ),
    ".epub": ParserSpec(f"{_NARRATIVE}.epub_parser", "EpubParser"),
    # ODP: OpenDocument Presentation — a presentation-family parser
    # of its own (not folded into PPT/PPTX parsers), stdlib-only
    # zipfile + ElementTree; slide-container spans + identity key
    # odp_profile.
    ".odp": ParserSpec(f"{_NARRATIVE}.odp_parser", "OdpParser"),
    # ODP-family: four suffixes, one chain — .fodp flat single-XML
    # file (OdpParser branches on is_zipfile), .otp/.sti template
    # zips on the same chain (is_template / legacy_suffix
    # registered in odp_profile).
    ".fodp": ParserSpec(
        f"{_NARRATIVE}.odp_parser", "OdpParser",
        note="Flat ODF XML 平文件 (单 XML, 非 zip 容器); OdpParser "
             "is_zipfile 分流共享 XML 走查, prereg "
             "odp_family_deepening_prereg_v1 D2",
    ),
    ".otp": ParserSpec(
        f"{_NARRATIVE}.odp_parser", "OdpParser",
        note="OpenDocument 演示模板 (zip 同链, is_template 变体), "
             "prereg odp_family_deepening_prereg_v1 D3",
    ),
    ".sti": ParserSpec(
        f"{_NARRATIVE}.odp_parser", "OdpParser",
        note="旧 StarOffice 演示模板扩展名 (zip 同链 + "
             "legacy_suffix 登记, 内容识别为准), prereg "
             "odp_family_deepening_prereg_v1 D3",
    ),
    # Image-input routing + four-outlet OCR: eight suffixes, one
    # parser. Magic-number self-verification → OCR four-outlet
    # routing (success yields OCR text + image_ocr channel facts;
    # failure registers ocr_unresolved); before this fix the SDK
    # fell back to TxtParser for these suffixes, parsing binaries
    # as text and idling the whole scenario chain.
    ".png": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十): 魔数自证 + OCR 四出口路由",
    ),
    ".jpg": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十), 同 .png 链",
    ),
    ".jpeg": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十), 同 .png 链",
    ),
    ".gif": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十), 同 .png 链",
    ),
    ".bmp": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十), 同 .png 链",
    ),
    ".tif": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十), 同 .png 链",
    ),
    ".tiff": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十), 同 .png 链",
    ),
    ".webp": ParserSpec(
        f"{_NARRATIVE}.image_parser", "ImageParser",
        note="图片输入 (#127→§二十), 同 .png 链; pymupdf 打不开"
             " webp → OCR_EXECUTION_FAILED 诚实登记",
    ),
    # ── structured_config ────────────────────────────────────
    ".toml": ParserSpec(f"{_CONFIG}.toml_parser", "TomlParser"),
    # cargo.lock full-name canonical routing (Path.suffix is
    # ".lock", non-empty; caller entry points normalize by full
    # filename); TomlParser identifies the cargo_lock profile from
    # the exact basename; the dependency-graph projection lands in
    # toml_lock metadata.
    "cargo.lock": ParserSpec(f"{_CONFIG}.toml_parser", "TomlParser"),
    ".yaml": ParserSpec(f"{_CONFIG}.yaml_parser", "YamlParser"),
    ".yml": ParserSpec(f"{_CONFIG}.yaml_parser", "YamlParser"),
    ".json": ParserSpec(f"{_CONFIG}.json_parser", "JsonParser"),
    # JSONC dialect (comments/trailing commas): JsonParser has a
    # built-in strict → jsonc tolerant path and honestly records
    # the dialect; the .jsonc suffix is explicitly registered,
    # .json files like tsconfig.json are covered via the tolerant
    # path.
    ".jsonc": ParserSpec(f"{_CONFIG}.json_parser", "JsonParser"),
    # .json5 → dedicated Json5Parser (three formats kept separate —
    # not "JSONC plus a loose switch").
    ".json5": ParserSpec(f"{_CONFIG}.json5_parser", "Json5Parser"),
    # eslintrc/babelrc dotfile family (identity + content detection
    # + hand-off to Json/Yaml parser reuse + zero-execution tool
    # Profile registration). The extensionless originals are matched
    # by DOTFILE_CANONICAL_NAMES full name; the extensioned
    # variants normalize via compound_suffix_of (both entry points
    # share this single source of truth).
    ".eslintrc": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".eslintrc.json": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".eslintrc.yml": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".eslintrc.yaml": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".babelrc": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".babelrc.json": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".babelrc.yml": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".babelrc.yaml": ParserSpec(
        f"{_CONFIG}.dotfile_json_yaml_parser",
        "DotfileJsonYamlParser",
    ),
    ".xml": ParserSpec(f"{_CONFIG}.xml_parser", "XmlParser"),
    ".ini": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    # INI dialect: .cfg is a real suffix (setup.cfg etc.); the other
    # six are extensionless dotfiles (.gitconfig etc.) whose
    # canonical suffix is the filename itself — normalization
    # happens at the caller entry points (see
    # DOTFILE_CANONICAL_NAMES). Honest exclusions: .yarnrc drifts
    # across versions, .gitignore/.dockerignore are line lists not
    # key-value — none registered. .conf used to be on this
    # exclusion list (to avoid nginx confusion); it now routes by
    # content identification — ConfParser first reads the raw text
    # (detected_syntax/profile/confidence), hands off to the
    # corresponding parser when it identifies an existing family
    # (JSON/YAML/TOML/INI/XML/HCL), otherwise falls to the generic
    # five-syntax recovery layer (the exclusion reason stands, the
    # solution was upgraded; tool Profile semantics deferred to P1).
    ".cfg": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    ".gitconfig": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    ".gitmodules": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    ".editorconfig": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    ".npmrc": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    ".coveragerc": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    ".pylintrc": ParserSpec(f"{_CONFIG}.ini_parser", "IniParser"),
    ".properties": ParserSpec(f"{_CONFIG}.properties_parser", "PropertiesParser"),
    ".env": ParserSpec(f"{_CONFIG}.env_parser", "EnvParser"),
    # direnv .envrc (extensionless dotfile, full-name canonical
    # routing like .gitconfig). EnvrcParser inherits EnvParser
    # value semantics; directive lines are identify-only
    # (execute_semantics=not-executed-disclosed, zero shell zero
    # IO).
    ".envrc": ParserSpec(
        f"{_CONFIG}.envrc_parser", "EnvrcParser",
        note="direnv shell 方言：键值行同 dotenv 语义；direnv "
             "指令行只登记不执行（identify-only）",
    ),
    # generic .conf content-gating parser (content identification
    # takes priority over extension semantics; existing families
    # handed off for reuse + generic five-syntax recovery; include
    # registered with zero IO; `.service`/`.rules`/`.list` routing
    # boundaries — not in this channel)
    ".conf": ParserSpec(
        f"{_CONFIG}.conf_parser", "ConfParser",
        note="内容识别路由：detected_syntax 判既有家族则移交；"
             "不确定 → generic_conf_candidate + profile_uncertain",
    ),
    # generic .cnf content-gating parser (a channel separate from
    # .conf; five-tier routing mysql/mariadb/openssl/
    # tool_specific/generic_ini_candidate; only machine-verifiable
    # cross-family evidence (JSON/XML/YAML) hands off; include
    # registered with zero IO; $variable/@section references
    # identify-only)
    ".cnf": ParserSpec(
        f"{_CONFIG}.cnf_parser", "CnfParser",
        note="内容识别路由：五档 Profile 候选；JSON/XML/YAML "
             "证据移交；不确定 → generic_ini_candidate + "
             "profile_uncertain",
    ),
    # generic .config content-gating parser (the extension family
    # is not a single grammar — "no second .config grammar is
    # invented"; XML via three evidence pieces (prolog/root
    # configuration/dotnet filename+angle) / JSON / YAML / INI /
    # TOML five-way hand-off reusing existing Core parsers;
    # uncertain → ConfParser generic recovery reuse +
    # UNKNOWN_CONFIG_SYNTAX; .NET Domain Profile semantics deferred
    # to P1).
    ".config": ParserSpec(
        f"{_CONFIG}.dotconfig_parser", "DotConfigParser",
        note="内容识别路由：XML 三证据或 JSON/YAML/INI/TOML "
             "证据移交既有 parser；不确定 → generic_config_candidate "
             "+ profile_uncertain + UNKNOWN_CONFIG_SYNTAX",
    ),
    # systemd Unit extension-gated channel (unit type is decided by
    # filename — the opposite direction from the .conf/.cnf content
    # gating; this channel covers .service/.timer). INI-like lexer +
    # systemd semantics registered (repeated multi-value appends /
    # empty value resets / Exec* argv statically parsed with zero
    # execution / specifiers identified but not expanded /
    # EnvironmentFile with zero IO); the remaining unit suffixes
    # (.socket/.mount/... and .rules/.list) stay unregistered
    # (later batches). IniParser last_wins semantics do not apply —
    # a dedicated parser, not a reuse.
    ".service": ParserSpec(
        f"{_CONFIG}.systemd_unit_parser", "SystemdUnitParser",
        note="systemd service unit：重复 directive 多值追加 + "
             "Exec* 静态解析（not-executed-disclosed）",
    ),
    ".timer": ParserSpec(
        f"{_CONFIG}.systemd_unit_parser", "SystemdUnitParser",
        note="systemd timer unit：时间字段登记（schedule_kind "
             "弱声明）+ timer_triggers 边 + inferred_target_"
             "candidate 未裁决",
    ),
    ".plist": ParserSpec(f"{_CONFIG}.plist_parser", "PlistParser"),
    # ── record_stream ────────────────────────────────────────
    ".log": ParserSpec(f"{_EVENT}.log_parser", "LogParser"),
    ".jsonl": ParserSpec(f"{_EVENT}.jsonl_parser", "JsonlParser"),
    ".ndjson": ParserSpec(f"{_EVENT}.jsonl_parser", "JsonlParser"),
    # ── spreadsheet ──────────────────────────────────────────
    ".csv": ParserSpec(f"{_TABLE}.csv_parser", "CsvParser"),
    ".tsv": ParserSpec(f"{_TABLE}.csv_parser", "CsvParser"),
    ".psv": ParserSpec(f"{_TABLE}.csv_parser", "CsvParser"),
".xlsx": ParserSpec(f"{_TABLE}.excel.excel_parser", "ExcelParser"),
".xls": ParserSpec(
    f"{_TABLE}.excel.excel_parser", "ExcelParser",
    default_kwargs={"engine": "xlrd"},
    note="旧版二进制 .xls 必须显式走 xlrd 后端（默认 openpyxl 不支持；"
    "table_backend_parity 实测 2026-10-22 前该路由缺陷使全部 .xls "
    "落入 openpyxl honest failure）",
),
".xlsm": ParserSpec(f"{_TABLE}.excel.excel_parser", "ExcelParser"),
    ".xlsb": ParserSpec(
        f"{_TABLE}.excel.excel_parser", "ExcelParser",
        default_kwargs={"engine": "pyxlsb"},
        note="二进制 xlsb 必须显式走 pyxlsb 后端（默认 openpyxl 不支持）",
    ),
    ".ods": ParserSpec(f"{_TABLE}.ods_parser", "OdsParser"),
    ".parquet": ParserSpec(f"{_TABLE}.parquet_parser", "ParquetParser"),
    # ── code (optional dependency tree-sitter-language-pack) ──
    ".py": ParserSpec(_CODE, "PythonCodeParser"),
    ".pyi": ParserSpec(_CODE, "PythonCodeParser"),
    ".js": ParserSpec(_CODE, "JavaScriptCodeParser"),
    ".mjs": ParserSpec(_CODE, "JavaScriptCodeParser"),
    ".cjs": ParserSpec(_CODE, "JavaScriptCodeParser"),
    ".jsx": ParserSpec(_CODE, "ReactJsxParser"),
    ".ts": ParserSpec(_CODE, "TypeScriptCodeParser"),
    ".tsx": ParserSpec(_CODE, "ReactTsxParser"),
    ".vue": ParserSpec(_CODE, "VueCodeParser"),
    ".html": ParserSpec(_CODE, "HtmlCodeParser"),
    ".htm": ParserSpec(_CODE, "HtmlCodeParser"),
    ".css": ParserSpec(_CODE, "CssCodeParser"),
    ".scss": ParserSpec(_CODE, "ScssCodeParser"),
    ".less": ParserSpec(_CODE, "LessCodeParser"),
    ".java": ParserSpec(_CODE, "JavaCodeParser"),
    ".go": ParserSpec(_CODE, "GoCodeParser"),
    ".php": ParserSpec(_CODE, "PhpCodeParser"),
    ".c": ParserSpec(_CODE, "CCodeParser"),
    ".h": ParserSpec(_CODE, "CCodeParser"),
    ".cpp": ParserSpec(_CODE, "CppCodeParser"),
    ".cc": ParserSpec(_CODE, "CppCodeParser"),
    ".cxx": ParserSpec(_CODE, "CppCodeParser"),
    ".hpp": ParserSpec(_CODE, "CppCodeParser"),
    ".cs": ParserSpec(_CODE, "CSharpCodeParser"),
    ".rs": ParserSpec(_CODE, "RustCodeParser"),
    ".rb": ParserSpec(_CODE, "RubyCodeParser"),
    ".swift": ParserSpec(_CODE, "SwiftCodeParser"),
    ".kt": ParserSpec(_CODE, "KotlinCodeParser"),
    ".scala": ParserSpec(_CODE, "ScalaCodeParser"),
    ".sh": ParserSpec(_CODE, "BashCodeParser"),
    ".bash": ParserSpec(_CODE, "BashCodeParser"),
    ".sql": ParserSpec(_CODE, "SqlCodeParser"),
    ".lua": ParserSpec(_CODE, "LuaCodeParser"),
    ".dart": ParserSpec(_CODE, "DartCodeParser"),
    ".r": ParserSpec(_CODE, "RCodeParser"),
    ".pl": ParserSpec(_CODE, "PerlCodeParser"),
    ".pm": ParserSpec(_CODE, "PerlCodeParser"),
    ".zig": ParserSpec(_CODE, "ZigCodeParser"),
    # Infrastructure-as-code (HCL grammar family): .hcl generic
    # grammar stays in the code domain as a tree-sitter thin shell
    # with zero drift; .tf/.tfvars moved to the config domain with
    # a hand-written HclParser (zero execution zero IO); profile
    # differences disclosed via metadata["hcl_profile"]
    ".hcl": ParserSpec(_CODE, "HclCodeParser"),
    ".tf": ParserSpec(
        f"{_CONFIG}.hcl_parser", "HclParser",
        note="Terraform HCL native（P0）：结构/引用/注释/重复检测，"
             "表达式结构恢复不求值（evidence-only）",
    ),
    ".tfvars": ParserSpec(
        f"{_CONFIG}.hcl_parser", "HclParser",
        note="Terraform 变量定义（HCL native 赋值语法）：顶层 block"
             " 记 INVALID_TFVARS_BLOCK 但结构保留（P0）",
    ),
    # HCL JSON variants and tftest files (compound suffixes routed
    # via compound_suffix_of normalization). JSON variants go to
    # HclJsonParser (the JSON→HCL isomorphic tree, the semantic layer
    # carries straight through); tftest native goes to HclParser,
    # tftest JSON to HclJsonParser, the profile carrying
    # test_configuration identity; apply inside tests is registered,
    # never executed.
    ".tf.json": ParserSpec(
        f"{_CONFIG}.hcl_json_parser", "HclJsonParser",
        note="Terraform JSON 配置（P2b）：HCL JSON 映射规则转同构树，"
             "block/labels/表达式结构不丢（规格 §12/§十八），"
             "语义层贯通零求值",
    ),
    ".tfvars.json": ParserSpec(
        f"{_CONFIG}.hcl_json_parser", "HclJsonParser",
        note="Terraform JSON 变量定义（P2b）：顶层键即变量赋值，"
             "表达式字符串原文保全",
    ),
    ".tftest.hcl": ParserSpec(
        f"{_CONFIG}.hcl_parser", "HclParser",
        note="Terraform 测试配置 native（P2b）：run/assert/variables"
             " 结构登记，file_role=test_configuration，apply 只登记"
             " 不执行（规格 §8）",
    ),
    ".tftest.json": ParserSpec(
        f"{_CONFIG}.hcl_json_parser", "HclJsonParser",
        note="Terraform 测试配置 JSON（P2b）：同 .tftest.hcl 口径，"
             "JSON 映射规则转同构树",
    ),
    # Windows scripts / build systems / interface definitions
    # (grammars provided by tree-sitter-language-pack;
    # .dockerfile/.makefile are also the canonical suffixes of the
    # bare filenames Dockerfile/Makefile)
    ".ps1": ParserSpec(_CODE, "PowerShellCodeParser"),
    ".psm1": ParserSpec(_CODE, "PowerShellCodeParser"),
    ".psd1": ParserSpec(_CODE, "PowerShellCodeParser"),
    ".dockerfile": ParserSpec(_CODE, "DockerfileCodeParser"),
    ".makefile": ParserSpec(_CODE, "MakefileCodeParser"),
    ".mk": ParserSpec(_CODE, "MakefileCodeParser"),
    ".mak": ParserSpec(_CODE, "MakefileCodeParser"),
    ".proto": ParserSpec(_CODE, "ProtoCodeParser"),
    ".cmake": ParserSpec(_CODE, "CMakeCodeParser"),
}


def supported_suffixes():
    """All registered suffixes (reconciled with domain_registry; count pinned by tests)."""
    return tuple(PARSER_SPECS)


def get_parser_class(suffix):
    """Return the parser class for a suffix.

    Unknown suffix → None; registered but optional dependency
    missing → ImportError raised (the caller decides to skip or
    re-raise).
    """
    spec = PARSER_SPECS.get(suffix)
    if spec is None:
        return None
    module = importlib.import_module(spec.module)
    return getattr(module, spec.class_name)


def create_parser(suffix, **overrides):
    """Instantiate the parser for a suffix.

    The spec's default_kwargs (e.g. the .xlsb engine injection)
    merge with overrides, overrides winning. Unknown suffix →
    None; optional dependency missing → ImportError.
    """
    spec = PARSER_SPECS.get(suffix)
    if spec is None:
        return None
    kwargs = {**spec.default_kwargs, **overrides}
    return get_parser_class(suffix)(**kwargs)
