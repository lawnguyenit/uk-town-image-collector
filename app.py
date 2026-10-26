#!/usr/bin/env python3
"""Double-click desktop launcher for UK Town Image & Attribution Collector UI."""
import sys
from pathlib import Path

# Ensure src/ is on Python search path
src_dir = Path(__file__).resolve().parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from uk_town_image_collector.ui import launch_ui_app

if __name__ == "__main__":
    launch_ui_app()
