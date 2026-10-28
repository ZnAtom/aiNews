#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skills 每日采集入口（systemd radar-skills-sync.timer 06:30 调用）。

用法：uv run python app/radar/skills_sync.py [--force]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skills import sync_all  # noqa: E402

if __name__ == "__main__":
    result = sync_all()
    print(f"[skills-sync] done: {result}")
