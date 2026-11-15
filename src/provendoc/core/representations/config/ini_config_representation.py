# ============================================================
# ini_config_representation.py
# Compatibility shim — unified implementation in config_representation.py
# ============================================================
from provendoc.core.representations.config.config_representation import (
    ConfigRepresentation,
    IniConfigRepresentation,
)

__all__ = ["IniConfigRepresentation", "ConfigRepresentation"]
