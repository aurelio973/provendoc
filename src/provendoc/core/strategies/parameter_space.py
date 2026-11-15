"""ParameterSpace — dynamic parameter management for RepresentationPolicy.

Lets users specify partial overrides (just size_soft_limit, just overlap, etc.)
and builds complete RepresentationPolicy objects with sensible defaults for
unspecified params. Validates parameter combinations before construction.

Supports both legacy field names (chunk_size, overlap) and native
RepresentationPolicy field names (size_soft_limit, boundary_preference, etc.).

Used by:
    - evaluate(): user-specified params → validated RepresentationPolicy
    - auto_pipeline(): internal optimization search space
"""

from provendoc.core.strategies.representation_policy import (
    RepresentationPolicy,
)


class ParameterSpace:
    """Dynamic parameter management for representation policies.

    Supports two parameter layers:
        1. Legacy (backward-compatible): chunk_size, overlap, orphan_threshold,
           paragraph_merge, preserve_headings, collapse_blank_lines, long_text_strategy
        2. Native RepresentationPolicy: size_soft_limit, size_hard_limit,
           boundary_primary, context_mode, discourse_unit_target, paragraph_target

    Legacy params are automatically routed to the appropriate layer
    via RepresentationPolicy.from_legacy().

    Example:
        # user specifies only chunk_size and overlap (legacy)
        strategy = ParameterSpace.build(
            chunk_size=200,
            overlap=50,
        )

        # user specifies native params
        strategy = ParameterSpace.build(
            size_soft_limit=384,
            boundary_primary="heading_boundary",
            context_mode="selective",
        )
    """

    # ==================================
    # defaults — native RepresentationPolicy params
    # ==================================

    _NATIVE_DEFAULTS = {
        "name": "custom",
        "size_soft_limit": 384,
        "size_hard_limit": 640,  # aligned with RuntimePackagingPolicy/RunConfig defaults
        # Note: with no args or legacy-only args these two values are shadowed
        # by the from_legacy derivation of _LEGACY_DEFAULTS' chunk_size
        # (soft=cs, hard=cs*1.6); kept for documentation-level semantic
        # alignment only.
        "boundary_primary": "discourse_unit",
        "context_mode": "selective",
        "overlap_size": 0,        # context_propagation.overlap_size; no overlap by default
        "discourse_unit_target": 5,
        "paragraph_target": 3,
        "merge_short_sibling_sections": False,
        "short_section_ratio": 0.30,
        "hierarchy_continuity": True,
        "structural_block_complete": True,
        "discourse_locality": True,
    }

    # ==================================
    # legacy field defaults (fallback for the from_legacy() path)
    # ==================================

    _LEGACY_DEFAULTS = {
        # chunk_size default 300 → 640 (2026-10-01): evaluate() with no
        # args goes through build() → from_legacy(chunk_size=cs)
        # deriving soft=cs, hard=int(cs*1.6),
        # get_effective_chunk_size()=cs. The old default 300 gave
        # soft=300/hard=480 — recall@1 0.8006 on the frozen benchmark
        # corpus vs 0.8131 for cs=640 (= the frozen baseline,
        # diag_round2_result.json); 640 exactly reproduces the frozen
        # provendoc_default behavior (1299 blocks). The round-2
        # pre-registered grid (500/640/800) tied entirely; picking
        # 640 among ties is a product decision, aligned with
        # RunConfig packaging_size_hard_limit=640.
        "chunk_size": 640,
        "overlap": 30,
        "orphan_threshold": 120,
        "paragraph_merge": True,
        "preserve_headings": True,
        "collapse_blank_lines": True,
        "long_text_strategy": "sliding_window",
    }

    # All valid param names
    _VALID_PARAMS = set(_NATIVE_DEFAULTS.keys()) | set(_LEGACY_DEFAULTS.keys())

    # ==================================
    # build
    # ==================================

    @classmethod
    def build(cls, **overrides):
        """Build a RepresentationPolicy from partial user overrides.

        Accepts both legacy and native parameter names. Unspecified params
        use defaults. All params are validated before construction.

        Args:
            **overrides: any subset of valid params (see _VALID_PARAMS)

        Returns:
            RepresentationPolicy

        Raises:
            ValueError: if unknown params are passed or values fail validation
        """
        # check for unknown params
        unknown = set(overrides.keys()) - cls._VALID_PARAMS
        if unknown:
            raise ValueError(
                f"Unknown parameters: {unknown}. "
                f"Valid params: {cls._VALID_PARAMS}"
            )

        # explicitly mixing legacy chunk_size with native size params
        # would shadow each other — reject outright
        explicit_native_size = (
            {"size_soft_limit", "size_hard_limit"} & set(overrides)
        )
        if "chunk_size" in overrides and explicit_native_size:
            raise ValueError(
                "conflicting parameters: 'chunk_size' cannot be combined "
                f"with {sorted(explicit_native_size)} — use one parameter "
                "style (legacy OR native), not both"
            )

        # merge with defaults
        params = {**cls._LEGACY_DEFAULTS, **cls._NATIVE_DEFAULTS, **overrides}

        # validate
        issues = cls.validate(params)
        if issues:
            raise ValueError(
                f"Invalid parameters: {'; '.join(issues)}"
            )

        # Build RepresentationPolicy
        # Route legacy params through from_legacy, then apply native overrides
        legacy_params = {
            k: params[k] for k in cls._LEGACY_DEFAULTS if k in params
        }
        # fix parameter shadowing: when native size params
        # (size_soft_limit / size_hard_limit) are passed explicitly,
        # the legacy chunk_size default is dropped, otherwise
        # packaging.chunk_size would shadow size_hard_limit
        # (get_effective_chunk_size returns a non-None chunk_size
        # first).
        if explicit_native_size:
            legacy_params.pop("chunk_size", None)
        legacy_params["name"] = params.get("name", "custom")

        policy = RepresentationPolicy.from_legacy(**legacy_params)

        # Apply native overrides
        if "size_soft_limit" in overrides:
            policy.packaging.size_soft_limit = overrides["size_soft_limit"]
        if "size_hard_limit" in overrides:
            policy.packaging.size_hard_limit = overrides["size_hard_limit"]
        if "boundary_primary" in overrides:
            policy.boundary_preference.primary = overrides["boundary_primary"]
        if "context_mode" in overrides:
            policy.context_propagation.mode = overrides["context_mode"]
        if "overlap_size" in overrides:
            policy.context_propagation.overlap_size = overrides["overlap_size"]
        if "discourse_unit_target" in overrides:
            policy.semantic_density.discourse_unit_target = overrides["discourse_unit_target"]
        if "paragraph_target" in overrides:
            policy.semantic_density.paragraph_target = overrides["paragraph_target"]
        if "merge_short_sibling_sections" in overrides:
            policy.boundary_preference.merge_short_sibling_sections = (
                overrides["merge_short_sibling_sections"]
            )
        if "short_section_ratio" in overrides:
            policy.boundary_preference.short_section_ratio = overrides[
                "short_section_ratio"
            ]
        if "hierarchy_continuity" in overrides:
            policy.invariants.hierarchy_continuity = overrides[
                "hierarchy_continuity"
            ]
        if "structural_block_complete" in overrides:
            policy.invariants.structural_block_complete = overrides[
                "structural_block_complete"
            ]
        if "discourse_locality" in overrides:
            policy.invariants.discourse_locality = overrides[
                "discourse_locality"
            ]

        return policy

    # ==================================
    # validate
    # ==================================

    @classmethod
    def validate(cls, params):
        """Validate parameter values.

        Returns list of issue strings (empty if all valid).
        Does NOT raise — caller decides what to do.
        """
        issues = []

        # Legacy param checks
        if "chunk_size" in params:
            v = params["chunk_size"]
            if not isinstance(v, int) or v <= 0:
                issues.append("chunk_size must be a positive integer")

        if "overlap" in params:
            v = params["overlap"]
            if not isinstance(v, int) or v < 0:
                issues.append("overlap must be a non-negative integer")

        if "orphan_threshold" in params:
            v = params["orphan_threshold"]
            if not isinstance(v, int) or v < 0:
                issues.append("orphan_threshold must be a non-negative integer")

        if "long_text_strategy" in params:
            v = params["long_text_strategy"]
            if v not in ("sliding_window",):
                issues.append(
                    f"long_text_strategy '{v}' not supported "
                    f"(only: sliding_window)"
                )

        # Native param checks
        if "size_soft_limit" in params:
            v = params["size_soft_limit"]
            if not isinstance(v, int) or v <= 0:
                issues.append("size_soft_limit must be a positive integer")

        if "size_hard_limit" in params:
            v = params["size_hard_limit"]
            if not isinstance(v, int) or v <= 0:
                issues.append("size_hard_limit must be a positive integer")

        if "boundary_primary" in params:
            v = params["boundary_primary"]
            valid = ("discourse_unit", "heading_boundary", "structural_block", "token_limit")
            if v not in valid:
                issues.append(
                    f"boundary_primary '{v}' not in {valid}"
                )

        if "context_mode" in params:
            v = params["context_mode"]
            valid = ("none", "selective", "heading_only", "full")
            if v not in valid:
                issues.append(
                    f"context_mode '{v}' not in {valid}"
                )

        if "overlap_size" in params:
            v = params["overlap_size"]
            if not isinstance(v, int) or v < 0:
                issues.append("overlap_size must be a non-negative integer")

        if "discourse_unit_target" in params:
            v = params["discourse_unit_target"]
            if not isinstance(v, int) or v <= 0:
                issues.append("discourse_unit_target must be a positive integer")

        if "paragraph_target" in params:
            v = params["paragraph_target"]
            if not isinstance(v, int) or v <= 0:
                issues.append("paragraph_target must be a positive integer")

        if "merge_short_sibling_sections" in params:
            v = params["merge_short_sibling_sections"]
            if not isinstance(v, bool):
                issues.append("merge_short_sibling_sections must be a boolean")

        if "short_section_ratio" in params:
            v = params["short_section_ratio"]
            if (isinstance(v, bool) or not isinstance(v, (int, float))
                    or not 0 < v <= 1):
                issues.append("short_section_ratio must be within (0, 1]")

        for name in (
            "hierarchy_continuity",
            "structural_block_complete",
            "discourse_locality",
        ):
            if name in params and not isinstance(params[name], bool):
                issues.append(f"{name} must be a boolean")

        # Cross-param validation
        chunk_size = params.get("chunk_size", cls._LEGACY_DEFAULTS["chunk_size"])
        overlap = params.get("overlap", cls._LEGACY_DEFAULTS["overlap"])
        if (
            isinstance(chunk_size, int)
            and isinstance(overlap, int)
            and overlap >= chunk_size
        ):
            issues.append(
                f"overlap ({overlap}) must be less than "
                f"chunk_size ({chunk_size})"
            )

        token_soft = params.get("size_soft_limit")
        token_hard = params.get("size_hard_limit")
        if (isinstance(token_soft, int) and isinstance(token_hard, int)
            and token_hard < token_soft):
            issues.append(
                f"size_hard_limit ({token_hard}) must be >= "
                f"size_soft_limit ({token_soft})"
            )

        if (params.get("merge_short_sibling_sections")
                and params.get("boundary_primary") != "heading_boundary"):
            issues.append(
                "merge_short_sibling_sections requires "
                "boundary_primary='heading_boundary'"
            )

        return issues
