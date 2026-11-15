# -*- coding: utf-8 -*-
"""Central registry of cross-format vocabularies.

Single source of truth for the shared vocabularies: evidence
sources, error code categories, location envelope fields, loss
reasons, and coverage dimensions. Kept honest by machine-checked
tests.

Design rules:

- Read-only registry: parser output keys stay untouched; this
  module is module-level constants only, no runtime state.
- Register, never rewrite: REASON_CODE_CATEGORY classifies codes
  without renaming or merging them.
- No forced mappings: entries with no valid semantics map to
  ``None`` rather than inventing coverage.
- Separate vocabularies: marker-contract codes and markdown
  node-tree reasons live in distinct registries.

Per-code mapping rationale lives in the inline comments;
category semantics are defined by ERROR_CATEGORIES.

Version tag: ``CROSS_FORMAT_VOCAB_VERSION``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

CROSS_FORMAT_VOCAB_VERSION = "cross_format_vocab_v1"

# ============================================================
# (a) Evidence source vocabulary -- eight values
# ============================================================

#: native=raw bytes; decoded=after decoding; recovered=after
#: structural repair; included=embedded resource; converted=
#: format conversion output; rendered=rendered surface; ocr=
#: optical character recognition; inferred=synthesized.
EVIDENCE_SOURCES: tuple[str, ...] = (
    "native",
    "decoded",
    "recovered",
    "included",
    "converted",
    "rendered",
    "ocr",
    "inferred",
)

# ============================================================
# (b) Error code category vocabulary -- twelve prefixes
# ============================================================

ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "CONTAINER",  # 容器/包结构层失败（部件缺失、内嵌对象未展开）
        "ENCODING",  # 字节级解码失败（截断的 UTF-8 序列等）
        "SYNTAX",  # 语法层解析失败（非法 JSON 行、XHTML 解析失败）
        "STRUCTURE",  # 结构完整性失败（未闭合围栏、损坏的书签区间）
        "RESOURCE",  # 预算/限额截停（行数上限、单元格预算、超限行）
        "REFERENCE",  # 引用解析失败（style id、spine idref、循环引用）
        "LAYOUT",  # 版面/几何层失败（预留类，当前无码归入）
        "SEMANTIC",  # 语义冲突（重复键遮蔽——同名键后者覆盖前者）
        "SECURITY",  # 安全立场拦截（预留类，当前无码归入）
        "PRIVACY",  # 隐私脱敏（预留类，当前无码归入）
        "RECOVERY",  # 降级/恢复路径登记（通道降级、子类型委托失败）
        "UNSUPPORTED",  # 不支持的类型/形态（spine 非 XHTML 项）
    }
)

# ============================================================
# (b2) marker-contract reason_code -> category map
# ============================================================

#: Full marker-contract registry; per-code rationale lives in
#: the inline comments below, and category semantics are defined
#: by ERROR_CATEGORIES.
REASON_CODE_CATEGORY: dict[str, str] = {
    # table / spreadsheet (8)
    "SHEET_KEPT_AS_SKELETON": "STRUCTURE",
    "CELL_EXTRACTION_SKIPPED_SHAPE_BUDGET": "RESOURCE",
    "ROWS_NOT_EXTRACTED": "RESOURCE",
    "COLUMNS_NOT_EXTRACTED": "RESOURCE",
    "RECORDS_BEYOND_ROW_CAP": "RESOURCE",
    "RECORD_COLUMNS_TRUNCATED": "RESOURCE",
    "RECORDS_BEYOND_CELL_BUDGET": "RESOURCE",
    "CELL_BUDGET_STOPPED": "RESOURCE",
    # structured_config (5)
    "DUPLICATE_KEY_SHADOWED": "SEMANTIC",
    "YAML_DOC_CHUNK_FAILED": "SYNTAX",
    "REFERENCE_UNRESOLVED": "REFERENCE",
    "REFERENCE_CYCLIC": "REFERENCE",
    "REFERENCE_EXTERNAL": "REFERENCE",
    # structured_config / hcl terraform profile (5):
    # semantic-layer block codes, disclosed via
    # terraform_profile["loss_codes"]; zero execution, zero IO;
    # unclosed template interpolation is SYNTAX, policy blocks
    # are UNSUPPORTED
    "FUNCTION_EVALUATION_BLOCKED": "UNSUPPORTED",
    "EXTERNAL_FILE_READ_BLOCKED": "UNSUPPORTED",
    "PROVISIONER_EXECUTION_BLOCKED": "UNSUPPORTED",
    "INVALID_TEMPLATE": "SYNTAX",
    "PARTIAL_EVALUATION_BLOCKED": "UNSUPPORTED",
    "INVALID_HCL_JSON": "SYNTAX",
    "INVALID_TF_JSON_ROOT": "SYNTAX",
    "HCL_JSON_BLOCK_SHAPE_MISMATCH": "SYNTAX",
    # structured_config / generic .conf: syntax errors go to
    # the contract channel; block codes go to conf_profile
    # loss_codes; UNKNOWN_CONF_PROFILE is an honest unknown
    "UNKNOWN_CONF_PROFILE": "SEMANTIC",
    # generic .conf gate: unrecognized syntax is honestly
    # reported, never guessed
    "UNKNOWN_CONFIG_SYNTAX": "SEMANTIC",
    # structured_config / .NET domain profile: external refs,
    # transforms and protected sections are zero-IO disclosure
    # codes in profile loss_codes; EXTERNAL_CONFIG_CYCLE is a
    # self-reference graph defect
    "EXTERNAL_CONFIG_NOT_READ": "UNSUPPORTED",
    "EXTERNAL_CONFIG_CYCLE": "SEMANTIC",
    "TRANSFORM_NOT_EXECUTED": "UNSUPPORTED",
    "PROTECTED_CONFIG_NOT_DECRYPTED": "UNSUPPORTED",
    "INVALID_ENCODING": "ENCODING",
    "INVALID_DIRECTIVE": "SYNTAX",
    "MISSING_ARGUMENT": "SYNTAX",
    "EXTRA_ARGUMENT": "SYNTAX",
    "UNCLOSED_BLOCK": "STRUCTURE",
    "UNCLOSED_QUOTE": "SYNTAX",
    "MISSING_TERMINATOR": "SYNTAX",
    "INVALID_CONTINUATION": "SYNTAX",
    "DUPLICATE_DIRECTIVE": "SEMANTIC",
    "DUPLICATE_BLOCK": "SEMANTIC",
    "TRUNCATED_INPUT": "STRUCTURE",
    "COMMAND_EXECUTION_BLOCKED": "UNSUPPORTED",
    "PLUGIN_LOAD_BLOCKED": "UNSUPPORTED",
    "EXTERNAL_URL_BLOCKED": "UNSUPPORTED",
    "PATH_TRAVERSAL_BLOCKED": "UNSUPPORTED",
    # structured_config / tool profile: exception codes go to
    # conf_tool_profile.loss_codes, discovery codes go to
    # security_findings (findings are not losses)
    "DUPLICATE_INCLUDE": "SEMANTIC",
    "INCLUDE_CYCLE": "STRUCTURE",
    "APACHE_TAG_MISMATCH": "STRUCTURE",
    "CREDENTIAL_IN_URL": "SEMANTIC",
    "PRIVATE_RESOURCE_PATH_CANDIDATE": "SEMANTIC",
    "SENSITIVE_DIRECTIVE_CANDIDATE": "SEMANTIC",
    # conf include-graph codes: graph-level structural loss,
    # reported only on the merged contract
    "INCLUDE_DEPTH_LIMIT": "STRUCTURE",
    "GLOB_OVERLAP": "STRUCTURE",
    # structured_config / generic .cnf: syntax errors go to
    # the contract channel; discovery/block codes go to
    # cnf_profile loss_codes and security_findings;
    # UNKNOWN_CNF_PROFILE is an honest unknown;
    # BIDI_CONTROL_DETECTED is the first SECURITY code
    "UNKNOWN_CNF_PROFILE": "SEMANTIC",
    "INVALID_SECTION": "SYNTAX",
    "DUPLICATE_SECTION": "SEMANTIC",
    "DUPLICATE_KEY": "SEMANTIC",
    "INVALID_VARIABLE_REFERENCE": "SYNTAX",
    "MISSING_SECTION_REFERENCE": "REFERENCE",
    "PRIVATE_KEY_PATH_DETECTED": "SEMANTIC",
    "CERTIFICATE_PATH_DETECTED": "SEMANTIC",
    "SENSITIVE_VALUE_DETECTED": "SEMANTIC",
    "BIDI_CONTROL_DETECTED": "SECURITY",
    # systemd unit: syntax errors go to the contract channel;
    # semantic/discovery/block codes go to systemd_profile
    # loss_codes and security_findings; RESET_DIRECTIVE is
    # legal empty-value disclosure, not a loss
    "UNKNOWN_SECTION": "SYNTAX",
    "DIRECTIVE_IN_WRONG_SECTION": "SYNTAX",
    "DUPLICATE_SINGLE_VALUE": "SEMANTIC",
    "RESET_DIRECTIVE": "SEMANTIC",
    "INVALID_ESCAPE": "SYNTAX",
    "INVALID_SPECIFIER": "SYNTAX",
    "INVALID_EXEC_COMMAND": "SYNTAX",
    "ENVIRONMENT_FILE_BLOCKED": "UNSUPPORTED",
    # systemd unit, single-file semantic codes:
    # UNRESOLVED_SPECIFIER (instance specifier references),
    # INVALID_TIMER_SCHEDULE / TIMER_TARGET_MISSING (timer
    # units), CREDENTIAL_REFERENCE_BLOCKED (zero-IO
    # disclosure), INVALID_DEPENDENCY (dependency graph
    # validation)
    "UNRESOLVED_SPECIFIER": "SYNTAX",
    "INVALID_TIMER_SCHEDULE": "SYNTAX",
    "TIMER_TARGET_MISSING": "SEMANTIC",
    "MISSING_ENVIRONMENT_FILE": "SEMANTIC",
    "CREDENTIAL_REFERENCE_BLOCKED": "UNSUPPORTED",
    "TEMPLATE_INSTANCE_AMBIGUOUS": "SEMANTIC",
    "INVALID_DEPENDENCY": "SEMANTIC",
    # systemd unit assembly: DROP_IN_NOT_RESOLVED (drop-in
    # without a base unit), DROP_IN_CONFLICT (conflicting
    # effective values)
    "DROP_IN_NOT_RESOLVED": "UNSUPPORTED",
    "DROP_IN_CONFLICT": "SEMANTIC",
    # systemd unit dependency graph: DEPENDENCY_CYCLE
    # (DFS-detected cycle)
    "DEPENDENCY_CYCLE": "SEMANTIC",
    # record_stream / jsonl (2)
    "OVERSIZED_LINE": "RESOURCE",
    "INVALID_JSON_LINE": "SYNTAX",
    # docx (9)
    "MISSING_DOCUMENT_XML": "CONTAINER",
    "UNRESOLVED_STYLE_ID": "REFERENCE",
    "UNRESOLVED_NUMBERING_ID": "REFERENCE",
    "BROKEN_FIELD_RESULT": "STRUCTURE",
    "BROKEN_BOOKMARK_RANGE": "STRUCTURE",
    "ALTCHUNK_NOT_EXPANDED": "CONTAINER",
    "EMBEDDED_OBJECT_NOT_EXTRACTED": "CONTAINER",
    "IMAGE_NOT_EXTRACTED": "CONTAINER",
    "CHART_DATA_MISSING": "CONTAINER",
    # epub (6)
    "CHAPTER_PARSE_FAILED": "SYNTAX",
    "MALFORMED": "SYNTAX",
    "NAV_DOCUMENT_UNREADABLE": "SYNTAX",
    "SPINE_IDREF_UNRESOLVED": "REFERENCE",
    "SPINE_ITEM_NON_XHTML": "UNSUPPORTED",
    "SPINE_ITEM_MISSING_MEMBER": "CONTAINER",
    # pptx (4)
    "EVIDENCE_PARTS_NOT_EXTRACTED": "CONTAINER",
    "SLIDES_NOT_EXTRACTED": "CONTAINER",
    "SHAPES_NOT_EXTRACTED": "CONTAINER",
    "COMMENT_SAMPLES_TRUNCATED": "RESOURCE",
    # txt / markdown plain text (5)
    "UNCLOSED_CODE_FENCE": "STRUCTURE",
    "DISGUISED_CONTAINER_PARSED_AS_TEXT": "CONTAINER",
    "SUBTYPE_DELEGATION_FAILED": "RECOVERY",
    "TRUNCATED_TRAILING_SEQUENCE": "ENCODING",
    "TRUNCATED_UTF8_SEQUENCE_AT_EOF": "ENCODING",
    # pdf (4)
    "NATIVE_PAGE_ERROR": "SYNTAX",
    "OCR_BACKEND_UNAVAILABLE": "RESOURCE",
    "OCR_EMPTY_OUTPUT": "RECOVERY",
    "CHANNEL_DEGRADED": "RECOVERY",
    # code (4)
    "SYNTAX_ERROR": "SYNTAX",
    "MISSING_NODE": "STRUCTURE",
    "NODE_LIMIT_REACHED": "RESOURCE",
    "PARSE_FAILED": "SYNTAX",
    # odp (1)
    "BROKEN_ODF_XML": "SYNTAX",
    # image / pdf image regions (3)
    "OCR_ROUTE_UNAVAILABLE": "UNSUPPORTED",
    "NO_TEXT_EVIDENCE": "RESOURCE",
    "OCR_EXECUTION_FAILED": "RECOVERY",
}

#: Node-level reason values for the markdown node tree. Kept
#: separate from REASON_CODE_CATEGORY: node-level partial states
#: and whole-document marker codes are distinct vocabularies.
NODE_TREE_REASONS: frozenset[str] = frozenset(
    {
        "unclosed_fence",
        "ragged_table",
        "unbalanced_conflict",
        "EQUATION_UNCLOSED",
        "UNKNOWN_TEMPLATE_ENGINE",
        "UNKNOWN_MARKDOWN_EXTENSION",
    }
)

# ============================================================
# (c) Location vocabulary -- envelope fields + alias normalization
# ============================================================

#: Location envelope: field name -> (type, applicable scope).
LOCATION_FIELDS: dict[str, tuple[str, str]] = {
    "file": ("str", "全部格式"),
    "zip_part": ("str", "容器格式（docx/pptx/epub/odt 族）"),
    "page": ("int", "pdf / 分页叙事载体"),
    "slide": ("int", "pptx / odp"),
    "section": ("int | str", "epub / 分节载体"),
    "paragraph": ("int", "docx / 段内锚点格式"),
    "node_id": ("str", "markdown / 统一节点树"),
    "xpath": ("str", "docx / xml 系"),
    "offset": ("int", "txt / 字节偏移载体"),
    "byte_range": ("tuple[int, int]", "txt / 二进制证据"),
    "line_column": ("tuple[int, int]", "code / jsonl"),
    "bbox": ("tuple[float, float, float, float]", "pdf / 版面证据"),
}

#: Observed alias -> envelope canonical name (rename only,
#: never recompute).
LOCATION_KEY_ALIASES: dict[str, str] = {
    "page_number": "page",
    "paragraph_index": "paragraph",
    "part_name": "zip_part",
}


def canonical_location(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project a record onto the location envelope.
    Known location keys are extracted from any mapping: envelope
    names are kept as-is, aliases are renamed via
    LOCATION_KEY_ALIASES, unknown keys pass through untouched.
    Values are never recomputed; when both a canonical name and
    an alias appear, the canonical name wins.

    Args:
        record: any record mapping (non-mapping input returns {}).

    Returns:
        The projected new dict; insertion order preserved, first
        occurrence wins.
    """
    if not isinstance(record, Mapping):
        return {}
    located: dict[str, Any] = {}
    for key, value in record.items():
        canonical = LOCATION_KEY_ALIASES.get(key, key)
        if canonical in located:
            continue
        located[canonical] = value
    return located


