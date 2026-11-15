# -*- coding: utf-8 -*-
"""Retrieval Failure Benchmark — defines and detects RAG retrieval failure modes.

Core idea:
    don't test "is the chunk pretty" — test "will the chunk cause
    retrieval failure".

Failure mode taxonomy (three tiers, 12 modes, all with implemented
detectors):
    1. Structure Break: code/table/procedure/list gets chopped
    2. Context Loss: heading/section/hierarchy/reference context lost
    3. Semantic Drift: definition-example / code-follow-up /
       discourse / cross-section drift

Every failure mode:
    - has explicit detection logic
    - has a matching fix strategy
    - has test cases

Graded output:
    severity:    info / warning / error / critical (the old scale
                 critical/high/medium/low → mapped in order)
    confidence:  0.0-1.0, evidence strength — **detected ≠
                 confirmed**: only confidence >= CONFIRMED_THRESHOLD
                 counts as a confirmed failure, otherwise it is a
                 suspicion (for human review / statistics).
    blocking:    runtime circuit-breaker signal, orthogonal to
                 severity ("severe but continuable" vs "must block"
                 are separate concerns). Defaults are declared
                 centrally in FAILURE_MODE_DEFINITIONS and can be
                 overridden per instance.

Relation to quality assessment:
    quality dimensions measure "structural health", the failure
    benchmark measures "retrieval risk". They complement each
    other: a high quality score ≠ no failures, but a low one
    usually means more failures.
"""

from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Literal,
    NotRequired,
    Optional,
    TypedDict,
)

Severity = Literal["critical", "error", "warning", "info"]


class _ModeDefinition(TypedDict):
    """Entry shape for the single source of truth (severity/blocking consumed by _instance())."""

    description: str
    detection: str
    severity: Severity
    blocking: NotRequired[bool]
    example: str
    fix: str


class FailureMode(Enum):
    """Retrieval failure mode enum.

    Design principle:
        - these are not aesthetic judgments of "is the chunk pretty"
        - they are functional judgments of "will the chunk cause
          retrieval failure"
        - every failure mode has explicit detection logic and a fix
          strategy

    Three tiers:
        1. Structure Break: code/table/steps chopped
        2. Context Loss: heading/section/reference context lost
        3. Semantic Drift: definition-example / explanation-code
           separated
    """
    # === Structure Break ===
    CODE_EXPLANATION_SPLIT = "code_explanation_split"
    TABLE_DESCRIPTION_SPLIT = "table_description_split"
    PROCEDURE_STEP_SPLIT = "procedure_step_split"
    LIST_ITEM_SPLIT = "list_item_split"

    # === Context Loss ===
    HEADING_AMBIGUITY = "heading_ambiguity"
    SECTION_CONTEXT_LOSS = "section_context_loss"
    CROSS_REFERENCE_BREAK = "cross_reference_break"
    HIERARCHY_AMBIGUITY = "hierarchy_ambiguity"

    # === Semantic Drift ===
    DEFINITION_EXAMPLE_SPLIT = "definition_example_split"
    EXPLANATION_CODE_SPLIT = "explanation_code_split"
    DISCOURSE_SPLIT = "discourse_split"
    SEMANTIC_DRIFT = "semantic_drift"


# ======================================
# Grading scale and thresholds (single source of truth)
# ======================================

SEVERITY_LEVELS = ("critical", "error", "warning", "info")
"""分级刻度（严重→轻）。

旧刻度映射: critical→critical, high→error, medium→warning, low→info。
"""

CONFIRMED_THRESHOLD = 0.6
"""detected ≠ confirmed：confidence 达到该阈值才算 confirmed failure。
低于阈值的是 suspicion——检测器有信号但证据弱（如启发式 marker
匹配），只能作为统计/复核信号，不应直接阻断或计入硬性惩罚。"""


@dataclass
class FailureInstance:
    """A single failure instance.

    detected ≠ confirmed: only ``confidence >= CONFIRMED_THRESHOLD``
    counts as a confirmed failure (the confirmed property);
    low-confidence instances are suspicions that still appear in the
    failures list (for statistics), but are excluded from
    confirmed_failures / blocking_failures.
    """
    mode: FailureMode
    severity: Literal["critical", "error", "warning", "info"]
    description: str
    affected_chunks: List[int]  # chunk indices
    evidence: Dict[str, Any]  # 检测证据
    fix_strategy: str  # 建议修复策略
    confidence: float = 1.0  # 证据强度 [0.0, 1.0]
    blocking: bool = False  # 运行时熔断信号（独立于 severity）

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"FailureInstance.confidence must be in [0.0, 1.0], "
                f"got {self.confidence!r}"
            )
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(
                f"FailureInstance.severity must be one of "
                f"{SEVERITY_LEVELS}, got {self.severity!r}"
            )

    @property
    def confirmed(self) -> bool:
        """Confirmed failure only when confidence reaches the threshold; otherwise a suspicion."""
        return self.confidence >= CONFIRMED_THRESHOLD


