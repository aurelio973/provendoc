# Provendoc

[English](README.md) | [简体中文](README.zh-CN.md)

Messy documents in, retrieval-ready chunks out — with receipts.

Point it at real-world files (PDF, Word, PowerPoint, spreadsheets, code,
logs, configs, ebooks), get back semantically chunked text for RAG, and
know exactly where every chunk came from and where the pipeline is unsure.

Why that matters: most ingest tools silently mangle documents — tables
split mid-row, headings swallowed, scanned pages degraded into garbage
chunks. Provendoc either parses it correctly or fails loudly with
auditable diagnostics; every sanitization action is disclosed, and every
chunk carries provenance (source offsets, parser, fact codes) so
retrieval mistakes can be traced back to the source.

Every file is routed by a single domain registry
(`src/provendoc/core/domain/domain_registry.py`) into one of five
ingestion domains: `narrative`, `structured_config`, `spreadsheet`,
`code`, and `record_stream`. Each domain defines its own parsing,
representation, evaluation, and diagnostic behavior.

## In 30 seconds

```python
from provendoc import Provendoc

res = Provendoc().chunk("anything.pdf")   # pdf / docx / xlsx / py / log / yaml ...

for text in res.texts:        # just the text -> list[str]
    print(text)

for unit in res.chunks:       # offsets and provenance -> always a list
    print(unit.text[:40], unit.metadata["source_coordinates"])
```

Three sentences:

1. `chunk()` returns semantically chunked units, `parse()` returns the
   full document, `evaluate()` additionally returns quality feedback,
   `auto_pipeline()` additionally auto-tunes parameters.
2. `res.chunks` is always a list — for all five domains and all 142
   supported suffixes alike.
3. Missing optional backends degrade honestly (auditable fallback),
   never crash.

Honest degradation is concrete, not a slogan — run this with a
core-only install (no `provendoc[pdf]`):

```python
from provendoc import Provendoc

res = Provendoc().parse("report.pdf")
print(res.document.metadata["parse_failed"])        # True
print(res.document.metadata["missing_backend"])
# {'missing_module': 'fitz', 'pip_extra': 'pdf',
#  'install_hint': 'pip install "provendoc[pdf]" (or provendoc[all])'}
```

No exception, no garbage chunks — the reason nothing was parsed is
recorded in the document facts, together with the exact fix. A file
that *lies about its own format* (e.g. a fake `.pdf`) is caught by
the format-verification gate and fails with the observed-vs-declared
evidence attached.

## Install

Provendoc is not yet on PyPI; install from source:

```bash
git clone <this-repo> provendoc && cd provendoc
pip install -e .                  # core: txt / md / configs / code / logs, no heavy deps
pip install -e ".[pdf,docx]"      # add only the backends you need
pip install -e ".[all]"           # every optional backend
```

Optional extras, install only what you ingest:

