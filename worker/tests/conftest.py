"""Make /app and /app/.. importable in CI containers and locally.

The Dockerfiles set PYTHONPATH=/app, but running `pytest` from the worker/
directory needs both `src/` (this service) and the sibling `shared/` (one
directory up) reachable.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER_ROOT = os.path.dirname(_HERE)              # .../render-service/worker
_PROJECT_ROOT = os.path.dirname(_WORKER_ROOT)      # .../render-service

for p in (_WORKER_ROOT, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
