"""Vercel serverless entry point.

Vercel loads this file as the Python function and serves the ASGI ``app`` it
exports. The project root is added to ``sys.path`` so the ``triageops``
package next to this folder can be imported.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from triageops.app import app  # noqa: E402

__all__ = ["app"]