| Extra | Adds |
| --- | --- |
| `pdf` | PDF parsing (PyMuPDF; AGPL — see [License](#license)) |
| `pdf-layout` | MinerU layout analysis for complex PDFs |
| `docx` / `doc` | OOXML / legacy binary Word |
| `spreadsheet` | pandas engine + `.xlsb` / `.ods` backends |
| `parquet` | `.parquet` (pyarrow) |
| `code` | tree-sitter AST chunking for source code |
| `api` | FastAPI REST server (`provendoc-serve`) |
| `image-ocr` | OCR for scanned pages (needs Tesseract data) |
| `langchain` / `llama-index` | retriever-framework adapters |

Missing optional backends degrade honestly (auditable fallback) instead
of crashing — see the example above.

## Quickstart (SDK)

```python
from provendoc import Provendoc

ad = Provendoc()

result = ad.parse("file.pdf")                       # pure parsing
result = ad.chunk("file.md")                        # semantic chunking

# res.chunks is always List[RepresentationUnit], whichever of the
# five domains produced it.
for unit in result.chunks:
    print(unit.unit_type, unit.metadata.get("char_start"), unit.metadata.get("char_end"))

# Merged or rewritten units carry no char_start/char_end; they only
# keep the coordinate layer, possibly with an explicit note on why
# verbatim positioning is impossible.
if result.chunks:
    coords = result.chunks[0].metadata["source_coordinates"]
    print(coords["coordinate_system"], coords["char_span"])

# Coarse/fine granularity: pick a layer explicitly; always a list, [] when absent.
coarse = result.chunks_at("coarse")
fine = result.chunks_at("fine")

# Dependency-free outputs
texts = ad.chunk_texts("file.md")                   # ["chunk 1", "chunk 2", ...]
items = ad.chunk_dicts("file.md")                   # [{"text": ..., "metadata": {...}}, ...]

# Quality feedback (RSE three-layer envelope)
result = ad.evaluate("file.txt", chunk_size=200, overlap=50)
score = result.metadata["benchmark"]["rse"]["quality_profile"]["quality_score"]

# Evaluation + tuning
result = ad.auto_pipeline("file.txt")

# Code domain: cross-file semantics (opt-in, read-only)
result = ad.chunk("main.py", project_root="./src")  # chunks gain project_references
index = ad.index_project("./src")                   # whole-project cross-file index
```

In the `benchmark["evaluation"]` dict, read `invariant_composite`; the
legacy six-dimension `overall` was retired and stays 0.0.

## Supported formats

- narrative — txt, md, markdown, mdx, pdf, docx, doc, docm, dot, dotx,
  dotm, rtf, tex, rst, qmd, rmd, epub, pptx, pptm, ppsx, ppsm, potx,
  potm, ppt, pps, pot, odp, fodp, otp, sti, png, jpg, jpeg, webp, bmp,
  gif, tif, tiff
- structured_config — json, yaml, yml, toml, ini, cnf, conf, cfg, xml,
  properties, env, envrc, plist, editorconfig / gitconfig / npmrc
  families, tf, tfvars, tf.json, tftest.hcl, cargo.lock
- spreadsheet — csv, tsv, psv, xlsx, xls, xlsm, xlsb, ods,
  parquet (pyarrow extra)
- code — py, js, ts, jsx, tsx, vue, html, css, java, go, php, c/cpp,
  cs, rs, lua, dart, r, perl, zig, less, hcl, bash/powershell, sql,
  proto, swift/kotlin/scala/ruby, cmake/makefile, ...
  (48 suffixes total; single source of truth:
  `core/domain/domain_registry.py`)
- record_stream — log, jsonl, ndjson

Note: `.html/.htm` belong to the code domain (tolerant parsing +
AST/component chunking), not the narrative pipeline. Unknown suffixes
fall through to the tolerant text parser with a disclosed
`no_magic_expected` verdict instead of erroring out; a declared suffix
whose observed content contradicts it (e.g. a fake `.docx`) fails
honestly.

Archives (zip / tar / 7z-family compressed members) are expanded at the
collection stage: each member flows through its own domain pipeline with
a virtual path preserved for provenance, with nesting depth / member
count / size guards and auditable expansion facts.

## Why Provendoc

- **Evidence-first.** Source text is preserved untouched; every
  sanitization action on working text is disclosed in a registry plus
  warnings; every chunk carries provenance (source offsets, parser,
  fact codes).
- **Honest failure.** Unsupported or corrupt files fail loudly with
  auditable diagnostics instead of silently degrading into garbage
  chunks.
- **Domain-aware chunking.** Five ingestion domains with per-domain
  semantic representations — narrative prose, config key-value trees,
  spreadsheets, code (tree-sitter AST), and record streams (log-format
  family recognition: CLF / W3C / syslog / logfmt; JSONL envelope
  fingerprints).
- **Injection-carrier census.** An invisible-carrier census (zero-width
  characters, bidi controls, ANSI escapes, C0 controls, tag characters,
  variation selectors) runs at the parser layer, observation-only. This
  layer detects and discloses — it does not block or sanitize; deciding
  what to do with flagged carriers is a downstream policy choice.

By the numbers: **142 file suffixes** across 5 domains · MIT · Python
3.10+ · zero network calls at parse time.

## CLI & REST API

The package installs two console entry points:

```bash
provendoc path/to/dataset --mode benchmark   # batch CLI
provendoc-serve                               # REST API server (pip install "provendoc[api]")
```

The CLI walks a directory through the per-domain pipelines and writes
a run directory under `runs/{run_id}/` — `manifest.json`, per-file
representation/evaluation artifacts, and an `reports/index.html`
overview. Exit code is `0` only when every file was processed;
honest-failure documents (format mismatch, missing backend) are
counted as processed and keep their facts in the artifacts. Runtime
modes (`--list-modes`) trade speed for diagnostics: `benchmark`,
`development`, `batch`, `analysis`, `debug`.

REST example (curl):

```bash
curl -s http://127.0.0.1:8000/health
curl -s -F file=@report.md http://127.0.0.1:8000/api/v1/chunk | jq '.metadata.chunk_count'
```

Endpoints: `/api/v1/parse`, `/api/v1/chunk`, `/api/v1/evaluate`,
`/api/v1/auto-pipeline`, `/api/v1/domains`, plus an async `/api/v1/batch`
job API. OpenAPI docs are served at `/docs`.

A commented configuration template ships as `provendoc.toml.example`.
The REST app also lives at `provendoc.api.app` (FastAPI factory) for
direct embedding with uvicorn. The REST service supports opt-in
`X-API-Key` authentication via `PROVENDOC_API_KEY`.

## Versioning & stability

Provendoc is 0.x: the public SDK surface (`parse` / `chunk` /
`evaluate` / `auto_pipeline`, the export functions, and the REST
endpoints under `/api/v1/`) may still evolve between minor versions.
Versioned contracts carry breaking changes as a **new versioned key**
instead of in-place mutation; unversioned surface (configuration field
names, report layouts, internal keys) may change between minor
versions. Pin an exact version in production and review
`CHANGELOG.md` when upgrading.

## About this release

This is the initial **code-only** release (v0.1.0): the parser core,
the five-domain representation layer, the SDK, CLI, and REST app.
The test suite, the benchmark corpora, the preregistered evaluation
kit, and the design-spec documents referenced by some docstrings
(`specs/*.md`) are maintained in the development tree and are planned
for later releases. Accordingly, a few comments and tool-configuration
sections in this repository (e.g. in `pyproject.toml`) still mention
paths such as `tests/`, `tools/`, or `ci.yml` — those refer to the
development tree and are inert in this snapshot.

Parsing untrusted documents is itself an attack surface: see the
invisible-carrier census above and the honest-failure contract.

## License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

The core package ships under MIT with permissive dependencies only.
Restrictively-licensed engines are opt-in extras and are **not**
installed by default:

- `provendoc[pdf]` pulls in **PyMuPDF** (AGPL-3.0, or a commercial
  license from Artifex). Commercial users must either comply with AGPL
  or obtain an Artifex license. Without it, the PDF path degrades
  honestly (auditable fallback, never a crash).
- `provendoc[pdf-layout]` pulls in **MinerU** (MinerU Open Source
  License — an Apache-2.0 base with additional terms; read them before
  commercial use).
- The spreadsheet extras add **pyxlsb** (LGPLv3+) and **odfpy**
  (GPL-2.0+ / Apache-2.0 dual-licensed; consumed under Apache-2.0).

The MIT grant covers Provendoc's own code only; it does not extend to
optional backends you install yourself.

## Name

"Provendoc" (provenance + document) is an independent open-source
project. It is **not affiliated with, endorsed by, or connected to**
`provendoc.com` or any other entity that may use a similar name; any
resemblance is coincidental.