# ============================================================
# per-format method -> unified evidence-source enumeration
# ============================================================

#: pdf extraction_method values -> evidence sources. native
#: values are direct structural reads (figure_evidence records
#: cluster geometry, never re-drawn); borderless_tables =
#: heuristic table recovery; image_regions = embedded image
#: resources; ocr values = optical recognition;
#: hybrid_native_ocr = None (native and OCR channels coexist;
#: no single unified value applies).
EVIDENCE_METHOD_MAP: dict[str, Optional[str]] = {
    "native_text": "native",
    "native_tables": "native",
    "annotations": "native",
    "page_geometry": "native",
    "text_characters": "native",
    "watermark": "native",
    "file_integrity_evidence": "native",
    "figure_evidence": "native",
    "image_regions": "included",
    "borderless_tables": "recovered",
    "borderless_tables_ocr": "ocr",
    "ocr": "ocr",
    "hybrid_native_ocr": None,
}

#: pptx slide order methods: declared order is a structural
#: fact (native); filename fallback is synthesized (inferred).
SLIDE_ORDER_METHOD_MAP: dict[str, Optional[str]] = {
    "declared": "native",
    "filename_fallback": "inferred",
}

#: Known table detection_method values, registered for
#: information only: an open-ended value space (f-string
#: concatenation), not a controlled enum, so no mapping or
#: completeness claim is made.
DETECTION_METHOD_KNOWN_VALUES: frozenset[str] = frozenset(
    {
        "nonempty_4_connected_component",
        "excel_table_object",
        "excel_autofilter",
        "column_alignment",
        "geometry",
        "alignment",
        "alignment_ocr",
        "component+header_evidence",
    }
)


