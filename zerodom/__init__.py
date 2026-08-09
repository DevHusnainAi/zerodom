"""ZeroDOM: DOM-to-Interaction-Graph middleware for AI web agents."""

from importlib.metadata import version

from .label_linker import get_label
from .parser import InteractionGraph, ZeroDOMParser, parse_html
from .playwright_wrapper import ZeroDOM

# pyproject.toml is the single source of truth; three hardcoded copies drift.
__version__ = version("zerodom")
__all__ = ["ZeroDOM", "ZeroDOMParser", "InteractionGraph", "parse_html", "get_label"]
