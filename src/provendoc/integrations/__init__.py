"""Ecosystem adapters — LangChain and LlamaIndex integrations.

Optional, dependency-isolated: the ecosystem packages are imported
only inside the adapter modules, so installing Provendoc never
pulls them in. Import the adapter submodule directly:

    from provendoc.integrations.langchain import ProvendocLoader
    from provendoc.integrations.llamaindex import ProvendocReader

Both map ``IngestionResult`` semantic units to ecosystem documents
through the same shared conversion layer
(``integrations._common.result_to_items``), so the two ecosystems
always expose identical chunk text and metadata.
"""
from typing import Any

_LAZY_EXPORTS = {
    "ProvendocLoader": "provendoc.integrations.langchain",
    "ProvendocReader": "provendoc.integrations.llamaindex",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazy re-export (PEP 562) — ecosystem imports stay opt-in."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    import importlib

    module = importlib.import_module(module_name)
    return getattr(module, name)
