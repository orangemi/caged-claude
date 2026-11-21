"""Standalone fixture programs copied into isolated test containers."""
from pathlib import Path


def script(name):
    return Path(__file__).with_name(name).read_text()
