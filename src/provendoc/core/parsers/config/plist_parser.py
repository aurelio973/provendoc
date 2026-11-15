# ============================================================
# Structured Config Domain
# Apple Property List (.plist) Parser
# ============================================================
import logging
import plistlib
import time

from provendoc.base.models.document import Document
from provendoc.base.parser import BaseParser
from provendoc.core.foundation.unresolved_registry import (
    stamp_unresolved_registry,
)
from provendoc.core.parsers.config import contracts as cc
from provendoc.core.parsers.config.config_unresolved import (
    derive_config_unresolved,
)

_log = logging.getLogger(__name__)


class PlistParser(BaseParser):
    """Apple property list (.plist) parser.

    plistlib handles both XML and binary containers through this
    single parser.  Value domain: dict / list / str / int / float /
    bool / bytes (data) / datetime.  Duplicate keys: plistlib dict
    semantics are last wins.
    """

    def __init__(self, encoding="utf-8", type_infer=False):
        """Initialize with encoding and type-inference switch."""
        self.encoding = encoding
        self.type_infer = type_infer

    def parse(self, file_path) -> Document:
        """Parse a .plist file into a Document (XML and binary alike)."""
        start_time = time.perf_counter()
        try:
            ctx = self._read_and_diagnose(file_path, "plist", self.encoding, True)
            text = ctx.text
            metadata = ctx.metadata

            try:
                with open(file_path, "rb") as f:
                    # _is_binary rewinds with seek(0), so load can
                    # read on directly from the same handle
                    is_binary = self._is_binary(f)
                    data = plistlib.load(f)
                metadata["parse_success"] = True
                metadata["plist_format"] = (
                    plistlib.FMT_BINARY if is_binary else plistlib.FMT_XML
                )
            except Exception as e:
                data = {"_error": str(e)}
                metadata["parse_success"] = False
                metadata.setdefault("warnings", []).append("plist parse failed")

            if isinstance(data, dict):
                metadata["top_level_keys"] = list(data.keys())

            metadata["structured_data"] = self._json_safe(data)

            metadata["config_entries"] = self._build_contract(data)

            # Object-level unresolved registry: derived from the
            # contract's existing reference three-state facts (plist
            # has no override-chain facts, so shadowing never fires);
            # original keys are untouched, only non-empty output is
            # written, and failures degrade silently.
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(metadata["config_entries"]),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            metadata["parse_duration_ms"] = round(
                (time.perf_counter() - start_time) * 1000, 3,
            )
            return Document(
                text=text, metadata=metadata, source_text=ctx.source_text,
            )
        except Exception as e:
            return Document(
                text="",
                metadata={
                    "parse_failed": True,
                    "parse_error": str(e),
                    "parse_success": False,
                    "structured_data": {"_error": str(e)},
                },
            )

    @staticmethod
    def _is_binary(fh):
        fh.seek(0)
        head = fh.read(8)
        fh.seek(0)
        return head[:6] == b"bplist"

    @staticmethod
    def _json_safe(node):
        """Serializable projection of bytes/datetime (for structured_data)."""
        if isinstance(node, dict):
            return {str(k): PlistParser._json_safe(v) for k, v in node.items()}
        if isinstance(node, list):
            return [PlistParser._json_safe(v) for v in node]
        if isinstance(node, bytes):
            return f"<data:{len(node)} bytes>"
        if isinstance(node, (set, tuple)):
            return [PlistParser._json_safe(v) for v in node]
        return node

    @staticmethod
    def _raw_value(node):
        """Contract raw_value: readable rendering that keeps type facts."""
        if isinstance(node, bytes):
            return f"<data:{len(node)} bytes>"
        if isinstance(node, str):
            return node
        return repr(node)

    # ==================================
    # config entry contract
    # ==================================

    def _build_contract(self, data):
        """Build the contract: plistlib exposes no token positions, so
        there are no line numbers; XML comments are not read back."""
        contract = cc.build_contract(
            "plist", cc.DUP_LAST_WINS,
            line_numbers=False, comments=False,
        )
        reasons = contract["loss"]["reasons"]
        reasons.extend([
            "line_numbers_unavailable: plistlib provides no position info",
            "comments_unavailable: xml comments not exposed by plistlib",
            "raw_value_is_canonical_re_render",
        ])

        try:
            self._walk(data, [], None, contract["entries"], contract["containers"])
            cc.fill_child_counts(contract["entries"], contract["containers"])
            # Sensitive-entry disclosure via the shared helper; on
            # no match nothing is added (zero drift).
            sensitive_count = cc.scan_sensitive_entries(contract["entries"])
            if sensitive_count:
                reasons.append(f"sensitive_entry_values:{sensitive_count}")
            cc.resolve_references(contract)
        except RecursionError:
            reasons.append("nesting_too_deep")
        except Exception as e:  # pragma: no cover
            reasons.append(f"contract_build_failed: {e}")
        return contract

    def _walk(self, node, path, key, entries, containers):
        if isinstance(node, dict):
            containers.append(cc.make_container(
                path, "dict", child_count=len(node),
            ))
            for k, v in node.items():
                self._walk_value(
                    v, path + [str(k)], str(k), False, None,
                    entries, containers,
                )
        elif isinstance(node, list):
            containers.append(cc.make_container(
                path, "list", child_count=len(node),
            ))
            for idx, item in enumerate(node):
                self._walk_value(
                    item, path + [str(idx)], key, False, None,
                    entries, containers,
                )

    def _walk_value(self, node, path, key, dup, chain, entries, containers):
        if isinstance(node, (dict, list)):
            self._walk(node, path, key, entries, containers)
        else:
            if node is None:
                state = cc.VALUE_STATE_EXPLICIT_NULL
            elif isinstance(node, bool):
                state = cc.VALUE_STATE_SCALAR
            elif isinstance(node, (int, float)):
                state = cc.VALUE_STATE_SCALAR
            else:
                state = cc.VALUE_STATE_SCALAR
            entries.append(cc.make_entry(
                path, key, self._raw_value(node), node,
                value_state=state,
                duplicate=dup, override_chain=chain,
            ))

    # ========================================
    # Multi-file layered merge
    # (thin wrapper over the shared cc.merge_contracts layer)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Layered merge across files: later files shadow earlier ones
        (shared cc.merge_contracts layer).

        Each file goes through the existing single-file contract path
        (including static loss disclosure).  Plist specifics: no line
        numbers and no comments (the fallback contract uses the same
        line_numbers=False, comments=False); values keep their native
        types (int/bool/float).  The merged header sets
        ``duplicate_key_semantics`` to ``last_file_wins`` (cross-file
        shadowing at the merge layer; in-file last-wins semantics are
        orthogonal to it).  Returns the merged contract dict (not a
        Document — line numbers are None anyway, and provenance is
        attributed via source_file).  base parse_many semantics are
        unchanged.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            contracts.append(meta.get("config_entries") or cc.build_contract(
                "plist", cc.DUP_LAST_WINS,
                line_numbers=False, comments=False))
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
        return cc.merge_contracts("plist", contracts, sources)
