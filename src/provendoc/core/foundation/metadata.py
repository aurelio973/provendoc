import mimetypes
import os
from datetime import datetime


def _build_metadata(file_path):
    """Build file metadata.

    :param file_path:
    :return:
    """
    size = os.path.getsize(file_path)
    created_time = os.path.getctime(file_path)
    modified_time = os.path.getmtime(file_path)
    mime_type,_ = mimetypes.guess_type(file_path)

    return {
        "file_name": os.path.basename(file_path),
        "file_path": os.path.abspath(file_path),
        "file_size": size,
        "file_size_kb": round(size / 1024,2),
        "created_time": datetime.fromtimestamp(created_time).isoformat(),
        "modified_time": datetime.fromtimestamp(modified_time).isoformat(),
        "mime_type": mime_type or "unknown"
    }
