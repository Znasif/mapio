"""Where the code lives, and where the user's data lives.

Two roots, because a packaged install has to separate what ships from what the
user owns:

  APP_DIR     mapio.py, src/ and res/. Read-only in a package, replaced wholesale
              on update. Resolved from this file, so it does not care about cwd.

  MAPIO_HOME  models/, .env, the embedding cache and chat logs. Survives updates.

MAPIO_HOME defaults to APP_DIR, which is exactly what a git checkout has always
been -- so leaving it unset resolves every path where it resolved before, and
the Windows client keeps working untouched. A package sets it to a real data
directory instead.
"""

import os
from typing import List, Optional

APP_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
"""Root of the application: the directory holding mapio.py, src/ and res/."""

# Where the embedding cache lived before the split. Read-only fallback, so a
# map already indexed by the benchmark is not re-embedded through l1 just
# because the default moved.
LEGACY_CACHE_DIR = os.path.join(APP_DIR, "benchmark", ".cache")


def home() -> str:
    """The user data directory. $MAPIO_HOME, or APP_DIR when unset."""
    return os.path.abspath(os.environ.get("MAPIO_HOME") or APP_DIR)


def data(*parts: str) -> str:
    """A path inside the user data directory."""
    return os.path.join(home(), *parts)


def resource(*parts: str) -> str:
    """A path inside the shipped res/ directory."""
    return os.path.join(APP_DIR, "res", *parts)


def env_file() -> str:
    return data(".env")


def cache_dir() -> str:
    return os.environ.get("MAPIO_CACHE_DIR") or data("cache")


def models_dir() -> str:
    return data("models")


def resolve_resource(path: str) -> str:
    """A user-supplied path to a shipped file, e.g. --prompt res/prompt_en.yaml.

    Tried as given first, so a relative path keeps meaning what it meant from
    the repo root. Falling back to APP_DIR is what lets the same command line
    work from any working directory once the app is installed elsewhere.
    """
    if os.path.exists(path) or os.path.isabs(path):
        return path

    candidate = os.path.join(APP_DIR, path)
    return candidate if os.path.exists(candidate) else path


def available_maps() -> List[str]:
    """Map names under $MAPIO_HOME/models, for error messages."""
    root = models_dir()
    if not os.path.isdir(root):
        return []

    names = []
    for entry in sorted(os.listdir(root)):
        if os.path.isdir(os.path.join(root, entry)) and _map_json(
            os.path.join(root, entry)
        ):
            names.append(entry)

    return names


def _map_json(directory: str) -> Optional[str]:
    """The map JSON inside a map directory: <dir>/<dir>.json, else the only .json."""
    named = os.path.join(directory, f"{os.path.basename(directory)}.json")
    if os.path.isfile(named):
        return named

    jsons = [f for f in sorted(os.listdir(directory)) if f.endswith(".json")]
    return os.path.join(directory, jsons[0]) if len(jsons) == 1 else None


def resolve_map(spec: str) -> Optional[str]:
    """--model as a path or as a bare map name. None when nothing matches.

    A path is honoured exactly as before -- absolute, or relative to cwd -- so
    `--model models/new_york/new_york.json` from the repo root is unchanged.
    A bare name like `--model new_york` is looked up under $MAPIO_HOME/models,
    which is the form that works once the maps live outside the app directory.
    """
    if os.path.isfile(spec):
        return spec

    if os.path.isdir(spec):
        return _map_json(spec)

    for candidate in (
        os.path.join(models_dir(), spec),
        os.path.join(models_dir(), os.path.splitext(spec)[0]),
    ):
        if os.path.isdir(candidate):
            found = _map_json(candidate)
            if found:
                return found

    direct = os.path.join(models_dir(), spec)
    return direct if os.path.isfile(direct) else None
