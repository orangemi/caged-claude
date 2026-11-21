"""Host project identity, including ownership labels used by existing instances."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABEL = "io.caged-browser.managed"
LAYOUT_LABEL = "io.caged-browser.layout"
LAYOUT_VERSION = "package-v1"
YAML_READER = Path(__file__).with_name("load_config.rb")
