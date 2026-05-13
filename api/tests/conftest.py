"""Make /app and /app/.. importable in CI containers and locally.

The Dockerfile sets PYTHONPATH=/app, but running `pytest` from the api/
directory needs both `src/` (this service) and the sibling `shared/` (one
directory up) reachable.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_API_ROOT = os.path.dirname(_HERE)              # .../render-service/api
_PROJECT_ROOT = os.path.dirname(_API_ROOT)      # .../render-service

for p in (_API_ROOT, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("RENDER_API_KEY", "test-key-test-key-test-key")
