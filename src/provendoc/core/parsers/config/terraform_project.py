# ============================================================
# Structured Config Domain
# Terraform Project Static View (project_file_set / load_order /
# variable merge candidates / conflict candidates)
# ============================================================
"""Terraform directory-level static view layer.

Position: a **pure-function layer with a zero-IO red line** — it
never scans directories on its own, performs no filesystem IO,
and reads no environment variables (TF_VAR_*); the inputs are
the same-directory file-name list provided by the caller plus
each file's already-parsed ``terraform_profile`` (or None),
and the output is a static view structure. The single-file
parsers (hcl_parser / hcl_json_parser) keep their zero-IO
boundary untouched.

Honesty boundary:

- ``load_order`` is a **candidate** order, not the actual
  loading fact — filename order is never treated as resource
  semantic order;
- Variable merging only registers the precedence order and
  same-name conflict **candidates**; the final variable values
  are never computed (``effective_status: "context_dependent"`` —
  without an execution context the effective values are never
  directly computed);
- Cross-file duplicate blocks may be legal or conflicting —
  registered, never judged;
- Command-line -var-file / TF_VAR_* environment variables are
  **invisible** at the static layer, registered only as
  declarative entries in the precedence order (the context
  channel, values unknowable).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

# HCL-family suffixes participating at directory level (kept in
# lockstep with the parser_registry compound-suffix list)
_TF_SUFFIXES = (
    ".tf", ".tf.json", ".tftest.hcl", ".tftest.json",
)
_TFVARS_AUTO_SUFFIXES = (".auto.tfvars",)
_TFVARS_EXPLICIT_SUFFIXES = (".tfvars", ".tfvars.json")

# backup/temporary file candidates (hidden and backup files do
# not necessarily participate in loading)
_BACKUP_RE = re.compile(
    r"\.(?:bak|backup|orig|old|tmp|swp)$|~$"
)

# Variable precedence order (a declarative candidate order, not
# a computed result)
_VARIABLE_PRECEDENCE = (
    "terraform.tfvars",        # 1. explicit default file
    "*.auto.tfvars",           # 2. auto files (lexicographic)
    "-var-file",               # 3. command line (statically invisible, context declaration)
    "TF_VAR_*",                # 4. environment variables (zero IO, never read)
    "command_line",            # 5. command-line -var (statically invisible)
)


def _lower_name(name: str) -> str:
    return (name or "").replace("\\", "/").split("/")[-1].lower()


def _classify_file(base: str) -> Optional[str]:
    """Filename → project-level file_role (a candidate, never a
    replacement for syntax parsing).

    None = not an HCL-family file (the caller decides whether to
    count it into the file set).
    """
    if base == "terraform.tfvars":
        return "default_variables"
    for suffix in _TFVARS_AUTO_SUFFIXES:
        if base.endswith(suffix):
            return "auto_variables"
    for suffix in _TFVARS_EXPLICIT_SUFFIXES:
        if base.endswith(suffix):
            return "explicit_variables_file"
    for suffix in _TF_SUFFIXES:
        if base.endswith(suffix):
            if base.endswith((".tftest.hcl", ".tftest.json")):
                return "test_configuration"
            return "configuration"
    return None


def build_project_view(
    file_names: Sequence[str],
    profiles: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Same-directory file list + already-parsed profiles → the
    directory-level static view.

    Pure function, zero IO: no filesystem reads, no environment
    variables, no Terraform semantics executed. ``profiles`` is
    keyed by filename (same basis as ``file_names``), values are
    that file's ``terraform_profile`` (missing/None tolerated —
    unparsed or failed-to-parse files still enter the file set,
    the absent semantic fields being the honest state).
    """
    profiles = profiles or {}
    base_names: List[str] = []
    seen = set()
    for name in file_names or []:
        base = _lower_name(name)
        if base not in seen:
            seen.add(base)
            base_names.append(base)

    project_file_set: List[Dict[str, Any]] = []
    candidate_loaded_files: List[str] = []
    ignored_files: List[Dict[str, Any]] = []
    file_roles: Dict[str, str] = {}

    tf_config_files: List[str] = []
    tfvars_default: List[str] = []
    tfvars_auto: List[str] = []
    tfvars_explicit: List[str] = []
    test_files: List[str] = []

    for base in base_names:
        role = _classify_file(base)
        profile = profiles.get(base) or {}
        profile_role = profile.get("file_role")
        if profile_role:
            role = profile_role  # the syntax-layer profile wins (it
            # cannot be replaced by the filename)
        entry = {
            "file": base,
            "file_role": role,
            "source_variant": profile.get("source_variant"),
            "profile_version": profile.get("profile_version"),
        }
        project_file_set.append(entry)
        if role:
            file_roles[base] = role

        # ignore candidates: hidden files (starting with ., e.g.
        # .terraform/) / backup files
        if base.startswith(".") or _BACKUP_RE.search(base):
            ignored_files.append({
                "file": base,
                "ignore_reason": (
                    "hidden_file" if base.startswith(".")
                    else "backup_file_candidate"
                ),
                # "do not necessarily participate in loading" —
                # candidates, never judged
                "certain": False,
            })
            continue

        if role == "configuration":
            tf_config_files.append(base)
            candidate_loaded_files.append(base)
        elif role == "default_variables":
            tfvars_default.append(base)
            candidate_loaded_files.append(base)
        elif role == "auto_variables":
            tfvars_auto.append(base)
            candidate_loaded_files.append(base)
        elif role in ("explicit_variables_file",):
            # explicit tfvars participate only when -var-file names
            # them — the static layer cannot see the command line
            # → not in candidate_loaded, registered separately
            tfvars_explicit.append(base)
        elif role == "test_configuration":
            # tftest files are driven explicitly by terraform test,
            # not part of the module loading surface
            test_files.append(base)

    # variable merge candidates (precedence order declared, final
    # values never computed)
    tfvars_auto_sorted = sorted(tfvars_auto)
    variable_merge_candidates = {
        "precedence_order": list(_VARIABLE_PRECEDENCE),
        "static_sources": {
            "terraform.tfvars": tfvars_default,
            "*.auto.tfvars": tfvars_auto_sorted,
            "explicit_files_not_auto_loaded": tfvars_explicit,
        },
        "context_only_sources": ["-var-file", "TF_VAR_*", "command_line"],
        "effective_status": "context_dependent",
    }

    # same-name variable cross-file conflict candidates (values
    # never adjudicated, registered only)
    variable_assignments: Dict[str, List[str]] = {}
    for base in list(tfvars_default) + tfvars_auto_sorted + tfvars_explicit:
        profile = profiles.get(base) or {}
        for vv in profile.get("variable_values") or []:
            name = vv.get("variable")
            if name:
                variable_assignments.setdefault(name, []).append(base)
    variable_conflicts = [
        {"variable": name, "files": files, "outcome": "context_dependent"}
        for name, files in sorted(variable_assignments.items())
        if len(files) > 1
    ]

    # cross-file duplicate-block conflict candidates (may be
    # legal or conflicting)
    declared: Dict[str, List[str]] = {}
    for base in tf_config_files + tfvars_default + tfvars_auto_sorted:
        profile = profiles.get(base) or {}
        for block in profile.get("blocks") or []:
            bt = block.get("block_type")
            labels = block.get("labels") or []
            if bt in ("variable", "output", "provider", "module",
                      "resource", "data", "check", "moved", "import"):
                address = ".".join([bt] + [str(x) for x in labels])
                declared.setdefault(address, []).append(base)
    conflict_candidates = [
        {
            "address": address,
            "files": files,
            "note": (
                "duplicate_block_may_be_legal_or_conflict"
            ),
            "outcome": "context_dependent",
        }
        for address, files in sorted(declared.items())
        if len(files) > 1
    ]

    # load_order candidate sequence (declarative; filename order
    # is not resource semantic order)
    load_order = {
        "candidate_sequence": (
            sorted(tf_config_files)
            + tfvars_default
            + tfvars_auto_sorted
        ),
        "effective_status": "context_dependent",
        "disclosure": (
            "filename_order_is_not_resource_semantic_order"
        ),
    }

    return {
        "view_version": "terraform_project_view_p2b",
        "project_file_set": project_file_set,
        "candidate_loaded_files": candidate_loaded_files,
        "ignored_files": ignored_files,
        "file_roles": file_roles,
        "load_order": load_order,
        "variable_merge_candidates": variable_merge_candidates,
        "variable_conflict_candidates": variable_conflicts,
        "conflict_candidates": conflict_candidates,
        "test_files": test_files,
        "disclosures": {
            "zero_io_static_view_only": True,
            "no_directory_scan_performed": True,
            "effective_values_context_dependent": True,
            "filename_order_not_semantic_order": True,
        },
    }


