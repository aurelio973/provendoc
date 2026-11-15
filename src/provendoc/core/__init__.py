"""Provendoc Core — the pipeline and strategy layers.

Sub-packages:
    parsers/          — document parsers (txt, md, html, pdf, csv, excel, ...)
    cleaning/         — text cleaning (encoding repair, control chars, unicode normalization)
    diagnostics/      — diagnostic signals (encoding, unicode, structural, quality)
    domain/           — domain classification and registry
    evaluation/       — evaluation infrastructure (shared primitives)
    exporters/        — output exporters (json, markdown, raw)
    foundation/       — foundational models (metadata, serialization)
    pipeline/         — unified parse pipeline
    recovery/         — document recovery (PDF noise filter, reading order, ...)
    representations/  — semantic representations (narrative, config, table, event, code)
    semantics/        — semantic primitives (signals, relations, primitives)
    strategies/       — strategy layer (RepresentationPolicy, CorpusStratifier, ...)
    text/             — text processing (decoding, normalization, markdown_blocks)
"""
