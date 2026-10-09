"""
_toolpath.py - puts scripts/lib on sys.path so every module imports its
siblings by plain name (the engine modules were written that way).
Import-time only, no other effect.
"""
from __future__ import annotations

import os
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(SCRIPTS, "lib")
for _d in (LIB, SCRIPTS):
    if _d not in sys.path:
        sys.path.insert(1, _d)
