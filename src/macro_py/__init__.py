"""
Macro-py: A Python macro automation tool with keyboard input and GUI support
"""

from .MacroRecorder import MacroRecorder
from .MacroPlayer import MacroPlayer
from .MacroApp import MacroApp
from .MacroGUI import MacroGUI

__version__ = "0.1.0"
__all__ = ["MacroRecorder", "MacroPlayer", "MacroApp", "MacroGUI"]