def evidence_type_of(method: str) -> Optional[str]:
    """Look up the unified evidence source for a method literal.
    Args:
        method: a method literal from parser output.

    Returns:
        One of EVIDENCE_SOURCES, or None (no mapping, or dual
        channels coexist).
    """
    if method in EVIDENCE_METHOD_MAP:
        return EVIDENCE_METHOD_MAP[method]
    return SLIDE_ORDER_METHOD_MAP.get(method)


# ============================================================
# (d) Loss reason vocabulary
# ============================================================

#: Cause-axis values: ocr_recovered (recovered via OCR only);
#: render_recovered (rendered only); inferred_content;
#: resource_missing (missing or budget-stopped);
#: security_blocked; policy_excluded (unsupported by policy).
LOSS_REASONS: frozenset[str] = frozenset(
    {
        "ocr_recovered",
        "render_recovered",
        "inferred_content",
        "resource_missing",
        "security_blocked",
        "policy_excluded",
    }
)

#: Loss envelope scopes: document / section / page / object.
LOSS_SCOPES: frozenset[str] = frozenset({"document", "section", "page", "object"})

#: reason_code -> loss reason map (values are LOSS_REASONS or
#: None). None entries have no honest cause-axis value: their
#: failure mode is already carried by REASON_CODE_CATEGORY
#: (syntax / structure / encoding failures), the original
#: data is preserved (unresolved references keep the target
#: text, shadowed keys keep the override chain), or they are
#: pure degradation records. Config contract codes never
#: enter the marker channel and are intentionally absent.
#: Per-code rationale lives in the inline comments.
REASON_CODE_TO_LOSS_REASON: dict[str, Optional[str]] = {
    # policy_excluded (unsupported by policy)
    "SPINE_ITEM_NON_XHTML": "policy_excluded",
    "DISGUISED_CONTAINER_PARSED_AS_TEXT": "policy_excluded",
    "ALTCHUNK_NOT_EXPANDED": "policy_excluded",
    "EMBEDDED_OBJECT_NOT_EXTRACTED": "policy_excluded",
    "OCR_ROUTE_UNAVAILABLE": "policy_excluded",  # #127 纯图输入无 OCR 路由
    # ── resource_missing (budget stop / resource not obtained) ──
    "SHEET_KEPT_AS_SKELETON": "resource_missing",
    "CELL_EXTRACTION_SKIPPED_SHAPE_BUDGET": "resource_missing",
    "ROWS_NOT_EXTRACTED": "resource_missing",
    "COLUMNS_NOT_EXTRACTED": "resource_missing",
    "RECORDS_BEYOND_ROW_CAP": "resource_missing",
    "RECORD_COLUMNS_TRUNCATED": "resource_missing",
    "RECORDS_BEYOND_CELL_BUDGET": "resource_missing",
    "CELL_BUDGET_STOPPED": "resource_missing",
    "OVERSIZED_LINE": "resource_missing",
    "MISSING_DOCUMENT_XML": "resource_missing",
    "IMAGE_NOT_EXTRACTED": "resource_missing",
    "CHART_DATA_MISSING": "resource_missing",
    "SPINE_ITEM_MISSING_MEMBER": "resource_missing",
    "OCR_BACKEND_UNAVAILABLE": "resource_missing",
    "NO_TEXT_EVIDENCE": "resource_missing",  # #127 KIE 字段文本层无证据
    "EVIDENCE_PARTS_NOT_EXTRACTED": "resource_missing",
    "SLIDES_NOT_EXTRACTED": "resource_missing",
    "SHAPES_NOT_EXTRACTED": "resource_missing",
    "COMMENT_SAMPLES_TRUNCATED": "resource_missing",
    "NODE_LIMIT_REACHED": "resource_missing",
    # None (no honest cause-axis value)
    "DUPLICATE_KEY_SHADOWED": None,
    "YAML_DOC_CHUNK_FAILED": None,
    "REFERENCE_UNRESOLVED": None,
    "REFERENCE_CYCLIC": None,
    "REFERENCE_EXTERNAL": None,
    "INVALID_JSON_LINE": None,
    "UNRESOLVED_STYLE_ID": None,
    "UNRESOLVED_NUMBERING_ID": None,
    "BROKEN_FIELD_RESULT": None,
    "BROKEN_BOOKMARK_RANGE": None,
    "CHAPTER_PARSE_FAILED": None,
    "MALFORMED": None,
    "NAV_DOCUMENT_UNREADABLE": None,
    "SPINE_IDREF_UNRESOLVED": None,
    "UNCLOSED_CODE_FENCE": None,
    "SUBTYPE_DELEGATION_FAILED": None,
    "TRUNCATED_TRAILING_SEQUENCE": None,
    "TRUNCATED_UTF8_SEQUENCE_AT_EOF": None,
    "NATIVE_PAGE_ERROR": None,
    "OCR_EMPTY_OUTPUT": None,
    "OCR_EXECUTION_FAILED": None,  # §二十 图片 OCR 执行失败（webp/渲染异常），同 OCR_EMPTY_OUTPUT 族
    "CHANNEL_DEGRADED": None,
    "SYNTAX_ERROR": None,
    "MISSING_NODE": None,
    "PARSE_FAILED": None,
    # odp recovery-path record: partial recovery possible,
    # not whole-content loss
    "BROKEN_ODF_XML": None,
}

