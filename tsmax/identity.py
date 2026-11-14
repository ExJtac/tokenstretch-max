"""The only place the product's display and runtime names are defined.

Keep this file in sync with pyproject.toml (dist name, console script, package).
"""
from __future__ import annotations

from pathlib import Path

# Shown in --version, reports, and user-facing text.
PRODUCT_NAME = "tokenstretch MAX"
VERSION = "0.1.0.dev0"

# argv0 / console-script name (pyproject [project.scripts]).
CLI_NAME = "tsmax"

# Python import package directory name (pyproject packages.find).
PACKAGE_NAME = "tsmax"

# Packaging / dist name (pyproject [project].name).
DIST_NAME = "tsmax"

# Directory under the user's home for manifests, backups, hooks, and pins.
# Default: ~/.tsmax
STATE_DIRNAME = ".tsmax"

# Env prefix for kit-owned variables (e.g. TSMAX_CCUSAGE).
ENV_PREFIX = "TSMAX"


def state_dir(home: Path | str) -> Path:
    return Path(home) / STATE_DIRNAME