@dataclass
class FailureReport:
    """Complete failure report."""
    document_id: str
    total_chunks: int
    failures: List[FailureInstance]
    summary: Dict[str, int]  # mode -> count
    risk_score: float  # 0-1, 综合风险分数

    def has_critical(self) -> bool:
        """Whether a severity=critical AND confirmed failure instance exists."""
        return any(f.severity == "critical" and f.confirmed for f in self.failures)

    def confirmed_failures(self) -> List[FailureInstance]:
        """All confirmed instances (confidence >= CONFIRMED_THRESHOLD)."""
        return [f for f in self.failures if f.confirmed]

    def suspected_failures(self) -> List[FailureInstance]:
        """All suspected instances (insufficient evidence; statistics / human review only)."""
        return [f for f in self.failures if not f.confirmed]

    def blocking_failures(self) -> List[FailureInstance]:
        """Confirmed AND blocking instances — runtime circuit-breaker candidates."""
        return [f for f in self.failures if f.blocking and f.confirmed]

    def by_mode(self, mode: FailureMode) -> List[FailureInstance]:
        """Filter instances by failure mode."""
        return [f for f in self.failures if f.mode == mode]


# ======================================
# Failure mode definitions and detection logic
# ======================================

# Single source of truth for severity/blocking defaults (consumed by
# detectors via _instance(), never hardcoded per detector — prevents
# drift). Old scale mapping: critical→critical, high→error,
# medium→warning, low→info.

FAILURE_MODE_DEFINITIONS: Dict[FailureMode, _ModeDefinition] = {
    FailureMode.CODE_EXPLANATION_SPLIT: {
        "description": "代码块和前置/后置解释文本被分到不同 chunk",
        "detection": "相邻 chunk 中，一个以代码块结尾，下一个以解释文本开头（或反之）",
        "severity": "critical",
        "blocking": True,  # 代码执行语义断裂：可能影响安全/执行语义
        "example": "'以下是排序算法实现：' 在 chunk A，代码块在 chunk B",
        "fix": "将代码块和紧邻的解释文本合并到同一 chunk",
    },
    FailureMode.DEFINITION_EXAMPLE_SPLIT: {
        "description": "定义/概念和示例/用例被切断",
        "detection": "包含 '例如'/'比如'/'for example' 的段落被切分到两个 chunk",
        "severity": "error",
        "blocking": False,
        "example": "'递归函数是指...' 在 chunk A，'例如，计算阶乘...' 在 chunk B",
        "fix": "检测 explanation markers，避免在 marker 附近切分",
    },
    FailureMode.HEADING_AMBIGUITY: {
        "description": "heading 脱离上下文后语义不明确",
        "detection": "chunk 只包含 heading 文本，没有足够上下文说明其含义",
        "severity": "warning",
        "blocking": False,
        "example": "chunk 只包含 '## 配置'，没有说明是什么的配置",
        "fix": "heading chunk 必须携带父 heading path 和至少一段说明文本",
    },
    FailureMode.SECTION_CONTEXT_LOSS: {
        "description": "section 内容缺少 heading 上下文",
        "detection": "chunk 包含 section 内容，但 heading_path 不完整或缺失",
        "severity": "error",  # 解析证据丢失
        "blocking": False,
        "example": "chunk 包含 '设置 timeout 为 30 秒'，但不知道属于哪个 section",
        "fix": "确保所有 chunk 都有完整的 heading_path",
    },
    FailureMode.CROSS_REFERENCE_BREAK: {
        "description": "跨引用关系被切断",
        "detection": "chunk 包含 '参见第 X 节'/'详见上文' 等引用，但目标不在同一 chunk",
        "severity": "warning",
        "blocking": False,
        "example": "'详见 3.2 节的安全配置' 在 chunk A，3.2 节内容在 chunk B",
        "fix": "检测 cross-reference 信号，优先将引用目标合并",
    },
    FailureMode.PROCEDURE_STEP_SPLIT: {
        "description": "步骤列表被切断",
        "detection": "有序列表（1. 2. 3.）被分到多个 chunk",
        "severity": "error",
        "blocking": False,
        "example": "'步骤 1：下载安装包' 在 chunk A，'步骤 2：运行安装程序' 在 chunk B",
        "fix": "步骤列表必须保持完整，或至少保持步骤连续性",
    },
    FailureMode.TABLE_DESCRIPTION_SPLIT: {
        "description": "表格和描述文本分离",
        "detection": "表格 chunk 缺少表头说明或表格后的解释文本",
        "severity": "warning",
        "blocking": False,
        "example": "表格 chunk 只有数据，没有 '表 1：用户角色权限' 的说明",
        "fix": "表格必须携带标题/描述，或确保描述文本在同一 chunk",
    },
    FailureMode.LIST_ITEM_SPLIT: {
        "description": "无序列表被切断",
        "detection": "相邻 chunk 中，前一个末行与后一个首行同为列表项且标记一致（- / * / + / •）",
        "severity": "error",
        "blocking": False,
        "example": "'- 检查网络连接' 在 chunk A，'- 重启服务' 在 chunk B",
        "fix": "列表整体保持在一个 chunk，或在列表边界切分",
    },
    FailureMode.HIERARCHY_AMBIGUITY: {
        "description": "子 heading 缺少父 heading 上下文",
        "detection": "chunk 包含 level>1 heading 但 heading_path 深度不足",
        "severity": "warning",
        "blocking": False,
        "example": "chunk 只有 '### 网络配置'，heading_path 丢失 '## 部署' 等父级",
        "fix": "确保 chunk 携带完整的 heading_path",
    },
    FailureMode.EXPLANATION_CODE_SPLIT: {
        "description": "代码块与其后置解释分离",
        "detection": "chunk 以代码块结尾，下一个 chunk 以 '上述代码'/'该函数' 等指代开头",
        "severity": "error",
        "blocking": False,
        "example": "代码块在 chunk A，'上述代码首先…' 在 chunk B",
        "fix": "将代码块与紧随的解释文本合并到同一 chunk",
    },
    FailureMode.DISCOURSE_SPLIT: {
        "description": "话语连贯性被切断（承接/指代标记跨 chunk）",
        "detection": "承接/指代 marker（随后/然后/因此等）位于 chunk 末尾，延续内容在下一 chunk",
        "severity": "error",
        "blocking": False,
        "example": "'随后路由到编排层' 在 chunk A，编排层细节在 chunk B",
        "fix": "避免在 discourse marker 附近切分",
    },
    FailureMode.SEMANTIC_DRIFT: {
        "description": "chunk 内容跨越 section 边界，前段内容失去所属上下文",
        "detection": "chunk 首个 heading 出现在非首行（heading 之前还有正文）",
        "severity": "warning",  # 可能的话题跳转
        "blocking": False,
        "example": "chunk 以部署小节结尾内容开头，中途出现 '# 监控告警' 新小节",
        "fix": "在 section 边界切分，不要把两个 section 的内容并入同一 chunk",
    },
}