#: Coverage dimensions; only text / asset / annotation /
#: reference have document-level derived evidence so far.
COVERAGE_DIMENSIONS: tuple[str, ...] = (
    "text",
    "structure",
    "layout",
    "asset",
    "metadata",
    "annotation",
    "reference",
)


def loss_reason_of(reason_code: str) -> Optional[str]:
    """Look up the loss reason for a marker-contract reason_code.
    Args:
        reason_code: the reason_code string from an unresolved
            record.

    Returns:
        One of LOSS_REASONS, or None (cause axis does not apply;
        the failure-mode axis is carried by REASON_CODE_CATEGORY).
    """
    return REASON_CODE_TO_LOSS_REASON.get(reason_code)


# ============================================================
# (e) External reference state machine + input integrity codes
# ============================================================

#: Five monotonic reference states:
#:
#: - link_detected: format evidence keys registered that a
#:   link exists;
#: - target_resolved: the target string resolved (join hit or
#:   direct URI);
#: - target_read: target content read -- only possible for
#:   package_internal scope; external targets are recorded
#:   but never fetched;
#: - target_executed / target_trusted: always False by policy
#:   -- this project never executes or trusts targets (a
#:   proven fact, not an unimplemented feature).
#:
#: Discipline: policy False != evidence False (key present,
#: value empty) != None (no evidence key at all).
REFERENCE_STATES: tuple[str, ...] = (
    "link_detected",
    "target_resolved",
    "target_read",
    "target_executed",
    "target_trusted",
)