# ---- project-level Markdown summary (auxiliary view) ----

def render_project_summary(view: Optional[Mapping[str, Any]]) -> str:
    """project view → the Markdown Project Files section
    (auxiliary view)."""
    if not view:
        return ""
    lines = ["### Project Files", ""]
    loaded = view.get("candidate_loaded_files") or []
    ignored = view.get("ignored_files") or []
    lines.append(f"- Candidate loaded files: {len(loaded)}")
    if loaded:
        lines.append(f"  - `{', '.join(loaded[:10])}`")
    lines.append(f"- Ignored file candidates: {len(ignored)}")
    merge = view.get("variable_merge_candidates") or {}
    static_sources = (merge.get("static_sources") or {})
    lines.append(
        "- Variable merge candidates: precedence order is declarative; "
        "effective values are `context_dependent` (never computed)."
    )
    for source, files in static_sources.items():
        if files:
            lines.append(f"  - {source}: {', '.join(files[:10])}")
    conflicts = view.get("conflict_candidates") or []
    var_conflicts = view.get("variable_conflict_candidates") or []
    if conflicts or var_conflicts:
        lines.append(
            f"- Conflict candidates: {len(conflicts)} block-level, "
            f"{len(var_conflicts)} variable-level "
            "(may be legal or conflicting — registered, not judged)."
        )
    test_files = view.get("test_files") or []
    if test_files:
        lines.append(
            f"- Test configuration files: {', '.join(test_files[:10])} "
            "(driven by `terraform test`; `apply` registered, never "
            "executed)."
        )
    lines.append("")
    return "\n".join(lines)