def _instance(
    mode: FailureMode,
    description: str,
    affected_chunks: List[int],
    evidence: Dict[str, Any],
    confidence: float,
    fix_strategy: str = "",
    severity: Optional[Severity] = None,
    blocking: Optional[bool] = None,
) -> FailureInstance:
    """Centralized FailureInstance construction: severity/blocking defaults come from
    FAILURE_MODE_DEFINITIONS (single source of truth, no drift), overridable per instance."""
    definition = FAILURE_MODE_DEFINITIONS[mode]
    return FailureInstance(
        mode=mode,
        severity=severity if severity is not None else definition["severity"],
        description=description,
        affected_chunks=affected_chunks,
        evidence=evidence,
        fix_strategy=fix_strategy or definition["fix"],
        confidence=confidence,
        blocking=blocking if blocking is not None else definition.get("blocking", False),
    )


# ======================================
# Detector interface
# ======================================

class FailureDetector:
    """Base class for retrieval failure detectors."""

    def detect(self, chunks: List[Any], document_text: str = "") -> List[FailureInstance]:
        """Detect failure instances.

        Args:
            chunks: list of Chunks
            document_text: original document text (for context verification)

        Returns:
            List[FailureInstance]
        """
        raise NotImplementedError


class CodeExplanationSplitDetector(FailureDetector):
    """Detects code block / explanation text separation."""

    def detect(self, chunks, document_text=""):
        """Detect code-explanation separation (preceding explanation → code shape)."""
        failures = []
        for i in range(len(chunks) - 1):
            curr = chunks[i]
            next_chunk = chunks[i + 1]

            # Detection: current chunk ends with explanation text, next chunk starts with a code block
            curr_text = curr.text if hasattr(curr, 'text') else str(curr)
            next_text = next_chunk.text if hasattr(next_chunk, 'text') else str(next_chunk)

            curr_has_code = curr_text.strip().endswith("```") or "```" in curr_text[-100:]
            next_has_code = next_text.strip().startswith("```") or "```" in next_text[:100]

            # explanation-text features
            explanation_markers = ["如下", "示例", "实现", "代码", "如下所示"]
            curr_has_explanation = any(m in curr_text[-100:] for m in explanation_markers)

            if curr_has_explanation and next_has_code and not curr_has_code:
                failures.append(_instance(
                    mode=FailureMode.CODE_EXPLANATION_SPLIT,
                    description=f"chunk {i} 包含解释文本，chunk {i+1} 包含代码块",
                    affected_chunks=[i, i + 1],
                    evidence={
                        "explanation_text": curr_text[-100:],
                        "code_start": next_text[:100],
                    },
                    confidence=0.8,  # 代码围栏属结构性证据
                    fix_strategy="将代码块和前置解释文本合并",
                ))

        return failures


class DefinitionExampleSplitDetector(FailureDetector):
    """Detects definition-example separation."""

    EXPLANATION_MARKERS = [
        "例如", "比如", "举例来说", "如下示例", "for example",
        "e.g.", "举例", "示例", "比如", "比方说",
    ]

    def detect(self, chunks, document_text=""):
        """Detect definition-example separation (marker-at-chunk-tail and example-heads-next-chunk shapes)."""
        failures = []
        for i in range(len(chunks) - 1):
            curr = chunks[i]
            next_chunk = chunks[i + 1]

            curr_text = curr.text if hasattr(curr, 'text') else str(curr)
            next_text = next_chunk.text if hasattr(next_chunk, 'text') else str(next_chunk)

            # check whether a marker got split
            for marker in self.EXPLANATION_MARKERS:
                # marker at the end of the current chunk, but the example content is in the next chunk
                if marker in curr_text[-50:] and len(curr_text) < 300:
                    # the next chunk starts with example content (loosened detection)
                    next_start = next_text[:100].strip()
                    is_example = (
                        any(c.isdigit() for c in next_start) or
                        "```" in next_start or
                        "比如" in next_start or
                        "例如" in next_start or
                        len(next_text) > 10  # 只要有内容就算
                    )
                    if is_example:
                        failures.append(_instance(
                            mode=FailureMode.DEFINITION_EXAMPLE_SPLIT,
                            description=f"'{marker}' 在 chunk {i}，示例内容在 chunk {i+1}",
                            affected_chunks=[i, i + 1],
                            evidence={
                                "marker": marker,
                                "context_before": curr_text[-100:],
                                "context_after": next_text[:100],
                            },
                            confidence=0.5,  # marker 启发式，判定条件较宽松
                            fix_strategy="避免在 explanation marker 附近切分",
                        ))
                        break

        # The other half of definition-example separation: the
        # definition stays in the current chunk and the next chunk
        # starts with "例如，…". Previously only the
        # marker-at-previous-chunk-tail shape was checked, missing
        # this more common shape.
        definition_markers = ("是指", "指的是", "定义为", "定义是", "称为", "称作", "是一种", "是一个")
        for i in range(len(chunks) - 1):
            curr = chunks[i]
            next_chunk = chunks[i + 1]
            curr_text = curr.text if hasattr(curr, 'text') else str(curr)
            next_text = next_chunk.text if hasattr(next_chunk, 'text') else str(next_chunk)
            if any(f.mode == FailureMode.DEFINITION_EXAMPLE_SPLIT and f.affected_chunks[0] == i
                   for f in failures):
                continue
            next_head = next_text[:20]
            for marker in ("例如", "比如", "举例来说", "举例", "比方说"):
                if next_head.startswith(marker) and "```" not in curr_text:
                    if any(m in curr_text for m in definition_markers):
                        failures.append(_instance(
                            mode=FailureMode.DEFINITION_EXAMPLE_SPLIT,
                            description=f"定义在 chunk {i}，'{marker}' 开头的示例在 chunk {i+1}",
                            affected_chunks=[i, i + 1],
                            evidence={
                                "context_before": curr_text[-100:],
                                "example_start": next_text[:100],
                            },
                            confidence=0.7,  # 定义 marker + 示例开头双重证据
                            fix_strategy="将定义句与其示例合并到同一 chunk",
                        ))
                        break

        return failures