#: Reference scopes.
REFERENCE_SCOPES: tuple[str, ...] = (
    "external",  # 包外目标（网络/文件系统）——target_read 恒 False
    "package_internal",  # 同包内目标（rels/spine/manifest）
    "fragment",  # 同文档内锚（bookmark/#anchor）
)

#: Per-format evidence keys for the first three reference
#: states (target_executed / target_trusted are policy
#: constants, never mapped). Values are metadata dot-paths:
#: any path present and non-zero -> True; key present but
#: empty -> False; key absent -> None. ``ns[kind=v]`` counts
#: records whose ``kind`` equals ``v``. None means the format
#: has no honest evidence key for that state.
#:
#: Per-format notes:
#:
#: - docx: hyperlink and external-connection counts detect;
#:   external_uri_count resolves (join hits); no read key.
#: - pptx: external_targets.count detects and resolves
#:   (targets are direct rels strings); no read key.
#: - ppt_cfb: ExObjList registry counts detect; URL-carrying
#:   entries resolve; no read key.
#: - odp: slide_objects[kind=hyperlink] objects carry link
#:   targets verbatim; no read key.
#: - pdf: link_count detects; external and resolved internal
#:   counts resolve; no read key.
#: - epub: four navigation counters detect; no positive
#:   resolution key (unresolved ids are registered
#:   negatively); chapter spans count as read targets.
#: - tex: external label-reference and IO-reference counts
#:   detect; IO reference count resolves; no read key.
#: - rtf: hyperlink_count detects and resolves (fldinst
#:   targets carried verbatim); no read key.
#: - excel: all None -- hyperlink evidence is object-level
#:   only, no document-level count key.
FORMAT_REFERENCE_EVIDENCE_MAP: dict[str, dict[str, Optional[tuple[str, ...]]]] = {
    "docx": {
        "link_detected": (
            "docx_links.hyperlink_count",
            "docx_security.external_connections.count",
        ),
        "target_resolved": ("docx_links.external_uri_count",),
        "target_read": None,
    },
    "pptx": {
        "link_detected": ("pptx_security.external_targets.count",),
        "target_resolved": ("pptx_security.external_targets.count",),
        "target_read": None,
    },
    "ppt_cfb": {
        "link_detected": ("ppt_cfb_structure.hyperlink_count",),
        "target_resolved": ("ppt_cfb_structure.hyperlink_url_count",),
        "target_read": None,
    },
    "odp": {
        "link_detected": ("odp_slide_objects[kind=hyperlink]",),
        "target_resolved": ("odp_slide_objects[kind=hyperlink]",),
        "target_read": None,
    },
    "pdf": {
        "link_detected": ("pdf_navigation.link_count",),
        "target_resolved": (
            "pdf_navigation.external_uri_count",
            "pdf_navigation.internal_link_count",
        ),
        "target_read": None,
    },
    "epub": {
        "link_detected": (
            "epub_navigation.internal_link_count",
            "epub_navigation.external_link_count",
            "epub_navigation.stylesheet_link_count",
            "epub_navigation.fragment_link_count",
        ),
        "target_resolved": None,
        "target_read": ("epub_chapter_spans",),
    },
    "tex": {
        "link_detected": (
            "tex_structure.external_label_reference_count",
            "tex_structure.external_io_references.count",
        ),
        "target_resolved": ("tex_structure.external_io_references.count",),
        "target_read": None,
    },
    "rtf": {
        "link_detected": ("rtf_structure.hyperlink_count",),
        "target_resolved": ("rtf_structure.hyperlink_count",),
        "target_read": None,
    },
    "excel": {
        "link_detected": None,
        "target_resolved": None,
        "target_read": None,
    },
}

