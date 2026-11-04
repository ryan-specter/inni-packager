"""Entry point for the PyInstaller bundle."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from mirror_app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