class HeadingAmbiguityDetector(FailureDetector):
    """Detects headings detached from their context."""

    def detect(self, chunks, document_text=""):
        """Detect isolated heading chunks (heading only, no heading_path context)."""
        failures = []
        for i, chunk in enumerate(chunks):
            text = chunk.text if hasattr(chunk, 'text') else str(chunk)
            metadata = chunk.metadata if hasattr(chunk, 'metadata') else {}

            # check whether it contains only a heading
            lines = text.strip().split('\n')
            if len(lines) == 1 and lines[0].startswith('#'):
                # check for a heading_path
                heading_path = metadata.get('heading_path', [])
                if not heading_path or len(heading_path) < 2:
                    failures.append(_instance(
                        mode=FailureMode.HEADING_AMBIGUITY,
                        description=f"chunk {i} 只包含孤立 heading: {lines[0][:50]}",
                        affected_chunks=[i],
                        evidence={
                            "heading": lines[0],
                            "heading_path": heading_path,
                            "chunk_length": len(text),
                        },
                        confidence=0.8,  # 单行 heading 结构性证据
                        fix_strategy="heading chunk 必须携带父 heading path 和说明文本",
                    ))

        return failures


class SectionContextLossDetector(FailureDetector):
    """Detects section content lacking heading context."""

    def detect(self, chunks, document_text=""):
        """Detect chunks that have content but lack heading context."""
        failures = []
        for i, chunk in enumerate(chunks):
            text = chunk.text if hasattr(chunk, 'text') else str(chunk)
            metadata = chunk.metadata if hasattr(chunk, 'metadata') else {}

            heading_path = metadata.get('heading_path', [])
            heading = metadata.get('heading', '')

            # a content chunk without heading context
            if len(text) > 100 and not heading_path and not heading:
                failures.append(_instance(
                    mode=FailureMode.SECTION_CONTEXT_LOSS,
                    description=f"chunk {i} 有内容但缺少 heading 上下文",
                    affected_chunks=[i],
                    evidence={
                        "chunk_length": len(text),
                        "heading_path": heading_path,
                        "heading": heading,
                        "content_preview": text[:100],
                    },
                    confidence=0.8,  # metadata 缺失属结构性证据
                    fix_strategy="确保所有内容 chunk 都有 heading_path",
                ))

        return failures


class ProcedureStepSplitDetector(FailureDetector):
    """Detects step lists getting cut."""

    STEP_PATTERNS = [
        r'^步骤\s*\d+',
        r'^\d+\.\s+',
        r'^Step\s*\d+',
    ]

    STEP_NUMBER_PATTERN = r"(?:步骤\s*|Step\s*|^)((?:\d+\.)+\d+|\d+)"

    @staticmethod
    def _step_tuple(number: str) -> tuple:
        return tuple(int(part) for part in number.split("."))

    def detect(self, chunks, document_text=""):
        """Detect ordered step-number continuity breaking at chunk boundaries."""
        import re
        failures = []

        for i in range(len(chunks) - 1):
            curr = chunks[i]
            next_chunk = chunks[i + 1]

            curr_text = curr.text if hasattr(curr, 'text') else str(curr)
            next_text = next_chunk.text if hasattr(next_chunk, 'text') else str(next_chunk)

            # check whether the current chunk ends with a step
            curr_lines = curr_text.strip().split('\n')
            last_line = curr_lines[-1] if curr_lines else ""

            # check whether the next chunk starts with a step
            next_lines = next_text.strip().split('\n')
            first_line = next_lines[0] if next_lines else ""

            # Detect step-number continuity breaks. Numbers can be
            # plain integers ("2"→"3") or nested decimal numbers
            # ("3.2"→"3.3"): same depth, same prefix, last component
            # exactly +1 → a cut continuous step sequence.
            curr_step_match = re.search(self.STEP_NUMBER_PATTERN, last_line)
            next_step_match = re.search(self.STEP_NUMBER_PATTERN, first_line)

            if curr_step_match and next_step_match:
                curr_number = curr_step_match.group(1)
                next_number = next_step_match.group(1)
                curr_parts = self._step_tuple(curr_number)
                next_parts = self._step_tuple(next_number)
                if (
                    len(curr_parts) == len(next_parts)
                    and curr_parts[:-1] == next_parts[:-1]
                    and next_parts[-1] == curr_parts[-1] + 1
                ):
                    failures.append(_instance(
                        mode=FailureMode.PROCEDURE_STEP_SPLIT,
                        description=f"步骤 {curr_number} 和 {next_number} 被分到不同 chunk",
                        affected_chunks=[i, i + 1],
                        evidence={
                            "step_before": last_line[:100],
                            "step_after": first_line[:100],
                        },
                        confidence=0.9,  # 编号连续性是强结构性证据
                        fix_strategy="步骤列表应保持完整或在步骤边界切分",
                    ))

        return failures