#: Input-level integrity codes: facts about the input file
#: itself being incomplete or concatenated. Parser-side
#: budget stops are not input defects and are deliberately
#: excluded.
INPUT_INTEGRITY_CODES: tuple[str, ...] = (
    "TRUNCATED_INPUT",
    "APPENDED_DATA",
    "MULTIPLE_DOCUMENTS_DETECTED",
    "INCOMPLETE_REVISION",
)

#: Code -> evidence keys (only semantically direct keys;
#: adjacent semantics stay None pending corpus evidence; the
#: execution side is _derive_codes in
#: pipeline/input_integrity.py and matches this table key by
#: key):
#:
#: - TRUNCATED_INPUT: txt truncation codes, rtf unbalanced
#:   groups, tex unclosed group depth;
#: - APPENDED_DATA: trailing_data.detected;
#: - MULTIPLE_DOCUMENTS_DETECTED:
#:   trailing_data.polyglot_detected (known container magic
#:   in the trailing region); prefix-type concatenation (SFX)
#:   has no reliable start anchor and is not mapped;
#: - INCOMPLETE_REVISION: None -- multiple revisions prove
#:   revisions exist, not that the last one is incomplete.
INPUT_INTEGRITY_EVIDENCE_MAP: dict[str, Optional[tuple[str, ...]]] = {
    "TRUNCATED_INPUT": (
        "truncation.truncated",
        "rtf_structure.unbalanced_groups",
        "tex_structure.unclosed_group_depth",
    ),
    "APPENDED_DATA": ("format_verification.trailing_data.detected",),
    "MULTIPLE_DOCUMENTS_DETECTED": ("format_verification.trailing_data.polyglot_detected",),
    "INCOMPLETE_REVISION": None,
}

