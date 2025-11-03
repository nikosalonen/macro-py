#!/usr/bin/env python3
"""
Main entry point for macro-py package.
Provides both GUI and CLI interfaces.
"""

import sys
import argparse
from PyQt5.QtWidgets import QApplication
from .MacroGUI import MacroGUI
from .MacroApp import MacroApp


def main():
    parser = argparse.ArgumentParser(description="Macro Recorder and Player")
    parser.add_argument(
        "--cli", action="store_true", help="Run in CLI mode (default is GUI mode)"
    )
    parser.add_argument("--version", action="version", version="macro-py 0.1.0")

    args = parser.parse_args()

    try:
        if args.cli:
            print("Starting macro-py in CLI mode...")
            app = MacroApp()
            app.run()
        else:
            print("Starting macro-py in GUI mode...")
            qt_app = QApplication(sys.argv)
            gui = MacroGUI()
            gui.run()
            sys.exit(qt_app.exec_())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
