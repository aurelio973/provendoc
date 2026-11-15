"""Config representations package.

Unified implementation: config_representation.ConfigRepresentation
(fine/base/coarse granularities + raw/retrieval_text split).

Format subclasses keep their existing class names for client /
universal_parser dispatch and metadata["representation"]
assertions:
    - JsonConfigRepresentation
    - YamlConfigRepresentation
    - TomlConfigRepresentation
    - XmlConfigRepresentation
    - IniConfigRepresentation
"""

from provendoc.core.representations.config.config_representation import (
    ConfigRepresentation,
    IniConfigRepresentation,
    JsonConfigRepresentation,
    TomlConfigRepresentation,
    XmlConfigRepresentation,
    YamlConfigRepresentation,
)

__all__ = [
    "ConfigRepresentation",
    "JsonConfigRepresentation",
    "YamlConfigRepresentation",
    "TomlConfigRepresentation",
    "XmlConfigRepresentation",
    "IniConfigRepresentation",
]
