# ============================================================
# Narrative Domain — RTF family package (split off rtf_parser.py on 2026-11-10)
# ============================================================
# Three-layer layout: facts.py (fact registration, pure data)
# -> scanner.py (state-machine core) -> adapters.py (boundary adapters).
# The external rtf_parser.py facade stays unchanged (parser_registry
# module paths and test imports: zero drift).
