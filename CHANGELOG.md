# Changelog

## 0.1.0 (2026-11-15)

Initial public code release.

- Parser core with a single domain registry routing 142 file suffixes
  into five ingestion domains: narrative, structured_config,
  spreadsheet, code, and record_stream.
- Per-domain semantic representations with per-chunk provenance (source
  offsets, parser, fact codes), disclosed sanitization actions, and the
  honest-failure contract.
- Invisible-carrier census at the parser layer (observation-only).
- SDK (`Provendoc` facade: `parse` / `chunk` / `evaluate` /
  `auto_pipeline`), CLI (`provendoc`), and REST app
  (`provendoc-serve`).
- Optional extras for PDF, Word, layout-aware PDF, spreadsheets,
  parquet, OCR, REST, and ecosystem adapters (LangChain / LlamaIndex).

This release contains the code only. The test suite, benchmark
corpora, preregistered evaluation kit, and design-spec documents are
maintained in the development tree and are planned for later releases.