# ======================================
# Cross Reference Break / Table Description Split detectors
# ======================================

class CrossReferenceBreakDetector(FailureDetector):
    """Detects cross-reference breaks.

    Detection logic: a "参见 X.Y 节"/"详见 X.Y" reference in a chunk
    whose numbered target (a line in the "X.Y 标题" shape) is not in
    the same chunk. A target in another chunk means the reference
    was cut; a target absent from the whole chunk sequence is a
    dangling reference — both mean the reference cannot be resolved
    near where it is retrieved.
    """

    REFERENCE_PATTERNS = [
        r"参见\s*(\d+(?:\.\d+)*)\s*节",
        r"详见\s*(\d+(?:\.\d+)*)",
        r"见\s*第\s*(\d+(?:\.\d+)*)\s*节",
    ]
    SECTION_LINE_PATTERN = r"(?:^|\n)\s*(\d+(?:\.\d+)+)\s*[\s、．.：:]"

    def detect(self, chunks, document_text=""):
        """Detect cross-reference breaks (numbered targets across chunks or dangling)."""
        import re
        failures = []

        texts = [c.text if hasattr(c, 'text') else str(c) for c in chunks]
        targets: Dict[str, int] = {}
        for index, text in enumerate(texts):
            for match in re.finditer(self.SECTION_LINE_PATTERN, text):
                targets.setdefault(match.group(1), index)

        for i, text in enumerate(texts):
            for pattern in self.REFERENCE_PATTERNS:
                for match in re.finditer(pattern, text):
                    ref = match.group(1)
                    if ref in targets and targets[ref] == i:
                        continue  # 引用目标就在同一 chunk，无断裂
                    if ref in targets:
                        failures.append(_instance(
                            mode=FailureMode.CROSS_REFERENCE_BREAK,
                            description=(
                                f"chunk {i} 引用 {ref} 节，"
                                f"但目标内容在 chunk {targets[ref]}"
                            ),
                            affected_chunks=[i, targets[ref]],
                            evidence={
                                "reference": match.group(0),
                                "reference_number": ref,
                                "target_chunk": targets[ref],
                            },
                            confidence=0.8,  # 编号引用与目标行匹配属结构性证据
                            fix_strategy="将引用句与目标小节合并，或在 chunk 中携带目标摘要",
                        ))
                    else:
                        failures.append(_instance(
                            mode=FailureMode.CROSS_REFERENCE_BREAK,
                            description=(
                                f"chunk {i} 引用 {ref} 节，但目标在所有 chunk 中均不可见"
                            ),
                            affected_chunks=[i],
                            evidence={
                                "reference": match.group(0),
                                "reference_number": ref,
                                "target_chunk": None,
                            },
                            confidence=0.5,  # 悬空引用：目标可能只是未被 chunk 化
                            fix_strategy="引用目标缺失时在 chunk 内补目标上下文摘要",
                        ))
        return failures


class TableDescriptionSplitDetector(FailureDetector):
    """Detects table/description/footnote separation.

    Detection logic: when a markdown table chunk (containing the
    ``|---|`` separator row) itself lacks a "表 N" caption, check
    the neighbors: a table caption at the end of the previous chunk,
    or the next chunk starting with a table footnote ("注："), both
    mean descriptive context was split from the table.
    """

    TABLE_MARKER = "|---"
    TITLE_PATTERN = r"表\s*\d+\s*[：:]"
    NOTES_PREFIXES = ("注：", "注:", "备注：", "备注:", "说明：", "说明:")

    def detect(self, chunks, document_text=""):
        """Detect tables split from their caption/footnote into different chunks."""
        import re
        failures = []

        texts = [c.text if hasattr(c, 'text') else str(c) for c in chunks]
        for i, text in enumerate(texts):
            if self.TABLE_MARKER not in text:
                continue
            if re.search(self.TITLE_PATTERN, text):
                continue  # 标题随表格在同一 chunk，无分离

            if i > 0:
                previous_tail = texts[i - 1][-120:]
                if re.search(self.TITLE_PATTERN, previous_tail) and self.TABLE_MARKER not in previous_tail:
                    failures.append(_instance(
                        mode=FailureMode.TABLE_DESCRIPTION_SPLIT,
                        description=f"表格在 chunk {i}，其标题留在 chunk {i-1}",
                        affected_chunks=[i - 1, i],
                        evidence={
                            "title_context": previous_tail,
                            "table_preview": text[:100],
                        },
                        confidence=0.8,  # 表标题模式 + 相邻表格行双重证据
                        fix_strategy="将表标题与表格数据合并到同一 chunk",
                    ))

            if i < len(texts) - 1:
                next_head = texts[i + 1][:60].strip()
                if next_head.startswith(self.NOTES_PREFIXES):
                    failures.append(_instance(
                        mode=FailureMode.TABLE_DESCRIPTION_SPLIT,
                        description=f"表格在 chunk {i}，其脚注落在 chunk {i+1}",
                        affected_chunks=[i, i + 1],
                        evidence={
                            "table_tail": text[-100:],
                            "notes_start": texts[i + 1][:100],
                        },
                        confidence=0.7,  # 脚注前缀模式匹配
                        fix_strategy="将表格与脚注（\"注：…\"）合并到同一 chunk",
                    ))
        return failures


