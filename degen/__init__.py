"""DEGEN — AI degen trader (personal use).

Self-contained package. Imports backend plumbing (pumpfun_client, candle_aggregator,
…) by putting backend/ on sys.path, so every module here can `import pumpfun_client`
directly whether it runs under backend/main.py or standalone.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

__version__ = "0.1.0"
