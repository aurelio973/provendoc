from provendoc.base.exporter import BaseExporter
from provendoc.core.exporters.atomic_write import atomic_write_json
from provendoc.core.foundation.serialization import safe_serialize


class JsonExporter(BaseExporter):
    """Generic JSON exporter — safe-serialize any data and write it
    atomically to disk."""

    def export(
            self,
            data,
            path
    ):
        """Serialize data to indented JSON and atomically write it to
        path.

        Args:
            data: any serializable object (normalized via
                safe_serialize).
            path: output file path (atomic temp + os.replace write).
        """
        serialized = safe_serialize(data)
        atomic_write_json(path, serialized, indent=2)
