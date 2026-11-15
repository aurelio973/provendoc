"""RepresentationUnit — the unified semantic unit interface.

The canonical semantic object of the whole Provendoc runtime:
representation, chunking, quality evaluation, and retrieval all
use it as the shared semantic carrier.

Chunk is a compatibility alias of RepresentationUnit
(base.models.chunk.Chunk); new code should use the typed fields
directly.

Field design:
    - required: text, unit_type
    - core semantic: semantic_primitives (cross-domain uniform)
    - optional: semantic_role, discourse_signals, structural_info,
      source_context
    - every field serializes (JSON-compatible)
"""

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Any, Dict, List, Literal, Optional

# three granularity tiers
Granularity = Literal["coarse", "base", "fine"]


class UnitType(Enum):
    """Semantic unit type."""
    NARRATIVE = "narrative"  # 叙事文本（段落、文章）
    CONFIG = "config"  # 配置项（JSON, YAML, TOML）
    TABLE = "table"  # 表格数据
    CODE = "code"  # 代码块
    HEADING = "heading"  # 标题/章节
    LIST = "list"  # 列表
    PROCEDURE = "procedure"  # 步骤/流程
    MIXED = "mixed"  # 混合类型


class SemanticRole(Enum):
    """Semantic role."""
    DEFINITION = "definition"  # 定义/概念
    EXAMPLE = "example"  # 示例
    EXPLANATION = "explanation"  # 解释
    PROCEDURE_STEP = "procedure_step"  # 步骤
    REFERENCE = "reference"  # 引用/交叉引用
    SUMMARY = "summary"  # 总结
    TRANSITION = "transition"  # 过渡
    UNKNOWN = "unknown"


@dataclass
class DiscourseSignal:
    """Lightweight discourse signal."""
    signal_type: str  # "explanation_marker", "transition", "continuation", etc.
    text: str  # 信号文本
    position: int  # 在 text 中的位置
    confidence: float = 0.8


