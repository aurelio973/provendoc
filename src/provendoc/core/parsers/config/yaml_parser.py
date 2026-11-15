# ============================================================
# Structured Config Domain
# YAML Semantic Structure Parser
# ============================================================
import logging
import re
import time

import yaml
from yaml.events import AliasEvent

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

#: alias materialization budget (measured corpora merge depth < 100;
#: thousands cover real K8s/Compose use cases; combined references
#: over the limit → sentinel + PARTIAL)
ALIAS_MATERIALIZE_BUDGET = 1000

# ============================================
# tolerant loader
# ============================================

class _AliasEdgeComposerMixin:
    """Composer mixin: reference-relationship fact collection.

    After compose, an alias and its anchor definition share the
    same node object, so the walk layer cannot distinguish a
    "definition arrival" from a "reference arrival" from the node
    itself. This mixin records exact reference edges while
    compose_node consumes AliasEvents:
      - mapping value edges: (id(parent), id(key_node))
      - sequence item edges: (id(parent), item_index)
    (PyYAML's Composer.compose_document resets anchors after each
    document, so anchor definitions are also registered here into
    a defs list.)
    The walk queries by the same keys to decide true alias arrivals
    precisely (incl. reference line numbers); anchors nested inside
    an aliased subtree are never misjudged (their arrival edge is
    still the original definition edge).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.anchor_defs = []   # [{"name", "line", "node"}] definition order
        self.alias_events = []  # [{"anchor", "line"}] event order
        self.alias_edges = {}   # {(id(parent), id(key_node)|idx): info}

    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            event = self.peek_event()
            line = event.start_mark.line if getattr(event, "start_mark", None) else None
            info = {"anchor": event.anchor, "line": line}
            self.alias_events.append(info)
            if parent is not None and isinstance(index, yaml.Node):
                self.alias_edges[(id(parent), id(index))] = info
            elif parent is not None and isinstance(index, int):
                self.alias_edges[(id(parent), index)] = info
            return super().compose_node(parent, index)
        event = self.peek_event()
        anchor = getattr(event, "anchor", None)
        node = super().compose_node(parent, index)
        if anchor is not None and node is not None:
            line = event.start_mark.line if getattr(event, "start_mark", None) else None
            self.anchor_defs.append({"name": anchor, "line": line, "node": node})
        return node


class TolerantSafeLoader(_AliasEdgeComposerMixin, yaml.SafeLoader):
    """Tolerant SafeLoader: unknown tags fall back to best-effort
    construction; also collects anchor/alias/merge reference-edge
    facts."""

    pass


def unknown_tag_handler(loader, tag_suffix, node):
    """Construction fallback for unknown tags: best effort, fall back to node text on failure.

    Security red line: language-object deserialization feature tags
    (python/ruby/apply etc.) are rejected outright at this layer
    (constructor exception), sending _load_yaml down the
    compose-level recovery path — ordinary keys recover as usual,
    dangerous subtrees are registered as {"_blocked_tag": tag}
    sentinels (safe but incomplete; never executed, never silent).
    """
    if classify_tag(tag_suffix) == "unsafe":
        raise yaml.constructor.ConstructorError(
            None, None,
            f"unsafe tag blocked (not executed): {tag_suffix}",
            node.start_mark,
        )
    try:
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        elif isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
    except Exception:
        try:
            return str(node.value)
        except Exception:
            return None


TolerantSafeLoader.add_multi_constructor("", unknown_tag_handler)


# ============================================
# scalar tag semantics
# ============================================

#: whitelist of YAML standard safe tags (the full set SafeLoader
#: supports under tag:yaml.org,2002:*). Nodes in this set take the
#: standard construction path; type facts are authoritatively disclosed by tag.
_STANDARD_YAML_TAGS = frozenset(
    f"tag:yaml.org,2002:{name}" for name in (
        "bool", "float", "int", "null", "str", "seq", "map", "merge",
        "timestamp", "value", "binary", "set", "omap", "pairs",
    )
)

#: language-object deserialization tag markers (the classic YAML deserialization CVE surface; security red line)
_UNSAFE_TAG_MARKERS = ("python/", "ruby/", "perl/", ":apply:", "js/function")


def classify_tag(tag: str) -> str:
    """Three-way tag classification: standard | unsafe | custom.

    - standard: official YAML tag → normal construction (authoritative type facts)
    - unsafe:   language-object deserialization marker → node-level
                sentinel block (UNSAFE_TAG_BLOCKED; not executed, not
                constructed, does not drop the whole file)
    - custom:   other custom tags (!Env / !Ref etc.) → best-effort
                construction (existing TolerantSafeLoader behavior,
                zero drift), registered as not_executed
    """
    if tag in _STANDARD_YAML_TAGS:
        return "standard"
    if any(marker in tag for marker in _UNSAFE_TAG_MARKERS):
        return "unsafe"
    return "custom"


def derive_value_type(node) -> tuple:
    """ScalarNode → (value_type, type_source, hazard).

    Type facts come from the YAML node tag (syntax-layer authority,
    type_source="tag"); only leading-zero ints (PyYAML's YAML 1.1
    resolver octal/hex semantics) additionally disclose a hazard
    (the value keeps the observed 1.1 semantics without silent
    overwriting; lexical traps are explicitly registered as
    "octal_like" / "hex_like").
    """
    tag = getattr(node, "tag", "") or ""
    raw = getattr(node, "value", "") or ""
    if tag == "tag:yaml.org,2002:int":
        if raw.lower().startswith("0x") or raw.lower().startswith("-0x"):
            return cc.VALUE_TYPE_HEX_INT, cc.TYPE_SOURCE_TAG, "hex_like"
        if raw.startswith(("0", "-0")) and raw not in ("0", "-0") \
                and not raw.startswith(("0.", "-0.")) \
                and not raw.startswith(("0e", "0E", "-0e", "-0E")) \
                and "e" not in raw.lower() and "E" not in raw:
            # the 1.1 resolver constructs leading-zero non-zero ints as octal (001234→668)
            return cc.VALUE_TYPE_OCTAL_INT, cc.TYPE_SOURCE_TAG, "octal_like"
        return cc.VALUE_TYPE_INT, cc.TYPE_SOURCE_TAG, None
    if tag == "tag:yaml.org,2002:float":
        if raw in (".inf", "+.inf", ".Inf", "+.Inf", ".INF", "+.INF"):
            return cc.VALUE_TYPE_FLOAT_INFINITY, cc.TYPE_SOURCE_TAG, None
        if raw in ("-.inf", "-.Inf", "-.INF"):
            return cc.VALUE_TYPE_FLOAT_NEG_INFINITY, cc.TYPE_SOURCE_TAG, None
        if raw in (".nan", ".NaN", ".NAN"):
            return cc.VALUE_TYPE_FLOAT_NAN, cc.TYPE_SOURCE_TAG, None
        return cc.VALUE_TYPE_FLOAT, cc.TYPE_SOURCE_TAG, None
    if tag == "tag:yaml.org,2002:bool":
        return cc.VALUE_TYPE_BOOL, cc.TYPE_SOURCE_TAG, None
    if tag == "tag:yaml.org,2002:null":
        return cc.VALUE_TYPE_NULL, cc.TYPE_SOURCE_TAG, None
    if tag == "tag:yaml.org,2002:timestamp":
        return cc.VALUE_TYPE_TIMESTAMP, cc.TYPE_SOURCE_TAG, None
    if classify_tag(tag) == "unsafe":
        return cc.VALUE_TYPE_BLOCKED_TAG, cc.TYPE_SOURCE_TAG, None
    if classify_tag(tag) == "custom":
        return cc.VALUE_TYPE_CUSTOM_TAG, cc.TYPE_SOURCE_TAG, None
    return cc.VALUE_TYPE_STRING, cc.TYPE_SOURCE_TAG, None


def strip_directives(text: str):
    """Strip leading %YAML/%TAG directive lines.

    The tolerant '\\n---' pre-split would mistake a directive header
    for a standalone chunk (compose reports "expected '<document
    start>'", inflating document_count). Directive lines belong
    syntactically to the first document that follows (YAML spec:
    directives bind to a document). Returns (remaining_text,
    directive_lines, directive_version).
    """
    directive_lines = []
    version = None
    lines = text.split("\n")
    i = 0
    while i < len(lines) and lines[i].startswith("%"):
        directive_lines.append(lines[i])
        if lines[i].startswith("%YAML"):
            version = lines[i].split()[1] if len(lines[i].split()) > 1 else None
        i += 1
    if not directive_lines:
        return text, [], None
    remaining = "\n".join(lines[i:])
    return remaining, directive_lines, version


def _node_kind(node) -> str:
    """Short node kind name (identity_graph.anchors[].kind)."""
    if isinstance(node, yaml.MappingNode):
        return "mapping"
    if isinstance(node, yaml.SequenceNode):
        return "sequence"
    return "scalar"


def _identity_snapshot(loader):
    """Snapshot of single-document reference facts after compose."""
    return {
        "anchors": list(loader.anchor_defs),
        "aliases": list(loader.alias_events),
        "edges": loader.alias_edges,
    }


def _is_merge_key(key_node):
    """Decide whether a mapping key is the YAML merge key (`<<`, merge tag)."""
    if getattr(key_node, "tag", "") == "tag:yaml.org,2002:merge":
        return True
    try:
        return str(key_node.value) == "<<"
    except Exception:
        return False


def _mark_record(record, node_anchor, alias_ctx, merged_ctx, tagged=None):
    """Stamp additive anchor/alias/merge fields onto an entry/container
    on demand (no keys added when absent — files without reference
    syntax keep a byte-identical contract)."""
    if node_anchor is not None:
        record["anchor"] = node_anchor
    if alias_ctx:
        record["alias_of"] = alias_ctx["alias_of"]
        record["alias_ref_line"] = alias_ctx["alias_ref_line"]
    if merged_ctx:
        record["merged_from"] = dict(merged_ctx)
    if tagged:
        record["tagged_value"] = dict(tagged)


# yaml-language-server modeline (the de-facto standard declaration slot)
_SCHEMA_MODELINE_RE = re.compile(
    r"^\s*#\s*yaml-language-server:\s*\$schema=\s*(\S+)", re.M,
)


# schema tri-state helpers were elevated to the contracts family layer
# (schema_expectations / apply_schema_status, logic kept verbatim);
# aliases kept here so existing call sites stay unchanged
# (behavior-equivalent refactor)
_schema_expectations = cc.schema_expectations
_apply_schema_status = cc.apply_schema_status


# ============================================
# parser
# ============================================

class YamlParser(BaseParser):
    """YAML config file parser."""

    def __init__(
        self,
        encoding="utf-8",
        tolerant=True,
    ):
        """Initialize with encoding and tolerant switches."""
        self.encoding = encoding
        self.tolerant = tolerant

    # ========================================
    # fallback partial extraction
    # ========================================

    def _extract_simple_pairs(self, raw_doc):
        result = {}
        for line in raw_doc.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in stripped:
                key, value = stripped.split(":", 1)
                result[key.strip()] = value.strip()
        return result

    # ========================================
    # yaml load
    # ========================================

    def _load_yaml(self, text):
        # strict
        if not self.tolerant:
            return list(yaml.safe_load_all(text))

        # tolerant (directive headers stripped first, preventing "\n---" split false positives)
        text, _directive_lines, _version = strip_directives(text)
        raw_docs = text.split("\n---")
        parsed_docs = []
        for raw_doc in raw_docs:
            raw_doc = raw_doc.strip()
            if not raw_doc:
                continue
            try:
                parsed = yaml.load(raw_doc, Loader=TolerantSafeLoader)
                parsed_docs.append(parsed)
            except Exception as e:
                # on construction failure, recover node-by-node via
                # compose — dangerous-tag subtrees blocked as
                # sentinels, ordinary keys recover as usual
                # (safe but incomplete)
                recovered = self._compose_recover(raw_doc)
                if recovered is not None:
                    recovered["_error"] = str(e)
                    parsed_docs.append(recovered)
                else:
                    parsed_docs.append({
                        "_error": str(e),
                        "_partial_data": self._extract_simple_pairs(raw_doc),
                    })
        return parsed_docs

    def _compose_recover(self, raw_doc):
        """Compose-level node recovery for a chunk whose construction failed.

        The compose phase constructs no Python objects (safe) and only
        yields the node tree; children are constructed one by one,
        dangerous-tag subtrees are replaced by sentinel registrations
        ({"_blocked_tag": tag}), everything else proceeds as usual.
        Returns None when compose itself fails (syntax-level damage;
        the existing text-level fallback applies).
        """
        try:
            loader = TolerantSafeLoader(raw_doc)
            root = loader.get_single_node()
        except Exception:
            return None
        if not isinstance(root, yaml.MappingNode):
            return None

        def build(node):
            if isinstance(node, yaml.ScalarNode):
                if classify_tag(node.tag) == "unsafe":
                    return {"_blocked_tag": node.tag}
                try:
                    return loader.construct_object(node)
                except Exception:
                    return node.value
            if isinstance(node, yaml.SequenceNode):
                if classify_tag(node.tag) == "unsafe":
                    return {"_blocked_tag": node.tag}
                return [build(c) for c in node.value]
            if isinstance(node, yaml.MappingNode):
                if classify_tag(node.tag) == "unsafe":
                    return {"_blocked_tag": node.tag}
                out = {}
                for kn, vn in node.value:
                    try:
                        k = loader.construct_object(kn)
                    except Exception:
                        k = kn.value
                    out[k if isinstance(k, str) else str(k)] = build(vn)
                return out
            return None

        return build(root)

    # ========================================
    # config entry contract
    # ========================================

    def _build_contract(self, text):
        """Build the ConfigEntry contract (contract v1).

        Fact sources:
            - node tree (compose)  → line numbers, duplicate keys, override chains, empty-container tri-state
            - construct_object     → parsed values
            - line scan             → comment blocks/inline comments and their binding

        Any exception degrades into a loss reason; contract building
        never breaks the main parse flow.
        """
        contract = cc.build_contract("yaml", cc.DUP_LAST_WINS)
        reasons = contract["loss"]["reasons"]

        # measured profile disclosure (PyYAML resolver = YAML 1.1
        # semantics, the de-facto standard; %YAML directives
        # registered as captured)
        _text, _directive_lines, directive_version = strip_directives(text)
        contract["schema_profile"] = "yaml_1_1_pyyaml_default"
        if directive_version:
            contract["yaml_directive"] = directive_version

        try:
            doc_specs = self._compose_documents(text, reasons)

            entries = contract["entries"]
            containers = contract["containers"]
            key_lines = []       # [{line, col, path, kind, doc_index}]
            protected = set()    # block scalar / flow content line numbers (0-based)

            # identity aggregation (across documents) + alias
            # materialization budget (shared file-wide)
            graph_anchors = []  # [{name, line, kind, doc_index}]
            graph_aliases = []  # [{anchor, line, doc_index}]
            merge_edges = []    # [{path, doc_index, sources}]
            budget = {"left": ALIAS_MATERIALIZE_BUDGET}

            top_level_keys = set()  # top-level keys (strict k8s/compose evidence)
            all_keys = set()        # whole-tree keys (CI evidence — image/script often below the top level)

            for doc_index, (loader, root, line_offset, snap) in enumerate(doc_specs):
                if root is None:
                    # None document (empty/comment-only/compose-failure
                    # placeholder) — skip the walk but keep doc_index
                    # aligned with the structured_data list positions
                    # (otherwise all later documents shift)
                    continue
                if isinstance(root, yaml.MappingNode):
                    for kn, _vn in root.value:
                        try:
                            top_level_keys.add(str(loader.construct_object(kn)))
                        except Exception:
                            top_level_keys.add(str(kn.value))

                def _collect_keys(nd, ld, seen):
                    if id(nd) in seen:
                        return
                    seen.add(id(nd))
                    if isinstance(nd, yaml.MappingNode):
                        for kn, vn in nd.value:
                            try:
                                all_keys.add(str(ld.construct_object(kn)))
                            except Exception:
                                all_keys.add(str(getattr(kn, "value", kn)))
                            _collect_keys(vn, ld, seen)
                    elif isinstance(nd, yaml.SequenceNode):
                        for child in nd.value:
                            _collect_keys(child, ld, seen)

                try:
                    _collect_keys(root, loader, set())
                except Exception:
                    pass  # hints are weak declarations; collection failure never affects the contract
                if snap:
                    for a in snap["anchors"]:
                        graph_anchors.append({
                            "name": a["name"],
                            "line": (a["line"] + line_offset)
                                    if a["line"] is not None else None,
                            "kind": _node_kind(a["node"]),
                            "doc_index": doc_index,
                        })
                    for ev in snap["aliases"]:
                        graph_aliases.append({
                            "anchor": ev["anchor"],
                            "line": (ev["line"] + line_offset)
                                    if ev["line"] is not None else None,
                            "doc_index": doc_index,
                        })
                ictx = {
                    "anchor_by_id": {
                        id(a["node"]): a for a in (snap["anchors"] if snap else [])
                    },
                    "alias_edges": snap["edges"] if snap else {},
                    "visit_counts": {},
                    "budget": budget,
                    "merge_edges": merge_edges,
                    "line_offset": line_offset,
                }
                try:
                    self._walk_node(
                        loader, root, [], None, None,
                        doc_index, line_offset,
                        entries, containers, key_lines, protected,
                        set(), reasons,
                        ictx=ictx,
                    )
                except RecursionError:
                    reasons.append(f"doc_{doc_index}_nesting_too_deep")
                except Exception as e:
                    reasons.append(f"doc_{doc_index}_walk_failed: {e}")

            # identity_graph (keys omitted for files without reference
            # syntax — no empty shells)
            if graph_anchors or graph_aliases or merge_edges:
                contract["identity_graph"] = {
                    "anchors": graph_anchors,
                    "aliases": graph_aliases,
                    "merge_edges": merge_edges,
                }

            # domain hints as weak declarations (heuristic evidence,
            # omitted when nothing hits). k8s/compose are judged
            # strictly by top-level keys; CI image/script often live
            # below the second level, so the whole-tree keys are used
            hints = []
            if {"apiVersion", "kind"} <= top_level_keys:
                hints.append("kubernetes")
            if "services" in top_level_keys and "version" in top_level_keys:
                hints.append("compose")
            if (({"jobs", "stages"} & all_keys)
                    and ({"image", "script"} & all_keys)):
                hints.append("ci")
            if hints:
                contract["domain_hints"] = hints

            # comment scan and binding (includes the fill_child_counts
            # backfill — schema structural expectations depend on
            # child_count, so the keys must land after the backfill)
            try:
                self._scan_comments(
                    text, entries, containers, key_lines,
                    protected, contract["comments"], reasons,
                )
            except Exception as e:
                reasons.append(f"comment_scan_failed: {e}")

            # schema validation tri-state (on-demand keys: all omitted
            # when there is no evidence). schema_ref = declaration slot
            # ($schema top-level key / yaml-language-server modeline);
            # status evidence is only the domain_hints structural
            # expectation (offline-verifiable); declared but no known
            # profile → absent + reason (no network, no guessing).
            # Must run after _scan_comments: the compose expectation
            # reads child_count
            schema_ref = None
            m = _SCHEMA_MODELINE_RE.search(text)
            if m:
                schema_ref = m.group(1)
            else:
                for ent in entries:
                    if ent["path"] == ["$schema"] and ent.get("value"):
                        schema_ref = str(ent["value"])
                        break
            _apply_schema_status(
                contract, schema_ref, hints, entries, containers, reasons)

            # sensitive disclosure (shared helper across channels;
            # no hits → no keys, no reason — zero drift. The merge path
            # does not rescan — the winning entry's shallow copy
            # transparently carries the candidate keys).
            sensitive_count = cc.scan_sensitive_entries(entries)
            if sensitive_count:
                reasons.append(f"sensitive_entry_values:{sensitive_count}")

            cc.resolve_references(contract)
        except Exception as e:  # pragma: no cover - final fallback
            reasons.append(f"contract_build_failed: {e}")

        return contract

    # ----------------------------------------
    # document chunking (same semantics as _load_yaml)
    # ----------------------------------------

    def _compose_documents(self, text, reasons):
        """Compose documents → [(loader, root_node, line_offset, snap), ...].

        tolerant: the same '\\n---' pre-split as _load_yaml (each chunk
                  is composed independently, line numbers need the
                  line_offset correction; reference facts are
                  snapshotted per loader).
        strict:   a single loader compose_all, global line numbers
                  (anchor_defs/alias_events snapshotted in slices in
                  document order — PyYAML resets anchors after each
                  document).
        """
        specs = []
        if self.tolerant:
            text, directive_lines, _version = strip_directives(text)
            line_cursor = len(directive_lines)
            parts = text.split("\n---")
            for part in parts:
                if part.strip():
                    try:
                        loader = TolerantSafeLoader(part)
                        root = loader.get_single_node()
                        # a None root (comment-only document) also
                        # takes a slot, keeping document indexes
                        # strictly aligned with _load_yaml
                        specs.append((loader, root, line_cursor,
                                      _identity_snapshot(loader)))
                    except Exception as e:
                        reasons.append(f"compose_failed: {e}")
                        # a failed part also takes a slot in
                        # _load_yaml (error dict); same here
                        specs.append((None, None, line_cursor, None))
                line_cursor += part.count("\n") + 1
        else:
            try:
                loader = TolerantSafeLoader(text)
                anchors_seen = 0
                aliases_seen = 0
                while loader.check_node():
                    node = loader.get_node()
                    snap = {
                        "anchors": loader.anchor_defs[anchors_seen:],
                        "aliases": loader.alias_events[aliases_seen:],
                        "edges": loader.alias_edges,
                    }
                    anchors_seen = len(loader.anchor_defs)
                    aliases_seen = len(loader.alias_events)
                    specs.append((loader, node, 0, snap))
            except Exception as e:
                reasons.append(f"compose_failed: {e}")
        return specs

    # ----------------------------------------
    # node-tree walk
    # ----------------------------------------

    def _walk_node(
        self, loader, node, path, key, key_node,
        doc_index, line_offset,
        entries, containers, key_lines, protected,
        visiting, reasons,
        duplicate=False, override_chain=None,
        ictx=None, alias_arrival=None, alias_ctx=None,
        merged_ctx=None,
    ):
        """Walk the node tree recursively, emitting entry/container records.

        - the parent records its children's key line numbers (key_lines, for comment binding)
        - cyclic references (alias self-references) are detected via visiting (an id set)
        - alias_arrival = a true alias reference arrival (reference-edge
          hit, consumes materialization budget); alias_ctx = inherited
          materialization context (inner nodes of an aliased subtree);
          merged_ctx = merge expansion source chain. Returns the
          entry/container records emitted by this call (for merge
          shadow-chain backfill).
        """
        line = (node.start_mark.line + line_offset) if node.start_mark else None
        end_line = (node.end_mark.line + line_offset) if node.end_mark else None

        # arrival-mode decision — first definition arrival (anchor name disclosed) vs alias materialization
        node_anchor = None
        eff_alias_ctx = alias_ctx
        if ictx is not None:
            nid = id(node)
            ictx["visit_counts"][nid] = ictx["visit_counts"].get(nid, 0) + 1
            if ictx["visit_counts"][nid] == 1 and alias_arrival is None:
                ainfo = ictx["anchor_by_id"].get(nid)
                if ainfo is not None:
                    node_anchor = ainfo["name"]
        if alias_arrival is not None:
            # true alias arrival (line numbers keep definition
            # semantics + reference line disclosed; budget exhausted
            # → sentinel, stop expanding this subtree)
            ictx["budget"]["left"] -= 1
            if ictx["budget"]["left"] < 0:
                reasons.append(f"alias_budget_exhausted:{cc.path_str(path)}")
                entry = cc.make_entry(
                    path, key, None,
                    {"_alias_budget_exhausted": alias_arrival["anchor"]},
                    value_state=cc.VALUE_STATE_SCALAR,
                    line_start=line, line_end=end_line,
                    duplicate=duplicate, override_chain=override_chain,
                    doc_index=doc_index,
                    value_type=cc.VALUE_TYPE_ALIAS_BUDGET,
                    type_source=cc.TYPE_SOURCE_TAG,
                )
                _mark_record(entry, None, None, merged_ctx)
                entries.append(entry)
                return entry
            _ref_line = alias_arrival.get("line")
            eff_alias_ctx = {
                "alias_of": alias_arrival["anchor"],
                "alias_ref_line": (_ref_line + line_offset)
                                  if _ref_line is not None else None,
            }

        # multi-line scalars / flow containers → protected lines (content must not be mistaken for comments)
        try:
            spans_lines = node.start_mark.line != node.end_mark.line
        except Exception:
            spans_lines = False
        if spans_lines and line is not None and end_line is not None:
            for ln in range(line, end_line + 1):
                protected.add(ln)

        # cyclic reference: after compose, an alias returns the same node object
        if id(node) in visiting:
            reasons.append(f"cyclic_anchor_at:{cc.path_str(path)}")
            return None

        # dangerous tags on container nodes get the same node-level
        # block (probe-verified: !!python/object/apply on a sequence
        # would be best-effort constructed as its argument list —
        # zero disclosure and misleading semantics; must be a sentinel)
        if classify_tag(node.tag) == "unsafe":
            reasons.append(f"unsafe_tag_blocked:{cc.path_str(path)}")
            entry = cc.make_entry(
                path, key, None,
                {"_blocked_tag": node.tag},
                value_state=cc.VALUE_STATE_SCALAR,
                line_start=line, line_end=end_line,
                duplicate=duplicate, override_chain=override_chain,
                doc_index=doc_index,
                value_type=cc.VALUE_TYPE_BLOCKED_TAG,
                type_source=cc.TYPE_SOURCE_TAG,
            )
            _mark_record(entry, node_anchor, eff_alias_ctx, merged_ctx)
            entries.append(entry)
            if line is not None and end_line is not None:
                for ln in range(line, end_line + 1):
                    protected.add(ln)
            return entry

        if isinstance(node, yaml.MappingNode):
            container = self._record_container(
                path, "dict", doc_index, line, end_line,
                containers, key_node, line_offset,
                duplicate=duplicate, override_chain=override_chain,
            )
            _mark_record(container, node_anchor, eff_alias_ctx, merged_ctx)
            visiting.add(id(node))

            # --- merge (<<) source collection (before local-key ---
            # --- grouping, to backfill local shadow chains; earlier ---
            # --- sources win among sources, local keys always win) ---
            merge_occ = {}      # k_str -> {vn, kn, mfrom, src}
            merge_order = []    # first-appearance order of source keys
            has_merge_key = False
            if ictx is not None:
                for kn, vn in node.value:
                    if not _is_merge_key(kn):
                        continue
                    has_merge_key = True
                    edge = ictx["alias_edges"].get((id(node), id(kn)))
                    if edge is not None:
                        ictx["budget"]["left"] -= 1
                        if ictx["budget"]["left"] < 0:
                            reasons.append(
                                f"alias_budget_exhausted:{cc.path_str(path)}")
                            continue
                    ref_line = None
                    if edge is not None and edge.get("line") is not None:
                        ref_line = edge["line"] + line_offset
                    sources = (
                        vn.value if isinstance(vn, yaml.SequenceNode)
                        else [vn]
                    )
                    for si, src in enumerate(sources):
                        src_ref_line = ref_line
                        if edge is None and isinstance(vn, yaml.SequenceNode):
                            # sequence-form merge (<<: [*a, *b]): the
                            # merge key has no alias edge; the
                            # reference line comes from the sequence
                            # item edge
                            iedge = ictx["alias_edges"].get((id(vn), si))
                            if iedge is not None and iedge.get("line") is not None:
                                src_ref_line = iedge["line"] + line_offset
                        if not isinstance(src, yaml.MappingNode):
                            reasons.append(
                                f"merge_source_not_mapping:"
                                f"{cc.path_str(path)}")
                            continue
                        ainfo = ictx["anchor_by_id"].get(id(src))
                        if ainfo is not None:
                            # alias sources count the budget separately (prevents exponential blowup from nested merges)
                            ictx["budget"]["left"] -= 1
                            if ictx["budget"]["left"] < 0:
                                reasons.append(
                                    f"alias_budget_exhausted:"
                                    f"{cc.path_str(path)}")
                                continue
                        if ainfo is not None:
                            mfrom = {
                                "anchor": ainfo["name"],
                                "line": (ainfo["line"] + line_offset)
                                        if ainfo["line"] is not None else None,
                                "ref_line": src_ref_line,
                            }
                        else:
                            mfrom = {
                                "anchor": None,
                                "line": (src.start_mark.line + line_offset)
                                        if src.start_mark else None,
                                "ref_line": src_ref_line,
                            }
                        ictx["merge_edges"].append({
                            "path": cc.path_str(path),
                            "doc_index": doc_index,
                            "anchor": mfrom["anchor"],
                            "anchor_line": mfrom["line"],
                            "ref_line": src_ref_line,
                        })
                        for skn, svn in src.value:
                            if _is_merge_key(skn):
                                # YAML merge is non-recursive: inner << inside sources is skipped
                                reasons.append("nested_merge_key_skipped")
                                continue
                            try:
                                sk = loader.construct_object(skn)
                            except Exception:
                                sk = skn.value
                            sk_str = str(sk)
                            if sk_str in merge_occ:
                                continue    # earlier source wins
                            merge_occ[sk_str] = {
                                "vn": svn, "kn": skn,
                                "mfrom": mfrom, "src": src,
                            }
                            merge_order.append(sk_str)
                if has_merge_key:
                    reasons.append("merge_keys_expanded")

            # group same-level duplicate keys (first-appearance order kept; merge keys excluded)
            groups: dict = {}
            for kn, vn in node.value:
                if _is_merge_key(kn):
                    if ictx is None:
                        reasons.append("merge_keys_not_expanded")
                    continue
                # non-scalar keys (complex keys) are disclosed as-is —
                # an unquoted helm template `{{ .Values.x }}` that
                # PyYAML misparses as a flow mapping key is exactly
                # this shape; the contract layer used to be fully
                # silent about it
                if isinstance(kn, (yaml.MappingNode, yaml.SequenceNode)):
                    parent_str = cc.path_str(path)
                    reasons.append(f"non_scalar_key:{parent_str}")
                    try:
                        k = loader.construct_object(kn)
                        k_str = str(k)
                    except Exception:
                        k_str = "<complex>"
                    if k_str == "{}" or k_str.startswith("{"):
                        reasons.append(f"template_suspect_at:{parent_str}")
                    groups.setdefault(k_str, []).append((kn, vn))
                    continue
                try:
                    k = loader.construct_object(kn)
                except Exception:
                    k = kn.value
                k_str = str(k)
                groups.setdefault(k_str, []).append((kn, vn))

            for k_str, occurrences in groups.items():
                kn = occurrences[0][0]
                kn_line = (kn.start_mark.line + line_offset) if kn.start_mark else None
                kn_col = kn.start_mark.column if kn.start_mark else 0
                child_path = path + [k_str]
                key_lines.append({
                    "line": kn_line, "col": kn_col,
                    "path": child_path, "kind": "key",
                    "doc_index": doc_index,
                })
                mo = merge_occ.pop(k_str, None)  # local key shadows the source key
                dup = len(occurrences) > 1 or mo is not None
                chain = None
                if dup:
                    chain = []
                    if mo is not None:
                        # a local key always shadows the merge source
                        # (YAML semantics); the source appears at the
                        # head of the chain as the lowest priority
                        mo_line = (
                            mo["vn"].start_mark.line + line_offset
                            if mo["vn"].start_mark else None
                        )
                        chain.append({
                            "value": self._node_value_repr(loader, mo["vn"]),
                            "line": mo_line,
                            "merged_from": dict(mo["mfrom"]),
                        })
                    for occ_kn, occ_vn in occurrences:
                        occ_line = (
                            occ_vn.start_mark.line + line_offset
                            if occ_vn.start_mark else None
                        )
                        chain.append({
                            "value": self._node_value_repr(loader, occ_vn),
                            "line": occ_line,
                        })
                # last_wins: walk the last local occurrence (merge sources never shadow local)
                kn_last = occurrences[-1][0]
                last_vn = occurrences[-1][1]
                edge = (
                    ictx["alias_edges"].get((id(node), id(kn_last)))
                    if ictx is not None else None
                )
                self._walk_node(
                    loader, last_vn, child_path, k_str, kn,
                    doc_index, line_offset,
                    entries, containers, key_lines, protected,
                    visiting, reasons,
                    duplicate=dup, override_chain=chain,
                    ictx=ictx, alias_arrival=edge,
                    alias_ctx=eff_alias_ctx, merged_ctx=merged_ctx,
                )

            # --- merge-expanded keys (not shadowed by local keys; ---
            # --- source value + source line + merged_from disclosed, ---
            # --- entry line number = the source line) ---
            for k_str in merge_order:
                mo = merge_occ.get(k_str)
                if mo is None:
                    continue
                skn = mo["kn"]
                svn = mo["vn"]
                child_path = path + [k_str]
                key_lines.append({
                    "line": (skn.start_mark.line + line_offset)
                            if skn.start_mark else None,
                    "col": skn.start_mark.column if skn.start_mark else 0,
                    "path": child_path, "kind": "key",
                    "doc_index": doc_index,
                })
                edge = (
                    ictx["alias_edges"].get((id(mo["src"]), id(skn)))
                    if ictx is not None else None
                )
                self._walk_node(
                    loader, svn, child_path, k_str, skn,
                    doc_index, line_offset,
                    entries, containers, key_lines, protected,
                    visiting, reasons,
                    ictx=ictx, alias_arrival=edge,
                    alias_ctx=eff_alias_ctx, merged_ctx=mo["mfrom"],
                )
            visiting.discard(id(node))
            return None

        if isinstance(node, yaml.SequenceNode):
            container = self._record_container(
                path, "list", doc_index, line, end_line,
                containers, key_node, line_offset,
                duplicate=duplicate, override_chain=override_chain,
            )
            _mark_record(container, node_anchor, eff_alias_ctx, merged_ctx)
            visiting.add(id(node))
            for idx, child in enumerate(node.value):
                edge = (
                    ictx["alias_edges"].get((id(node), idx))
                    if ictx is not None else None
                )
                self._walk_node(
                    loader, child, path + [str(idx)], key, None,
                    doc_index, line_offset,
                    entries, containers, key_lines, protected,
                    visiting, reasons,
                    ictx=ictx, alias_arrival=edge,
                    alias_ctx=eff_alias_ctx, merged_ctx=merged_ctx,
                )
            visiting.discard(id(node))
            return None

        # ScalarNode → entry
        value_state = cc.VALUE_STATE_SCALAR
        value = None
        raw_value = node.value
        tag_class = classify_tag(node.tag)

        # dangerous-tag node-level sentinel block — nothing
        # constructed, nothing executed, disclosed wholesale via
        # the loss reason UNSAFE_TAG_BLOCKED (safe but incomplete)
        if tag_class == "unsafe":
            reasons.append(f"unsafe_tag_blocked:{cc.path_str(path)}")
            entries.append(cc.make_entry(
                path, key, raw_value,
                {"_blocked_tag": node.tag},
                value_state=value_state,
                line_start=(node.start_mark.line + line_offset) if node.start_mark else None,
                line_end=(node.end_mark.line + line_offset) if node.end_mark else None,
                duplicate=duplicate,
                override_chain=override_chain,
                doc_index=doc_index,
                value_type=cc.VALUE_TYPE_BLOCKED_TAG,
                type_source=cc.TYPE_SOURCE_TAG,
            ))
            return

        if node.tag == "tag:yaml.org,2002:null":
            value_state = cc.VALUE_STATE_EXPLICIT_NULL
        else:
            try:
                value = loader.construct_object(node)
            except Exception:
                value = node.value

        # layered type disclosure (tag authoritative) + explicit lexical-trap registration
        value_type, type_source, hazard = derive_value_type(node)
        if tag_class == "custom":
            reasons.append(
                f"custom_tag_not_executed:{cc.path_str(path)}"
            )
        if hazard:
            reasons.append(
                f"lexical_hazard:{hazard}:{cc.path_str(path)}"
            )

        entry = cc.make_entry(
            path, key, raw_value, cc.json_safe(value),
            value_state=value_state,
            line_start=(node.start_mark.line + line_offset) if node.start_mark else None,
            line_end=(node.end_mark.line + line_offset) if node.end_mark else None,
            duplicate=duplicate,
            override_chain=override_chain,
            doc_index=doc_index,
            value_type=value_type,
            type_source=type_source,
        )
        # anchor/alias/merge/tagged structured stamping (additive; no keys added when absent)
        _mark_record(
            entry, node_anchor, eff_alias_ctx, merged_ctx,
            tagged=({"tag": node.tag} if tag_class == "custom" else None),
        )
        entries.append(entry)
        return entry
        # inline reference detection is deferred to resolve_references (from raw_value)

    def _record_container(
        self, path, container_type, doc_index, line, end_line,
        containers, key_node, line_offset,
        duplicate=False, override_chain=None,
    ):
        # container line numbers start at the key line (key and value usually share the line)
        start = line
        if key_node is not None and key_node.start_mark:
            start = key_node.start_mark.line + line_offset
        # child_count is backfilled by the caller after recursion → record 0 first, corrected at the end
        record = cc.make_container(
            path, container_type,
            child_count=0,
            line_start=start, line_end=end_line,
            duplicate=duplicate, override_chain=override_chain,
            doc_index=doc_index,
        )
        containers.append(record)
        return record

    @staticmethod
    def _node_value_repr(loader, node):
        """Compact representation of one occurrence value in an override chain."""
        if isinstance(node, yaml.MappingNode):
            return {"kind": "dict", "child_count": len(node.value)}
        if isinstance(node, yaml.SequenceNode):
            return {"kind": "list", "child_count": len(node.value)}
        try:
            return cc.json_safe(loader.construct_object(node))
        except Exception:
            return node.value

    # ----------------------------------------
    # comment scan and binding
    # ----------------------------------------

    def _scan_comments(
        self, text, entries, containers, key_lines,
        protected, comments, reasons,
    ):
        lines = text.splitlines()
        n = len(lines)

        # --- 1. comment blocks ---
        i = 0
        blocks = []
        while i < n:
            stripped = lines[i].lstrip()
            if stripped.startswith("#") and i not in protected:
                indent = len(lines[i]) - len(stripped)
                j = i
                while (j < n and lines[j].lstrip().startswith("#")
                       and j not in protected):
                    j += 1
                block_text = "\n".join(
                    ln.lstrip().lstrip("#").strip()
                    for ln in lines[i:j]
                )
                blocks.append((i, j - 1, indent, block_text))
                i = j
            else:
                i += 1

        # sort key_lines by line
        sorted_keys = sorted(
            (k for k in key_lines if k["line"] is not None),
            key=lambda k: k["line"],
        )

        bound_desc = {}  # path_str → (text, [start, end])

        for (s, e, indent, block_text) in blocks:
            target = None
            for k in sorted_keys:
                if k["line"] > e and k["col"] <= indent:
                    target = k
                    break
            raw = "\n".join(lines[s:e + 1])
            comments.append(cc.make_comment(
                "block", block_text, raw, s, e,
                bound_path=target["path"] if target else None,
                bound_kind=target["kind"] if target else None,
            ))
            if target:
                p = cc.path_str(target["path"])
                if p not in bound_desc:
                    bound_desc[p] = (block_text, [s, e])

        # --- 2. inline comments ---
        for k in sorted_keys:
            ln = k["line"]
            if ln >= n:
                continue
            idx = self._inline_comment_index(lines[ln], k["col"])
            if idx is not None:
                inline_text = lines[ln][idx:].lstrip("#").strip()
                comments.append(cc.make_comment(
                    "inline", inline_text, lines[ln][idx:], ln, ln,
                    bound_path=k["path"], bound_kind="key",
                ))
                p = cc.path_str(k["path"])
                if p not in bound_desc:
                    bound_desc[p] = (inline_text, [ln, ln])

        # --- 3. bind descriptions to entries/containers ---
        for record in entries + containers:
            desc = bound_desc.get(record["path_str"])
            if desc:
                record["description"] = desc[0]
                record["description_lines"] = desc[1]

        # --- 4. backfill container child_count ---
        cc.fill_child_counts(entries, containers)

        # empty containers (legal but informative) never enter loss
        # reasons — a fact, not a loss
        orphan_count = sum(1 for c in comments if c["bound_path"] is None)
        if orphan_count:
            reasons.append(f"orphan_comments:{orphan_count}")

    @staticmethod
    def _inline_comment_index(line, key_col):
        """Position of an inline '#' comment (quote-aware); returns None when there is none."""
        quote = None
        idx = max(key_col, 0)
        while idx < len(line):
            ch = line[idx]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "#" and idx > 0 and line[idx - 1] in (" ", "\t"):
                return idx
            idx += 1
        return None

    # ========================================
    # parse
    # ========================================

    def parse(self, file_path):
        """Parse a YAML file into a Document (incl. config-contract metadata)."""
        start_time = time.perf_counter()

        try:
            ctx = self._read_and_diagnose(
                file_path, "yaml", self.encoding, self.tolerant
            )
            text = ctx.text
            metadata = ctx.metadata

            # yaml parse
            try:
                documents = self._load_yaml(text)
                metadata["parse_success"] = True
            except Exception as e:
                documents = [{"_error": str(e)}]
                metadata["parse_success"] = False
                metadata["warnings"].append("yaml parse failed")

            metadata["document_count"] = len(documents)

            if len(documents) == 1:
                structured = documents[0]
            else:
                structured = documents

            if isinstance(structured, dict):
                metadata["top_level_keys"] = list(structured.keys())

            # ConfigEntry contract (fact layer; failure never blocks parsing)
            metadata["config_entries"] = self._build_contract(text)

            # object-level unresolved registry: derived from existing
            # fact sources — contract reference tri-states / last_wins
            # shadowed occurrences / yaml failure slices; never rewrites
            # original keys, written only when non-empty; guarded
            # silent degradation, never blocks the main parse.
            try:
                stamp_unresolved_registry(
                    metadata, "config_unresolved",
                    derive_config_unresolved(
                        metadata["config_entries"],
                        yaml_documents=documents,
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive guard
                _log.debug("config unresolved registry skipped: %s", exc)

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            metadata["parse_duration_ms"] = round(elapsed_ms, 3)

            return Document(
                text=text,
                metadata={**metadata, "structured_data": structured},
                source_text=ctx.source_text,
            )

        except Exception as e:
            metadata = {"parse_failed": True, "parse_error": str(e)}
            return Document(text="", metadata=metadata)

    # ========================================
    # multi-file cascade merge (kustomize/helm de-facto semantics)
    # ========================================

    def parse_many_merged(self, file_paths):
        """Multi-file cascade merge: later files shadow earlier ones (kustomize/helm de-facto standard).

        Each file goes through the existing single-file contract
        channel (with all disclosure semantics); the core cascade
        merge converges on the shared helper ``cc.merge_contracts``.
        The yaml-family tail steps stay in this parser: schema/hints
        cross-file propagation (later declarations win; structural
        expectations re-run on the merged entries/containers) +
        structured_data dict deep merge (lists replaced wholesale,
        later file wins).

        Returns the merged contract dict (not a Document — no single
        raw text across files; line-number semantics rely on
        source_file). The base parse_many semantics stays frozen.
        """
        docs = [self.parse(fp) for fp in file_paths]
        contracts = []
        sources = []
        for i, doc in enumerate(docs):
            meta = doc.metadata
            c = meta.get("config_entries") or cc.build_contract(
                "yaml", cc.DUP_LAST_WINS)
            contracts.append(c)
            sources.append({
                "index": i,
                "file_name": meta.get("file_name"),
                "sha256_prefix": (meta.get("sha256") or "")[:12] or None,
            })

        # the core cascade merge converges on the shared helper
        # (behavioral equivalence guarded by the existing panel +
        # full regression suite)
        merged = cc.merge_contracts("yaml", contracts, sources)
        merged_entries = merged["entries"]
        merged_containers = merged["containers"]

        # merge-layer schema disclosure is factually recomputed by
        # apply_schema_status below after the merge (single owner) —
        # the per-file transmitted unverifiable reason is stripped
        # first; otherwise the union + re-run append would double it,
        # and loser declarations would leak in, contradicting the
        # header.
        merged["loss"]["reasons"] = [
            r for r in merged["loss"]["reasons"]
            if not r.startswith("schema_declared_unverifiable:")
        ]

        # schema/hints cross-file propagation (later declarations
        # win; structural expectations re-run on the merged
        # entries/containers — consistent with last_file_wins)
        merged_hints = []
        seen_hints = set()
        for c in contracts:
            for h in c.get("domain_hints") or []:
                if h not in seen_hints:
                    seen_hints.add(h)
                    merged_hints.append(h)
        if merged_hints:
            merged["domain_hints"] = merged_hints
        merged_schema_ref = None
        for c in contracts:
            r = c.get("schema_ref")
            if r:
                merged_schema_ref = r
        cc.apply_schema_status(
            merged, merged_schema_ref, merged_hints,
            merged_entries, merged_containers,
            merged["loss"]["reasons"],
        )

        # structured_data deep merge (dicts recursive, lists replaced wholesale)
        def _deep_merge(a, b):
            if isinstance(a, dict) and isinstance(b, dict):
                out = dict(a)
                for k, v in b.items():
                    out[k] = _deep_merge(out[k], v) if k in out else v
                return out
            return b

        structured = None
        for doc in docs:
            sd = doc.metadata.get("structured_data")
            if not isinstance(sd, dict):
                continue
            structured = sd if structured is None else _deep_merge(structured, sd)
        if structured is not None:
            merged["structured_data"] = structured
        return merged