# ============================================================
# (h) PDF security semantic signals
# ============================================================

#: PDF security compliance signal names (the signal slot of
#: ``pdf_security:<signal>:<count>`` fact codes; counts are
#: dynamic and not registered). The execution-side source of
#: truth is _SECURITY_SIGNAL_ORDER in pdf/evidence/security.py;
#: test_vocab_registry.py keeps the two in sync. A
#: warnings-signal namespace, kept separate from
#: REASON_CODE_CATEGORY and the two loss-axis maps.
PDF_SECURITY_SIGNALS: tuple[str, ...] = (
    "javascript_present",
    "invisible_text",
    "redaction_text_recoverable",
    "visual_obscuration",
    "partial_byte_range",
    "cmap_mismatch",
    "xfa_script_present",
    "launch_action_present",
    "submitform_action_present",
    "remote_goto_action_present",
)

#: Signal -> evidence keys (semantically direct keys; the
#: execution side is build_security_warnings in
#: pdf/evidence/security.py and matches this table key by
#: key).
PDF_SECURITY_SIGNAL_EVIDENCE_MAP: dict[str, tuple[str, ...]] = {
    "javascript_present": ("pdf_security.javascript.present",),
    "invisible_text": ("pdf_security.invisible_text.present",),
    "redaction_text_recoverable": ("pdf_security.redactions.marks",),
    "visual_obscuration": ("pdf_security.redactions.visual_obscuration.count",),
    "partial_byte_range": ("pdf_security.signatures.signature_fields",),
    "cmap_mismatch": ("pdf_security.cmap_audit.mismatch_count",),
    "xfa_script_present": ("pdf_security.forms.xfa_scripts.count",),
    "launch_action_present": ("pdf_security.document_actions.counts.Launch",),
    "submitform_action_present": (
        "pdf_security.document_actions.counts.SubmitForm",
    ),
    "remote_goto_action_present": (
        "pdf_security.document_actions.counts.GoToR",
    ),
}


