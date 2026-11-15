"""Code-domain representations.

CodeRepresentation materializes maximal non-overlapping AST and component
boundaries (functions, classes, interfaces, rules, Vue/HTML blocks) and retains
all remaining top-level code as module fragments. Dependency-aware and cross-file
chunking remain future work.
"""

from provendoc.core.representations.code.code_representation import (
    CodeRepresentation,
)

__all__ = ["CodeRepresentation"]
