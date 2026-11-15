"""Load a script from a skill folder (the folder names have hyphens)."""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")


def load(folder, name):
    """Import <folder>/<name>.py. The folder goes on sys.path first so the
    script's `import nse_common` finds the copy that sits next to it."""
    folder_path = os.path.join(ROOT, folder)
    if folder_path not in sys.path:
        sys.path.insert(0, folder_path)
    path = os.path.join(folder_path, name + ".py")
    spec = importlib.util.spec_from_file_location("dti_%s_%s" % (folder.replace("-", "_"), name), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
