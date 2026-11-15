# ============================================================
# toml_config_representation.py
# Compatibility shim — unified implementation in config_representation.py
# ============================================================
from provendoc.core.representations.config.config_representation import (
    ConfigRepresentation,
    TomlConfigRepresentation,
)

__all__ = ["TomlConfigRepresentation", "ConfigRepresentation"]