# ======================================
# List Item Split / Explanation-Code Split / Semantic Drift detectors
# ======================================

class ListItemSplitDetector(FailureDetector):
    """Detects unordered lists getting cut.

    Detection logic: among adjacent chunks, the previous chunk's
    last non-empty line and the next chunk's first non-empty line
    are both list items with the same marker (- / * / + / •).
    Complements ProcedureStepSplitDetector's ordered-number
    continuity check, covering the unordered-list shape.
    """

    BULLET_PATTERN = r"^\s*([-*+•])\s+\S"

    def detect(self, chunks, document_text=""):
        """Detect unordered lists cut at chunk boundaries (last/first lines both same-marker items)."""
        import re
        failures = []

        for i in range(len(chunks) - 1):
            curr = chunks[i]
            next_chunk = chunks[i + 1]

            curr_text = curr.text if hasattr(curr, 'text') else str(curr)
            next_text = next_chunk.text if hasattr(next_chunk, 'text') else str(next_chunk)

            curr_lines = [line for line in curr_text.strip().split('\n') if line.strip()]
            next_lines = [line for line in next_text.strip().split('\n') if line.strip()]
            if not curr_lines or not next_lines:
                continue

            curr_match = re.match(self.BULLET_PATTERN, curr_lines[-1])
            next_match = re.match(self.BULLET_PATTERN, next_lines[0])
            if (
                curr_match and next_match
                and curr_match.group(1) == next_match.group(1)
            ):
                failures.append(_instance(
                    mode=FailureMode.LIST_ITEM_SPLIT,
                    description=(
                        f"列表项 '{curr_lines[-1][:30]}' 在 chunk {i}，"
                        f"'{next_lines[0][:30]}' 在 chunk {i+1}"
                    ),
                    affected_chunks=[i, i + 1],
                    evidence={
                        "bullet_marker": curr_match.group(1),
                        "item_before": curr_lines[-1][:100],
                        "item_after": next_lines[0][:100],
                    },
                    confidence=0.9,  # 列表标记一致是强结构性证据
                    fix_strategy="列表整体保持在一个 chunk，或在列表边界切分",
                ))

        return failures


class ExplanationCodeSplitDetector(FailureDetector):
    """Detects code blocks separated from their following explanation.

    Detection logic: the chunk ends with a closing code fence and
    the next chunk starts with a back-reference marker like
    '上述代码'/'该函数'. Complements CodeExplanationSplitDetector:
    that one covers the "preceding explanation → code" shape, this
    one covers the "code → following explanation" shape.
    """

    ANAPHORA_MARKERS = (
        "上述代码", "上述实现", "上述函数", "上述逻辑",
        "上面的代码", "上面的实现", "上面的函数",
        "该代码", "该函数", "该实现", "该段代码", "这段代码",
        "以上代码", "前述代码",
        "as shown above", "the code above", "the above code",
    )

    def detect(self, chunks, document_text=""):
        """Detect the code → following anaphoric explanation separation shape."""
        failures = []

        for i in range(len(chunks) - 1):
            curr = chunks[i]
            next_chunk = chunks[i + 1]

            curr_text = curr.text if hasattr(curr, 'text') else str(curr)
            next_text = next_chunk.text if hasattr(next_chunk, 'text') else str(next_chunk)

            ends_with_code = curr_text.rstrip().endswith("```")
            next_head = next_text.lstrip()[:16]

            if ends_with_code and next_head.startswith(self.ANAPHORA_MARKERS):
                failures.append(_instance(
                    mode=FailureMode.EXPLANATION_CODE_SPLIT,
                    description=(
                        f"代码块在 chunk {i} 结尾，其回指解释从 chunk {i+1} 开头"
                    ),
                    affected_chunks=[i, i + 1],
                    evidence={
                        "code_tail": curr_text[-100:],
                        "explanation_start": next_text[:100],
                    },
                    confidence=0.9,  # 代码围栏结尾 + 回指标记双重结构性证据
                    fix_strategy="将代码块与紧随的解释文本合并到同一 chunk",
                ))

        return failures


