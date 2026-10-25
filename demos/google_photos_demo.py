#!/usr/bin/env python3
"""Run the standalone demo; shared Google Photos code lives in visualizer/photos/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from visualizer.photos.demo import main

if __name__ == "__main__":
    main()
