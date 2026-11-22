"""Host project identity, including ownership labels used by existing instances."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "io.caged-browser.managed"
LAYOUT_LABEL = "io.caged-browser.layout"
LAYOUT_VERSION = "package-v1"
# Set by the launcher image: host loopback ports are not reachable from here.
IN_LAUNCHER = os.environ.get("CAGED_LAUNCHER") == "1"
