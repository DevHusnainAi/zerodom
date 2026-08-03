"""ZeroDOM: DOM-to-Interaction-Graph middleware for AI web agents."""

from .label_linker import get_label
from .parser import InteractionGraph, ZeroDOMParser, parse_html
from .playwright_wrapper import ZeroDOM

__version__ = "0.0.1"
__all__ = ["ZeroDOM", "ZeroDOMParser", "InteractionGraph", "parse_html", "get_label"]
