"""Provendoc — development CLI shim.

The package now lives under ``src/provendoc`` (install with
``pip install -e .``).  This shim keeps ``python app.py ...`` working from
the repository root without installation.  For the installed console entry
point, use ``provendoc`` (provendoc.app:main).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from provendoc.app import main

if __name__ == "__main__":
    main()