class SemanticDriftDetector(FailureDetector):
    """Detects chunk content crossing a section boundary (semantic drift).

    Detection logic: the chunk's first markdown heading appears on
    a non-first line — body text precedes the heading, meaning the
    chunk begins with the previous section's content and switches
    to a new section midway; the earlier part loses its owning
    context at retrieval time. A heading on the first line is the
    normal shape and is not reported.
    """

    HEADING_PATTERN = r"^\s{0,3}(#{1,6})\s+\S"

    def detect(self, chunks, document_text=""):
        """Detect chunks crossing section boundaries (first heading on a non-first line)."""
        import re
        failures = []

        for i, chunk in enumerate(chunks):
            text = chunk.text if hasattr(chunk, 'text') else str(chunk)
            metadata = chunk.metadata if hasattr(chunk, 'metadata') else {}

            in_code_fence = False
            for line_index, line in enumerate(text.split('\n')):
                if line.lstrip().startswith("```"):
                    in_code_fence = not in_code_fence
                    continue
                if in_code_fence:
                    continue  # 代码块内的 '#' 注释不是 heading
                if re.match(self.HEADING_PATTERN, line):
                    if line_index >= 1:
                        failures.append(_instance(
                            mode=FailureMode.SEMANTIC_DRIFT,
                            description=(
                                f"chunk {i} 首个 heading 出现在第 {line_index + 1} 行，"
                                f"之前的内容属于上一节"
                            ),
                            affected_chunks=[i],
                            evidence={
                                "heading_line": line[:100],
                                "heading_line_index": line_index,
                                "content_before": text.split('\n')[0][:100],
                                "heading_path": metadata.get('heading_path', []),
                            },
                            confidence=0.7,  # heading 位置证据，但合法小节开头也可能误报
                            fix_strategy="在 section 边界切分，不要把两个 section 的内容并入同一 chunk",
                        ))
                    break  # 只看首个 heading：heading 在首行的多 section chunk 不算漂移

        return failures


# ======================================
# Standalone detection functions (for direct use by tests)
# ======================================

def detect_code_explanation_split(chunks: List[Any]) -> List[FailureInstance]:
    """Detect code-explanation separation."""
    return CodeExplanationSplitDetector().detect(chunks)


def detect_definition_example_split(chunks: List[Any]) -> List[FailureInstance]:
    """Detect definition-example separation."""
    return DefinitionExampleSplitDetector().detect(chunks)


def detect_heading_ambiguity(chunks: List[Any]) -> List[FailureInstance]:
    """Detect heading ambiguity."""
    return HeadingAmbiguityDetector().detect(chunks)


def detect_section_context_loss(chunks: List[Any]) -> List[FailureInstance]:
    """Detect section context loss."""
    return SectionContextLossDetector().detect(chunks)


def detect_procedure_step_split(chunks: List[Any]) -> List[FailureInstance]:
    """Detect procedure step splits."""
    return ProcedureStepSplitDetector().detect(chunks)


def detect_cross_reference_break(chunks: List[Any]) -> List[FailureInstance]:
    """Detect cross-reference breaks."""
    return CrossReferenceBreakDetector().detect(chunks)


def detect_table_description_split(chunks: List[Any]) -> List[FailureInstance]:
    """Detect table-description separation."""
    return TableDescriptionSplitDetector().detect(chunks)


def detect_list_item_split(chunks: List[Any]) -> List[FailureInstance]:
    """Detect unordered list splits."""
    return ListItemSplitDetector().detect(chunks)


def detect_hierarchy_ambiguity(chunks: List[Any]) -> List[FailureInstance]:
    """Detect hierarchy ambiguity."""
    return HierarchyAmbiguityDetector().detect(chunks)


def detect_explanation_code_split(chunks: List[Any]) -> List[FailureInstance]:
    """Detect code-following-explanation separation."""
    return ExplanationCodeSplitDetector().detect(chunks)


def detect_discourse_split(chunks: List[Any]) -> List[FailureInstance]:
    """Detect discourse continuity breaks."""
    return DiscourseSplitDetector().detect(chunks)


def detect_semantic_drift(chunks: List[Any]) -> List[FailureInstance]:
    """Detect cross-section semantic drift."""
    return SemanticDriftDetector().detect(chunks)


# ======================================
# Discourse Split Detector
# ======================================

class DiscourseSplitDetector(FailureDetector):
    """Detects discourse continuity being cut.

    Detection logic:
        - a paragraph containing an explanation marker (例如/因此) is split across two chunks
        - a step containing a continuation signal (接着/然后) is cut
        - a paragraph containing anaphora (它/上述) is separated from its antecedent
    """

    DISCOURSE_MARKERS = [
        "例如", "比如", "举例来说", "因此", "所以", "因而",
        "接着", "然后", "接下来", "随后", "最后", "最终",
        "此外", "另外", "而且", "并且", "同时",
        "但是", "然而", "不过", "尽管如此",
        "for example", "e.g.", "therefore", "thus", "hence",
        "then", "next", "afterwards", "finally", "moreover",
        "however", "but", "nevertheless", "although",
    ]

    def detect(self, chunks, document_text=""):
        """Detect discourse markers at a chunk's end with the continuation landing in the next chunk."""
        failures = []
        for i in range(len(chunks) - 1):
            curr = chunks[i]
            next_chunk = chunks[i + 1]

            curr_text = curr.text if hasattr(curr, 'text') else str(curr)
            next_text = next_chunk.text if hasattr(next_chunk, 'text') else str(next_chunk)

            # check whether a discourse marker got split
            for marker in self.DISCOURSE_MARKERS:
                # marker at the end of the current chunk, the continuation in the next chunk
                if marker in curr_text[-50:] and len(curr_text) < 200:
                    # the next chunk contains the continuation
                    if len(next_text) > 20:
                        failures.append(_instance(
                            mode=FailureMode.DISCOURSE_SPLIT,
                            description=f"'{marker}' 在 chunk {i}，延续内容在 chunk {i+1}",
                            affected_chunks=[i, i + 1],
                            evidence={
                                "marker": marker,
                                "context_before": curr_text[-100:],
                                "context_after": next_text[:100],
                            },
                            confidence=0.5,  # marker 启发式，误报可能高（句子内正常承接）
                            fix_strategy="避免在 discourse marker 附近切分",
                        ))
                        break

        return failures


