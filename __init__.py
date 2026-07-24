"""Top-level package for comfyui_dataloader."""

from .src.nodes import NODE_CLASS_MAPPINGS
from .src.nodes import NODE_DISPLAY_NAME_MAPPINGS

# Web extension (node UI: placeholder, monospace editor, result panel).
WEB_DIRECTORY = "./js"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]

__author__ = """Boris Zyrianov"""
__email__ = "612boris40@gmail.com"
__version__ = "1.0.0"
