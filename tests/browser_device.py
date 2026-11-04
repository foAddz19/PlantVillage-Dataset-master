"""Compatibility entry point for the device UI browser regression.

The current rice interface requires separate capture and analysis actions.
The suite uses temporary data, real rice inference and simulated USB/camera.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.browser_ui import main
if __name__=='__main__':main()
