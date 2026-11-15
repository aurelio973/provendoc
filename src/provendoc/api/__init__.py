"""Provendoc REST API -- HTTP wrapper over the SDK.

Design rules:
    - The API is a faithful mapping of the four SDK interfaces
      (parse / chunk / evaluate / auto_pipeline); no second set of
      semantics. The SDK returns IngestionResult; the API returns its
      safe_serialize JSON form.
    - Fact boundary: text fields (text / original_text) can be huge;
      the ``include_text`` query parameter controls whether they ride
      with the response; when off, the response still carries the full
      metadata / units / benchmark structure.
    - Domain routing follows domain_registry as the single source of
      truth; the API keeps no suffix table of its own.

Endpoints (all JSON responses):

    GET  /health                    liveness + version + domain overview

    POST /api/v1/parse              multipart file -> parse only
    POST /api/v1/chunk              -> default semantic chunking
    POST /api/v1/evaluate           -> user-parameter chunking + quality baseline
    POST /api/v1/auto-pipeline      -> evaluation + auto-optimization
    GET  /api/v1/domains            domain registry (six-domain taxonomy)

Shared query parameters:
    include_text (bool, default true)   whether document text fields are returned

evaluate / auto-pipeline body (optional JSON):
    {"params": {...}}               semantic-first parameters (size_soft_limit etc.);
                                    legacy chunk_size/overlap also accepted
    {"stability_sweep": true}       stability sweep (narrative)
    {"profile": true}               RuntimeProfiler

Startup:
    uvicorn provendoc.api.app:create_app --factory
    provendoc-serve --port 8000
"""

from provendoc.api.app import create_app

__all__ = ["create_app"]