def _evidence_hit(metadata: Mapping[str, Any], path: str) -> Optional[bool]:
    """Resolve one evidence key path against metadata (read-only).
    Two syntaxes are supported: the dot-path (``ns.sub.key``
    walks metadata level by level; missing key -> None; a value
    counts as a hit when it is a truthy bool, a number > 0, or a
    non-empty sequence or mapping) and the object selector
    (``ns[kind=v]`` counts records whose ``kind`` equals ``v``).

    Args:
        metadata: the document metadata mapping.
        path: one evidence key path.

    Returns:
        True (hit), False (key present, empty value), or None
        (no evidence key).
    """
    if "[" in path:  # 对象级选择器：ns[kind=value]
        namespace, _, rest = path.partition("[")
        if not (rest.startswith("kind=") and rest.endswith("]")):
            return None  # 未登记的选择器语法 → 按无证据处理
        wanted = rest[len("kind=") : -1]
        node: Any = metadata
        for part in namespace.split("."):
            if not isinstance(node, Mapping):
                return None
            node = node.get(part)
        if not isinstance(node, list):
            return None
        return (
            sum(
                1 for record in node if isinstance(record, Mapping) and record.get("kind") == wanted
            )
            > 0
        )
    node = metadata
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    if isinstance(node, bool):
        return node
    if isinstance(node, (int, float)):
        return node > 0
    if isinstance(node, (list, tuple, set, frozenset, Mapping)):
        return len(node) > 0
    return bool(node) if node is not None else None


def reference_state_of(document: Any) -> dict[str, Optional[bool]]:
    """Compute the five reference states for a document (read-only).
    Cross-format OR semantics: any format's evidence key hit and
    non-zero -> True; key present but empty -> False; all keys
    absent -> None. target_executed / target_trusted are always
    False by policy, regardless of input.

    Args:
        document: an object with a ``metadata`` attribute, or a
            metadata mapping directly.

    Returns:
        A dict over REFERENCE_STATES with values in {True, False,
        None}; read-only, the input is not modified.
    """
    states: dict[str, Optional[bool]] = {
        "link_detected": None,
        "target_resolved": None,
        "target_read": None,
        # policy constants: never executed or trusted
        "target_executed": False,
        "target_trusted": False,
    }
    metadata: Any = getattr(document, "metadata", None)
    if not isinstance(metadata, Mapping):
        if isinstance(document, Mapping):
            metadata = document
        else:
            return states
    seen = {
        "link_detected": False,
        "target_resolved": False,
        "target_read": False,
    }
    hit_any = dict(seen)
    for row in FORMAT_REFERENCE_EVIDENCE_MAP.values():
        for state, paths in row.items():
            if paths is None or hit_any[state]:
                continue
            for path in paths:
                hit = _evidence_hit(metadata, path)
                if hit is None:
                    continue
                seen[state] = True
                if hit:
                    hit_any[state] = True
                    break
    for state, is_hit in hit_any.items():
        if is_hit:
            states[state] = True
        elif seen[state]:
            states[state] = False
    return states


# ============================================================
# (f) Sensitive candidate type vocabulary
# ============================================================

#: Sensitive candidate detection types. Mirrors the detection
#: patterns in core/diagnostics/sensitive_signals._PATTERNS;
#: test_vocab_registry.py asserts the two stay in sync. A hit
#: only means the text matches the shape of the type, never
#: a confirmed match.
SENSITIVE_CANDIDATE_TYPES: tuple[str, ...] = (
    "cn_mobile",
    "email",
    "cn_national_id",
    "bank_card_candidate",
    "private_ip",
    "credential_assignment",
    "jwt_token",
    "patient_id",
)


__all__ = [
    "CROSS_FORMAT_VOCAB_VERSION",
    "EVIDENCE_SOURCES",
    "ERROR_CATEGORIES",
    "REASON_CODE_CATEGORY",
    "NODE_TREE_REASONS",
    "LOCATION_FIELDS",
    "LOCATION_KEY_ALIASES",
    "canonical_location",
    "EVIDENCE_METHOD_MAP",
    "SLIDE_ORDER_METHOD_MAP",
    "DETECTION_METHOD_KNOWN_VALUES",
    "evidence_type_of",
    "LOSS_REASONS",
    "LOSS_SCOPES",
    "REASON_CODE_TO_LOSS_REASON",
    "COVERAGE_DIMENSIONS",
    "loss_reason_of",
    "REFERENCE_STATES",
    "REFERENCE_SCOPES",
    "FORMAT_REFERENCE_EVIDENCE_MAP",
    "reference_state_of",
    "INPUT_INTEGRITY_CODES",
    "INPUT_INTEGRITY_EVIDENCE_MAP",
    "PDF_SECURITY_SIGNALS",
    "PDF_SECURITY_SIGNAL_EVIDENCE_MAP",
    "SENSITIVE_CANDIDATE_TYPES",
]
