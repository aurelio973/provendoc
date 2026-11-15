# ============================================================
# Structured Config Domain
# XML Semantic Structure Parser
# ============================================================
"""XML parser that projects an XML tree into the ConfigEntry contract.

XML is represented as a source-addressable tree rather than flattened prose:
attributes use ``@name``, direct element text uses ``#text``, and repeated
sibling elements use an explicit list/index path.  This lets the shared config
representation retain element hierarchy without treating markup as narrative
text.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ElementTree
from collections import Counter
from typing import Any, Dict, Iterator, List, Tuple

# With a TreeBuilder built with insert_comments/insert_pis, the tags
# of special nodes are the factory functions themselves (not constant
# strings); alias them as Any so identity checks work against str tags.
_COMMENT_TAG: Any = ElementTree.Comment
_PI_TAG: Any = ElementTree.ProcessingInstruction

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

_DOCTYPE_RE = re.compile(r"<!DOCTYPE\b", re.IGNORECASE)


class XmlParser(BaseParser):
    """Parse well-formed XML into ``structured_data`` and ConfigEntry facts.

    The standard library parser is deliberately used with DTD input rejected.
    This keeps the parser dependency-free while avoiding entity-expansion and
    external-entity processing.  XML entity spellings and CDATA boundaries are
    retained in ``Document.source_text`` but decode to their XML text value in
    the semantic contract.
    """

    def __init__(self, encoding: str = "utf-8", tolerant: bool = True):
        """Initialize with encoding and tolerant switch."""
        self.encoding = encoding
        self.tolerant = tolerant

    def parse(self, file_path: str) -> Document:
        """Parse an XML file into a Document (DTD rejected to prevent
        entity expansion)."""
        start_time = time.perf_counter()
        ctx = self._read_and_diagnose(
            file_path, "xml", self.encoding, self.tolerant,
        )
        metadata = ctx.metadata
        structured_data, contract, xml_metadata, parse_error = self._parse_xml(
            ctx.text,
        )

        metadata["structured_data"] = structured_data
        metadata["config_entries"] = contract

        # Object-level unresolved registry: derived from the
        # contract's existing reference three-state facts (xml is
        # declaration-rejected here, so shadowing derives nothing);
        # original keys untouched, written only when non-empty;
        # guarded, silently degrades, never blocks the main parse.
        try:
            stamp_unresolved_registry(
                metadata, "config_unresolved",
                derive_config_unresolved(metadata["config_entries"]),
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            _log.debug("config unresolved registry skipped: %s", exc)

        metadata["xml"] = xml_metadata
        metadata["parse_success"] = parse_error is None
        if parse_error is not None:
            metadata["parse_failed"] = True
            metadata["parse_error"] = parse_error
            metadata["warnings"].append("xml parse failed")
        elif isinstance(structured_data, dict):
            metadata["top_level_keys"] = list(structured_data.keys())
        metadata["parse_duration_ms"] = round(
            (time.perf_counter() - start_time) * 1000, 3,
        )
        if parse_error is not None:
            # honest-failure contract: a failed parse must not surface the
            # raw text as recovered content (mirrors json/toml parsers);
            # source_text keeps the untouched provenance snapshot.
            return Document(
                text="",
                metadata=metadata,
                source_text=ctx.source_text,
            )
        return Document(
            text=ctx.text,
            metadata=metadata,
            source_text=ctx.source_text,
        )

    def _parse_xml(
        self, text: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], str | None]:
        contract = cc.build_contract("xml", cc.DUP_REJECTED)
        reasons = contract["loss"]["reasons"]
        reasons.extend([
            "line_end_unavailable: standard library XML parser exposes no end position",
            "entity_spellings_are_decoded_to_xml_text_values",
            "cdata_boundaries_are_not_preserved_by_xml_tree",
        ])

        if _DOCTYPE_RE.search(text):
            error = "DOCTYPE declarations are not supported for safe XML ingestion"
            reasons.append("doctype_rejected_for_safe_xml_ingestion")
            cc.resolve_references(contract)
            return (
                {"_error": error},
                contract,
                {"parse_status": "rejected", "reason": error},
                error,
            )

        try:
            parser = ElementTree.XMLParser(
                target=ElementTree.TreeBuilder(
                    insert_comments=True,
                    insert_pis=True,
                )
            )
            root = ElementTree.fromstring(text, parser=parser)
        except (ElementTree.ParseError, ValueError) as exc:
            error = str(exc)
            reasons.append(f"xml_parse_failed: {error}")
            cc.resolve_references(contract)
            return (
                {"_error": error},
                contract,
                {"parse_status": "failed", "reason": error},
                error,
            )

        source_positions = self._scan_source_positions(text)
        namespace_tokens = self._namespace_tokens(root)
        element_lines = iter(source_positions["elements"])
        comment_positions = iter(source_positions["comments"])
        pi_positions = iter(source_positions["processing_instructions"])
        xml_nodes: List[Dict[str, Any]] = []

        root_name = self._qualified_name(root.tag, namespace_tokens)
        contract["containers"].append(cc.make_container([], "dict", child_count=0))
        try:
            root_data = self._walk_element(
                element=root,
                path=[root_name],
                element_lines=element_lines,
                comment_positions=comment_positions,
                pi_positions=pi_positions,
                namespace_tokens=namespace_tokens,
                contract=contract,
                xml_nodes=xml_nodes,
            )
            cc.fill_child_counts(contract["entries"], contract["containers"])
            # Sensitive-entry disclosure via the shared helper; on
            # no match nothing is added (zero drift).
            sensitive_count = cc.scan_sensitive_entries(contract["entries"])
            if sensitive_count:
                reasons.append(f"sensitive_entry_values:{sensitive_count}")
        except RecursionError:
            # Graceful degradation on deep nesting (mirrors the
            # plist guard — never breaks parse(); the contract keeps
            # the entries walked so far plus a degradation reason,
            # and structured_data discloses the degraded state).
            reasons.append("nesting_too_deep")
            root_data = {"_error": "nesting too deep for contract walk"}
        cc.resolve_references(contract)

        namespaces = [
            {"prefix": token, "uri": uri}
            for uri, token in namespace_tokens.items()
        ]
        contract["xml"] = {
            "root_tag": root_name,
            "root_tag_expanded": root.tag,
            "namespaces": namespaces,
            "node_order": xml_nodes,
            "comments_preserved": bool(source_positions["comments"]),
            "processing_instructions_preserved": bool(
                source_positions["processing_instructions"]
            ),
        }
        return (
            {root_name: root_data},
            contract,
            {
                "parse_status": "ok",
                "root_tag": root_name,
                "root_tag_expanded": root.tag,
                "namespaces": namespaces,
                "element_count": sum(
                    node["kind"] == "element" for node in xml_nodes
                ),
                "comment_count": sum(
                    node["kind"] == "comment" for node in xml_nodes
                ),
                "processing_instruction_count": sum(
                    node["kind"] == "processing_instruction"
                    for node in xml_nodes
                ),
            },
            None,
        )

    def _walk_element(
        self,
        *,
        element: ElementTree.Element,
        path: List[str],
        element_lines: Iterator[int],
        comment_positions: Iterator[Dict[str, Any]],
        pi_positions: Iterator[Dict[str, Any]],
        namespace_tokens: Dict[str, str],
        contract: Dict[str, Any],
        xml_nodes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        line_start = self._next_or_none(element_lines)
        tag_name = self._qualified_name(element.tag, namespace_tokens)
        contract["containers"].append(cc.make_container(
            path,
            "dict",
            child_count=0,
            line_start=line_start,
            line_end=line_start,
        ))
        xml_nodes.append({
            "kind": "element",
            "path": list(path),
            "tag": tag_name,
            "expanded_tag": element.tag,
            "line_start": line_start,
        })

        data: Dict[str, Any] = {}
        for raw_name, value in element.attrib.items():
            name = "@" + self._qualified_name(raw_name, namespace_tokens)
            data[name] = value
            contract["entries"].append(cc.make_entry(
                path + [name],
                name,
                value,
                value,
                line_start=line_start,
                line_end=line_start,
            ))

        direct_text = self._meaningful_text(element.text)
        if direct_text is not None:
            data["#text"] = direct_text
            contract["entries"].append(cc.make_entry(
                path + ["#text"],
                "#text",
                direct_text,
                direct_text,
                line_start=line_start,
                line_end=line_start,
            ))

        children = list(element)
        normal_children = [child for child in children if isinstance(child.tag, str)]
        sibling_counts = Counter(
            self._qualified_name(child.tag, namespace_tokens)
            for child in normal_children
        )
        sibling_indices: Dict[str, int] = {}
        special_indices = {"comment": 0, "processing_instruction": 0}

        for child in children:
            if child.tag is _COMMENT_TAG:
                special_indices["comment"] += 1
                key = f"#comment:{special_indices['comment']}"
                source = self._next_or_none(comment_positions) or {}
                comment_text = (child.text or "").strip()
                line = source.get("line_start", line_start)
                data[key] = comment_text
                contract["entries"].append(cc.make_entry(
                    path + [key],
                    key,
                    comment_text,
                    comment_text,
                    line_start=line,
                    line_end=line,
                ))
                contract["comments"].append(cc.make_comment(
                    "block",
                    comment_text,
                    source.get("raw", f"<!--{child.text or ''}-->"),
                    line if line is not None else 0,
                    line if line is not None else 0,
                    bound_path=path,
                    bound_kind="element",
                ))
                xml_nodes.append({
                    "kind": "comment",
                    "path": list(path) + [key],
                    "line_start": line,
                })
                continue

            if child.tag is _PI_TAG:
                special_indices["processing_instruction"] += 1
                key = (
                    "#processing-instruction:"
                    f"{special_indices['processing_instruction']}"
                )
                source = self._next_or_none(pi_positions) or {}
                pi_text = child.text or ""
                line = source.get("line_start", line_start)
                data[key] = pi_text
                contract["entries"].append(cc.make_entry(
                    path + [key],
                    key,
                    pi_text,
                    pi_text,
                    line_start=line,
                    line_end=line,
                ))
                xml_nodes.append({
                    "kind": "processing_instruction",
                    "path": list(path) + [key],
                    "line_start": line,
                    "value": pi_text,
                })
                continue

            child_name = self._qualified_name(child.tag, namespace_tokens)
            sibling_indices[child_name] = sibling_indices.get(child_name, 0) + 1
            is_repeated = sibling_counts[child_name] > 1
            if is_repeated:
                list_path = path + [child_name]
                if child_name not in data:
                    data[child_name] = []
                    contract["containers"].append(cc.make_container(
                        list_path,
                        "list",
                        child_count=0,
                        line_start=line_start,
                        line_end=line_start,
                    ))
                child_path = list_path + [str(sibling_indices[child_name] - 1)]
            else:
                child_path = path + [child_name]

            child_data = self._walk_element(
                element=child,
                path=child_path,
                element_lines=element_lines,
                comment_positions=comment_positions,
                pi_positions=pi_positions,
                namespace_tokens=namespace_tokens,
                contract=contract,
                xml_nodes=xml_nodes,
            )
            if is_repeated:
                data[child_name].append(child_data)
            else:
                data[child_name] = child_data

            tail = self._meaningful_text(child.tail)
            if tail is not None:
                child_data["#tail"] = tail
                contract["entries"].append(cc.make_entry(
                    child_path + ["#tail"],
                    "#tail",
                    tail,
                    tail,
                    line_start=None,
                    line_end=None,
                ))

        return data

    @staticmethod
    def _meaningful_text(value: str | None) -> str | None:
        return value if value is not None and value.strip() else None

    @staticmethod
    def _next_or_none(values: Iterator[Any]) -> Any | None:
        try:
            return next(values)
        except StopIteration:
            return None

    @classmethod
    def _namespace_tokens(cls, root: ElementTree.Element) -> Dict[str, str]:
        uris: Dict[str, str] = {}
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            for raw_name in [element.tag, *element.attrib]:
                uri = cls._namespace_uri(raw_name)
                if uri is not None and uri not in uris:
                    uris[uri] = f"ns{len(uris)}"
        return uris

    @staticmethod
    def _namespace_uri(raw_name: str) -> str | None:
        if raw_name.startswith("{") and "}" in raw_name:
            return raw_name[1:].split("}", 1)[0]
        return None

    @classmethod
    def _qualified_name(
        cls, raw_name: str, namespace_tokens: Dict[str, str],
    ) -> str:
        uri = cls._namespace_uri(raw_name)
        if uri is None:
            return raw_name
        local_name = raw_name.split("}", 1)[1]
        return f"{namespace_tokens[uri]}:{local_name}"

    @classmethod
    def _scan_source_positions(cls, text: str) -> Dict[str, List[Any]]:
        """Collect source-order start positions without re-parsing XML semantics."""
        positions: Dict[str, List[Any]] = {
            "elements": [],
            "comments": [],
            "processing_instructions": [],
        }
        index = 0
        length = len(text)
        while index < length:
            start = text.find("<", index)
            if start < 0:
                break
            line_start = text.count("\n", 0, start)
            if text.startswith("<!--", start):
                end = text.find("-->", start + 4)
                if end < 0:
                    break
                raw = text[start:end + 3]
                positions["comments"].append({
                    "line_start": line_start,
                    "raw": raw,
                })
                index = end + 3
                continue
            if text.startswith("<![CDATA[", start):
                end = text.find("]]>", start + 9)
                if end < 0:
                    break
                index = end + 3
                continue
            if text.startswith("<?", start):
                end = text.find("?>", start + 2)
                if end < 0:
                    break
                raw = text[start:end + 2]
                if not raw.lower().startswith("<?xml"):
                    positions["processing_instructions"].append({
                        "line_start": line_start,
                        "raw": raw,
                    })
                index = end + 2
                continue
            if text.startswith("</", start) or text.startswith("<!", start):
                end = cls._find_tag_end(text, start + 1)
                if end < 0:
                    break
                index = end + 1
                continue

            end = cls._find_tag_end(text, start + 1)
            if end < 0:
                break
            positions["elements"].append(line_start)
            index = end + 1
        return positions

    @staticmethod
    def _find_tag_end(text: str, index: int) -> int:
        quote: str | None = None
        while index < len(text):
            char = text[index]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == ">":
                return index
            index += 1
        return -1

    # ========================================
    # Multi-file layered merge
    # (thin wrapper over the shared cc.merge_contracts layer)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Layered merge across files: later files shadow earlier ones
        (shared cc.merge_contracts layer).

        Each file goes through the existing single-file contract path
        (including static loss disclosure and line numbers).  XML
        specifics: entries/containers using the ``#text`` path shape
        (element text nodes as leaf entries); in-file semantics are
        rejected (dtd/entity channel facts stay in each file's
        entries/loss layers).  The merged header sets
        ``duplicate_key_semantics`` to ``last_file_wins`` (cross-file
        shadowing at the merge layer; the two layers are orthogonal).
        Returns the merged contract dict (not a Document — there is
        no single source text across files, and line-number semantics
        are attributed via source_file).  base parse_many semantics
        are unchanged.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            contracts.append(meta.get("config_entries") or cc.build_contract(
                "xml", cc.DUP_REJECTED))
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })
        return cc.merge_contracts("xml", contracts, sources)
