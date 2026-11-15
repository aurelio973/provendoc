"""Independent image-asset storage export.

Directory contract (run_dir/images/{base}/):
- original/{asset_id}.{format} — bit-exact original bytes, never modified;
- derived/{asset_id}_derived.png — OpenCV pipeline output; the manifest records
  the pipeline name/parameter fingerprint/version so it can be reproduced from original;
  gracefully skipped and recorded honestly when OpenCV is missing;
- manifest.json — the asset_id (first 12 hex chars of sha256) joins both sides:
  the body placeholder block [Image: asset_id ...] and the binary store meet via the
  same id, carrying page/bbox/paragraph/context/caption evidence.

Split out of provendoc_run.py (structural convergence). File writes are submitted
via submit_export (ProvendocRun's async export pool, or synchronous execution),
sharing the same I/O channel as other artifacts.
"""

from pathlib import Path

from provendoc.runtime.run_artifacts import write_json_sync


def write_image_files_sync(files: list):
    """Write (path, bytes) pairs — runs on the async export pool."""
    for path_str, data in files:
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class ImageAssetExporter:
    """Image-asset persister: original/ raw bytes + derived/ + manifest.json.

    submit_export: a callable (fn, *args) provided by ProvendocRun —
    submitted to the thread pool when async export is on, run synchronously when off.
    """

    def __init__(self, run_dir: Path, submit_export):
        """Inject the run directory and the export submitter."""
        self.run_dir = run_dir
        self._submit_export = submit_export

    def export(self, result, relative_path: Path, payloads: dict) -> dict:
        """Export a single file's image assets; returns a run-level manifest digest."""
        from provendoc.core.image_assets import processing
        from provendoc.core.image_assets.schema import (
            MANIFEST_VERSION,
        )

        metadata = result.document.metadata or {}
        facts_block = (
            metadata.get("pdf_image_assets")
            or metadata.get("docx_image_assets")
            or metadata.get("pptx_image_assets")
            or {}
        )
        facts = facts_block.get("assets", [])

        base_name = str(relative_path.with_suffix("")).replace("\\", "_").replace("/", "_")
        asset_dir = self.run_dir / "images" / base_name
        original_dir = asset_dir / "original"
        derived_dir = asset_dir / "derived"
        # Directories must exist before the async writes are submitted:
        # ``write_json_sync`` does not create parents, and the manifest
        # write could otherwise race ahead of the original-file write that
        # would have created them.
        original_dir.mkdir(parents=True, exist_ok=True)
        derived_dir.mkdir(parents=True, exist_ok=True)

        # format per asset from the extraction facts
        formats = {
            f.get("asset_id"): f.get("format", "unknown")
            for f in facts
        }

        # ── original layer: bit-exact ──
        original_files = [
            (str(original_dir / f"{asset_id}.{formats.get(asset_id, 'bin')}"), data)
            for asset_id, data in payloads.items()
        ]

        # ── derived layer: OpenCV, optional ──
        derived_section: dict = {
            "available": False,
            "reason": "opencv_not_available",
            "files": {},
        }
        derived_files = []
        if processing.opencv_available():
            try:
                derived_result = processing.build_derived(payloads)
                derived_section = {
                    "available": True,
                    "pipeline": processing.DEFAULT_PIPELINE,
                    "pipeline_version": processing.PIPELINE_VERSION,
                    "files": {},
                }
                for asset_id, info in derived_result.items():
                    for step, rendition in (
                        info.get("renditions") or {}
                    ).items():
                        derived_files.append(
                            (str(derived_dir / rendition["filename"]), rendition["bytes"])
                        )
                    entry: dict = {
                        "renditions": sorted(
                            (info.get("renditions") or {}).keys()
                        ),
                        "applied": info.get("applied", []),
                    }
                    if info.get("error"):
                        entry["error"] = info["error"]
                    derived_section["files"][asset_id] = entry
            except Exception as error:
                derived_section = {
                    "available": False,
                    "reason": f"derived_pipeline_failed: {error}",
                    "files": {},
                }

        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "source": {
                "file": str(relative_path),
                "file_type": relative_path.suffix.lower(),
                "extractor": facts_block.get("extractor", "unknown"),
            },
            "counts": facts_block.get("counts", {}),
            "assets": facts,
            "derived": derived_section,
        }

        self._submit_export(
            write_image_files_sync,
            original_files + derived_files,
        )
        self._submit_export(
            write_json_sync,
            manifest,
            str(asset_dir / "manifest.json"),
        )

        return {
            "directory": str(asset_dir),
            "unique_assets": len(payloads),
            "original_files": len(original_files),
            "derived_files": len(derived_files),
            "derived_available": derived_section.get("available", False),
        }