@dataclass
class SemanticPrimitives:
    """Shared semantic primitives — core fields every RepresentationUnit
    carries.

    Design:
        - cross-domain (narrative/config/table/code) semantic identity
        - keeps different representations' metadata from drifting
        - uniform interface for quality-invariant evaluation
        - explicit "which units must not be separated" signals for
          chunking decisions

    The core question is not "what does this unit mean" but
    "which units cannot be separated from this one".
    """

    identity: str = ""
    """唯一单元标识符（UUID 或 deterministic hash）。"""

    hierarchy_path: List[str] = field(default_factory=list)
    """标题路径 [h1, h2, h3]，表示该单元在文档层级中的位置。
    空列表表示无层级上下文。"""

    locality_scope: str = "unknown"
    """局部性范围: "section", "paragraph", "sentence", "block", "document", "unknown"。
    表示该单元的语义局部性边界。同一 locality_scope 的单元不应被切分到不同 chunk。"""

    semantic_role: str = "unknown"
    """语义角色: "definition", "example", "explanation", "procedure_step",
    "reference", "summary", "transition", "heading", "body", "unknown"。
    使用字符串而非 Enum，便于扩展和序列化。"""

    integrity_guarantees: List[str] = field(default_factory=list)
    """该单元承诺的不变量列表。
    例如: ["hierarchy_continuity", "structural_block_complete"]
    用于运行时验证和质量评估。"""

    confidence: float = 1.0
    """分类/提取置信度 [0.0, 1.0]。"""

    # === semantic dependencies (core) ===
    dependencies: List[str] = field(default_factory=list)
    """该单元依赖的其他单元 identity 列表。
    例如：["def-001"] 表示此单元（如示例）依赖于定义单元 def-001。
    Chunking 时必须确保依赖单元在同一或相邻 chunk 中。"""

    discourse_cues: List[str] = field(default_factory=list)
    """检测到的 discourse cue 类型列表。
    例如：["for_example", "therefore", "definition"]
    用于 lightweight structural semantics 分析。"""

    def __post_init__(self):
        """Hard cross-domain constraints:

        - confidence must be in [0.0, 1.0]: out-of-range values fail
          immediately instead of silently propagating into
          downstream confidence aggregation.
        - identity may be empty (the producer may not have filled
          it); RepresentationUnit.__post_init__ fills it in — the
          fallback lives on the consumer side, where unit_type and
          sequence index are known.
        """
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"SemanticPrimitives.confidence must be in [0.0, 1.0], "
                f"got {self.confidence!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dict."""
        return {
            "identity": self.identity,
            "hierarchy_path": self.hierarchy_path,
            "locality_scope": self.locality_scope,
            "semantic_role": self.semantic_role,
            "integrity_guarantees": self.integrity_guarantees,
            "confidence": round(self.confidence, 3),
            "dependencies": self.dependencies,
            "discourse_cues": self.discourse_cues,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SemanticPrimitives":
        """Restore from a dict."""
        return cls(
            identity=data.get("identity", ""),
            hierarchy_path=data.get("hierarchy_path", []),
            locality_scope=data.get("locality_scope", "unknown"),
            semantic_role=data.get("semantic_role", "unknown"),
            integrity_guarantees=data.get("integrity_guarantees", []),
            confidence=data.get("confidence", 1.0),
            dependencies=data.get("dependencies", []),
            discourse_cues=data.get("discourse_cues", []),
        )


@dataclass
class StructuralInfo:
    """Structural information."""
    heading_level: int = 0  # 0 = 无 heading
    heading_path: List[str] = field(default_factory=list)
    parent_section: Optional[str] = None
    page_number: Optional[int] = None
    position_in_parent: Optional[int] = None  # 在父 section 中的顺序


@dataclass
class SourceContext:
    """Source context."""
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    page: Optional[int] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None


@dataclass
class RepresentationUnit:
    """Semantic representation unit — the core output unit of the
    representation layer. More structured than a Chunk and carries
    semantic information.

    Design:
        - semantic_primitives: shared cross-domain primitives (required)
        - domain extensions: via discourse_signals, metadata, etc.

    Usage:
        1. the representation layer produces RepresentationUnit lists
        2. quality evaluation checks invariants over semantic_primitives
        3. retrieval optimizes on hierarchy_path and semantic_role

    Serialization: to_dict() / from_dict() (JSON-compatible).
    """

    # ======================================
    # required fields
    # ======================================
    text: str
    unit_type: UnitType

    # ======================================
    # shared semantic primitives (core, cross-domain)
    # ======================================
    semantic_primitives: SemanticPrimitives = field(default_factory=SemanticPrimitives)

    # ======================================
    # semantic fields (narrative-domain extension)
    # ======================================
    semantic_role: SemanticRole = SemanticRole.UNKNOWN
    discourse_signals: List[DiscourseSignal] = field(default_factory=list)

    # ======================================
    # structural fields (legacy; migrating into semantic_primitives)
    # ======================================
    heading_path: List[str] = field(default_factory=list)
    heading: str = ""  # 当前 heading 文本
    structural_info: StructuralInfo = field(default_factory=StructuralInfo)

    # ======================================
    # sequence / composition info (formerly metadata keys)
    # ======================================
    sequence_index: int = 0  # 原 chunk_index，在序列中的索引
    component_types: List[str] = field(default_factory=list)  # 原 unit_types，合并后的类型列表
    component_count: int = 1  # 原 unit_count，合并前的单元数
    merged_orphan: bool = False  # 是否由 orphan 合并产生

    # ======================================
    # context propagation
    # ======================================
    context_from_previous: str = ""  # 前一段落最后一句
    overlap_context: str = ""  # overlap 文本

    # ======================================
    # inline semantics
    # ======================================
    inline_semantics: List[str] = field(default_factory=list)

    # ======================================
    # quality / marker fields
    # ======================================
    duplicate_flag: bool = False  # 重复标记
    references: List[str] = field(default_factory=list)  # 交叉引用列表

    # ======================================
    # source fields
    # ======================================
    source_context: SourceContext = field(default_factory=SourceContext)

    # ======================================
    # metadata (extensions + backward-compatibility facade)
    # ======================================
    metadata: Dict[str, Any] = field(default_factory=dict)
    """扩展元数据字典。

    旧代码通过 chunk.metadata["block_type"] 访问的信息，
    新代码应直接使用 typed 字段：unit.unit_type.value。
    metadata 仍保留用于第三方扩展和未标准化的字段。
    """

    # ======================================
    # granularity (three parallel tiers)
    # ======================================
    granularity: Granularity = "base"
    """粒度层级:
        - "coarse": 粗粒度，跨标题合并的 section 级别块
        - "base":   基础粒度，现有 discourse unit 级别（默认）
        - "fine":   细粒度，超 chunk_size 预算的段落内句界切块
    三层并列存储，检索时按需选择粒度层。
    """

    # ======================================
    # quality fields (filled by quality evaluation)
    # ======================================
    quality_score: Optional[float] = None
    failure_modes: List[str] = field(default_factory=list)  # 检测到的 failure mode

    def __post_init__(self):
        """Validate and keep semantic_primitives in sync with the legacy
        fields."""
        if not self.text:
            raise ValueError("text cannot be empty")

        # sync heading_path (legacy field -> semantic_primitives)
        if self.heading_path and not self.semantic_primitives.hierarchy_path:
            self.semantic_primitives.hierarchy_path = list(self.heading_path)
        elif self.semantic_primitives.hierarchy_path and not self.heading_path:
            self.heading_path = list(self.semantic_primitives.hierarchy_path)

        # sync semantic_role (legacy Enum -> semantic_primitives string)
        if self.semantic_role != SemanticRole.UNKNOWN and self.semantic_primitives.semantic_role == "unknown":
            self.semantic_primitives.semantic_role = self.semantic_role.value
        elif self.semantic_primitives.semantic_role != "unknown" and self.semantic_role == SemanticRole.UNKNOWN:
            try:
                self.semantic_role = SemanticRole(self.semantic_primitives.semantic_role)
            except ValueError:
                pass  # 保持 UNKNOWN

        # sync structural_info.heading_path
        if self.heading_path and not self.structural_info.heading_path:
            self.structural_info.heading_path = list(self.heading_path)

        # default integrity guarantees inherit from unit_type when empty
        if not self.semantic_primitives.integrity_guarantees:
            default_guarantees = self._default_integrity_guarantees()
            if default_guarantees:
                self.semantic_primitives.integrity_guarantees = default_guarantees

        # Ensure identity is non-empty: dependency matching keys on
        # identity, so an empty one would silently break
        # cross-references. When the producer leaves it blank, a
        # deterministic id is generated (unit_type + sequence index
        # + content-hash prefix) — same input, same id, stable across
        # runs and serialization round-trips (to_dict exports the
        # filled value; from_dict sees it non-empty and skips).
        if not self.semantic_primitives.identity:
            text_digest = sha256(self.text.encode("utf-8")).hexdigest()[:10]
            self.semantic_primitives.identity = (
                f"{self.unit_type.value}:{self.sequence_index}:{text_digest}"
            )

    def _default_integrity_guarantees(self) -> List[str]:
        """Return the default invariant promise for this unit_type."""
        guarantees = ["hierarchy_continuity"]  # 所有类型默认保持层级
        if self.unit_type == UnitType.CODE:
            guarantees.append("structural_block_complete")
        elif self.unit_type == UnitType.TABLE:
            guarantees.append("structural_block_complete")
        elif self.unit_type == UnitType.LIST:
            guarantees.append("discourse_locality")
        elif self.unit_type == UnitType.PROCEDURE:
            guarantees.append("discourse_locality")
        return guarantees

    # ======================================
    # serialization
    # ======================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a JSON-compatible dict."""
        return {
            "text": self.text,
            "unit_type": self.unit_type.value,
            "granularity": self.granularity,
            "semantic_primitives": self.semantic_primitives.to_dict(),
            "semantic_role": self.semantic_role.value,
            "discourse_signals": [
                {
                    "signal_type": s.signal_type,
                    "text": s.text,
                    "position": s.position,
                    "confidence": s.confidence,
                }
                for s in self.discourse_signals
            ],
            "heading_path": self.heading_path,
            "heading": self.heading,
            "structural_info": {
                "heading_level": self.structural_info.heading_level,
                "heading_path": self.structural_info.heading_path,
                "parent_section": self.structural_info.parent_section,
                "page_number": self.structural_info.page_number,
                "position_in_parent": self.structural_info.position_in_parent,
            },
            "sequence_index": self.sequence_index,
            "component_types": self.component_types,
            "component_count": self.component_count,
            "merged_orphan": self.merged_orphan,
            "context_from_previous": self.context_from_previous,
            "overlap_context": self.overlap_context,
            "inline_semantics": self.inline_semantics,
            "duplicate_flag": self.duplicate_flag,
            "references": self.references,
            "source_context": {
                "file_name": self.source_context.file_name,
                "file_type": self.source_context.file_type,
                "page": self.source_context.page,
                "line_start": self.source_context.line_start,
                "line_end": self.source_context.line_end,
            },
            "metadata": self.metadata,
            "quality_score": self.quality_score,
            "failure_modes": self.failure_modes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RepresentationUnit":
        """Restore from a dict."""
        discourse_signals = [
            DiscourseSignal(
                signal_type=s["signal_type"],
                text=s["text"],
                position=s["position"],
                confidence=s.get("confidence", 0.8),
            )
            for s in data.get("discourse_signals", [])
        ]

        # restore semantic_primitives, or infer from legacy fields
        sp_data = data.get("semantic_primitives")
        if sp_data:
            semantic_primitives = SemanticPrimitives.from_dict(sp_data)
        else:
            # backward compatibility: infer from legacy fields
            semantic_primitives = SemanticPrimitives(
                hierarchy_path=data.get("heading_path", []),
                semantic_role=data.get("semantic_role", "unknown"),
            )

        structural_info = StructuralInfo(
            heading_level=data.get("structural_info", {}).get("heading_level", 0),
            heading_path=data.get("structural_info", {}).get("heading_path", []),
            parent_section=data.get("structural_info", {}).get("parent_section"),
            page_number=data.get("structural_info", {}).get("page_number"),
            position_in_parent=data.get("structural_info", {}).get("position_in_parent"),
        )

        source_context = SourceContext(
            file_name=data.get("source_context", {}).get("file_name"),
            file_type=data.get("source_context", {}).get("file_type"),
            page=data.get("source_context", {}).get("page"),
            line_start=data.get("source_context", {}).get("line_start"),
            line_end=data.get("source_context", {}).get("line_end"),
        )

        return cls(
            text=data["text"],
            unit_type=UnitType(data.get("unit_type", "narrative")),
            granularity=data.get("granularity", "base"),
            semantic_primitives=semantic_primitives,
            semantic_role=SemanticRole(data.get("semantic_role", "unknown")),
            discourse_signals=discourse_signals,
            heading_path=data.get("heading_path", []),
            heading=data.get("heading", ""),
            structural_info=structural_info,
            sequence_index=data.get("sequence_index", 0),
            component_types=data.get("component_types", []),
            component_count=data.get("component_count", 1),
            merged_orphan=data.get("merged_orphan", False),
            context_from_previous=data.get("context_from_previous", ""),
            overlap_context=data.get("overlap_context", ""),
            inline_semantics=data.get("inline_semantics", []),
            duplicate_flag=data.get("duplicate_flag", False),
            references=data.get("references", []),
            source_context=source_context,
            metadata=data.get("metadata", {}),
            quality_score=data.get("quality_score"),
            failure_modes=data.get("failure_modes", []),
        )

    # ======================================
    # backward compatibility: Chunk metadata view
    # ======================================

    @property
    def _metadata_facade(self) -> Dict[str, Any]:
        """Dynamically build a dict emulating legacy Chunk.metadata
        access: chunk.metadata["block_type"] maps to typed fields
        like unit.unit_type.value. Transitional compatibility
        facade — do not use in new code.
        """
        facade = {
            "block_type": self.unit_type.value,
            "unit_type": self.unit_type.value,
            "granularity": self.granularity,
            "semantic_path": "/".join(self.semantic_primitives.hierarchy_path) if self.semantic_primitives.hierarchy_path else "",
            "heading_path": self.heading_path,
            "semantic_role": self.semantic_role.value,
            "discourse_signals": [
                {
                    "signal_type": s.signal_type,
                    "text": s.text,
                    "position": s.position,
                    "confidence": s.confidence,
                }
                for s in self.discourse_signals
            ],
            "structural_info": {
                "heading_level": self.structural_info.heading_level,
                "heading_path": self.structural_info.heading_path,
                "parent_section": self.structural_info.parent_section,
                "page_number": self.structural_info.page_number,
            },
            "source_context": {
                "file_name": self.source_context.file_name,
                "file_type": self.source_context.file_type,
                "page": self.source_context.page,
            },
            "semantic_primitives": self.semantic_primitives.to_dict(),
            "sequence_index": self.sequence_index,
            "component_types": self.component_types,
            "component_count": self.component_count,
            "merged_orphan": self.merged_orphan,
            "context_from_previous": self.context_from_previous,
            "overlap_context": self.overlap_context,
            "inline_semantics": self.inline_semantics,
            "duplicate_flag": self.duplicate_flag,
            "references": self.references,
            **self.metadata,
        }
        if self.quality_score is not None:
            facade["quality_score"] = self.quality_score
        if self.failure_modes:
            facade["failure_modes"] = self.failure_modes
        return facade

    # ======================================
    # convenience methods
    # ======================================

    def has_heading(self) -> bool:
        """Whether the unit has a heading."""
        return bool(self.heading_path)

    def get_heading_level(self) -> int:
        """Return the heading level."""
        return self.structural_info.heading_level

    def has_discourse_signal(self, signal_type: str) -> bool:
        """Whether the unit has a discourse signal of the given type."""
        return any(s.signal_type == signal_type for s in self.discourse_signals)

    def is_structural_block(self) -> bool:
        """Whether the unit is an unsplittable structural block (code,
        table)."""
        return self.unit_type in (UnitType.CODE, UnitType.TABLE)

    def is_separable_from(self, other: "RepresentationUnit") -> bool:
        """Whether this unit may land in a different chunk than
        ``other``.

        Decision logic:
            - same *structural* locality_scope (code/table/list) ->
              inseparable
            - explicit dependency between them -> inseparable
            - otherwise -> separable

        "paragraph" scope is not a structural block — two ordinary
        paragraphs sharing a paragraph scope stay separable, or the
        whole document would fuse into one giant chunk.
        """
        # same *structural* locality_scope = must stay together
        # (code_block / table / list_group); "paragraph" is a
        # discourse unit, not a structural block.
        _STRUCTURAL = {"code_block", "table", "list_group"}
        self_scope = self.semantic_primitives.locality_scope
        other_scope = other.semantic_primitives.locality_scope
        if (self_scope in _STRUCTURAL and self_scope == other_scope):
            return False

        # explicit dependency = must stay together
        other_id = other.semantic_primitives.identity or other.metadata.get("id", "")
        self_id = self.semantic_primitives.identity or self.metadata.get("id", "")

        if other_id and other_id in self.semantic_primitives.dependencies:
            return False
        if self_id and self_id in other.semantic_primitives.dependencies:
            return False

        return True

    def get_context_summary(self) -> str:
        """Return a context summary (for retrieval)."""
        parts = []
        if self.heading_path:
            parts.append(" > ".join(self.heading_path))
        if self.semantic_role != SemanticRole.UNKNOWN:
            parts.append(f"[{self.semantic_role.value}]")
        return " | ".join(parts) if parts else ""

    def __repr__(self) -> str:
        return f"RepresentationUnit(type={self.unit_type.value}, role={self.semantic_role.value}, text={self.text[:50]}...)"