# ======================================
# Hierarchy Ambiguity Detector
# ======================================

class HierarchyAmbiguityDetector(FailureDetector):
    """Detects hierarchy ambiguity.

    Detection logic:
        - the chunk contains a child heading but no parent heading context
        - e.g. only "### 配置" without "## 系统设置"
    """

    def detect(self, chunks, document_text=""):
        """Detect child headings lacking parent heading_path context."""
        failures = []
        for i, chunk in enumerate(chunks):
            text = chunk.text if hasattr(chunk, 'text') else str(chunk)
            metadata = chunk.metadata if hasattr(chunk, 'metadata') else {}

            # check heading levels
            heading_path = metadata.get('heading_path', [])
            lines = text.strip().split('\n')

            # chunks containing only an isolated heading are the
            # responsibility of HeadingAmbiguityDetector, avoiding
            # the same problem being reported as two modes.
            if len([line for line in lines if line.strip()]) <= 1:
                continue

            # find the deepest heading in the chunk
            deepest_level = 0
            for line in lines:
                if line.startswith('#'):
                    level = len(line) - len(line.lstrip('#'))
                    deepest_level = max(deepest_level, level)

            # if heading_path length < deepest_level, parent context is missing
            if deepest_level > 1 and len(heading_path) < deepest_level - 1:
                failures.append(_instance(
                    mode=FailureMode.HIERARCHY_AMBIGUITY,
                    description=f"chunk {i} 包含 level {deepest_level} heading 但 heading_path 不完整",
                    affected_chunks=[i],
                    evidence={
                        "heading_path": heading_path,
                        "deepest_level": deepest_level,
                        "content_preview": text[:100],
                    },
                    confidence=0.8,  # heading 层级与 metadata 深度比对属结构性证据
                    fix_strategy="确保 chunk 携带完整的 heading_path",
                ))

        return failures


# ======================================
# Unified detection entry point
# ======================================

class RetrievalFailureBenchmark:
    """Retrieval failure benchmark entry point.

    Three-tier detection system:
        1. Structure Break: code/table/steps/lists chopped
        2. Context Loss: heading/section/hierarchy/reference lost
        3. Semantic Drift: definition-example/discourse/semantic drift
    """

    DETECTORS = [
        # Structure Break
        CodeExplanationSplitDetector(),
        ProcedureStepSplitDetector(),
        TableDescriptionSplitDetector(),
        ListItemSplitDetector(),

        # Context Loss
        HeadingAmbiguityDetector(),
        SectionContextLossDetector(),
        HierarchyAmbiguityDetector(),
        CrossReferenceBreakDetector(),

        # Semantic Drift
        DefinitionExampleSplitDetector(),
        ExplanationCodeSplitDetector(),
        DiscourseSplitDetector(),
        SemanticDriftDetector(),
    ]

    def run(self, chunks: List[Any], document_id: str = "", document_text: str = "") -> FailureReport:
        """Run the full detection.

        Args:
            chunks: list of Chunks
            document_id: document identifier
            document_text: original document text (optional)

        Returns:
            FailureReport
        """
        all_failures = []
        for detector in self.DETECTORS:
            failures = detector.detect(chunks, document_text)
            all_failures.extend(failures)

        # Risk score: weight only confirmed instances (confidence >=
        # CONFIRMED_THRESHOLD); suspected ones are excluded —
        # detected ≠ confirmed.
        severity_weights = {
            "critical": 1.0,
            "error": 0.7,
            "warning": 0.4,
            "info": 0.1,
        }
        total_weight = sum(
            severity_weights[f.severity]
            for f in all_failures
            if f.confirmed
        )
        # Normalization: 5 criticals count as full risk
        risk_score = min(total_weight / 5.0, 1.0)

        summary = {}
        for mode in FailureMode:
            count = len([f for f in all_failures if f.mode == mode])
            if count > 0:
                summary[mode.value] = count

        return FailureReport(
            document_id=document_id,
            total_chunks=len(chunks),
            failures=all_failures,
            summary=summary,
            risk_score=risk_score,
        )


def format_report(report: FailureReport) -> str:
    """Format the report as readable text."""
    lines = [
        f"Retrieval Failure Report: {report.document_id}",
        f"Total chunks: {report.total_chunks}",
        f"Total failures: {len(report.failures)}",
        f"Confirmed failures: {len(report.confirmed_failures())}",
        f"Suspected failures: {len(report.suspected_failures())}",
        f"Blocking failures: {len(report.blocking_failures())}",
        f"Risk score: {report.risk_score:.3f}",
        "",
        "Summary by mode:",
    ]
    for mode, count in report.summary.items():
        lines.append(f"  {mode}: {count}")

    if report.failures:
        lines.extend(["", "Details:"])
        for f in report.failures:
            confirmed_tag = "confirmed" if f.confirmed else "suspected"
            blocking_tag = ", blocking" if f.blocking else ""
            lines.append(
                f"  [{f.severity.upper()} | {confirmed_tag}{blocking_tag}] "
                f"{f.mode.value} (confidence={f.confidence:.2f})"
            )
            lines.append(f"    {f.description}")
            lines.append(f"    Fix: {f.fix_strategy}")
            lines.append("")

    return "\n".join(lines)
