"""Host project identity, including ownership labels used by existing instances."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "io.caged-claude.managed"
LAYOUT_LABEL = "io.caged-claude.layout"
LAYOUT_VERSION = "package-v1"
# Containers started before the caged-claude rename carry this label instead.
LEGACY_LABEL = "io.caged-browser.managed"
# Set by the launcher image: host loopback ports are not reachable from here.
IN_LAUNCHER = os.environ.get("CAGED_LAUNCHER") == "1"
