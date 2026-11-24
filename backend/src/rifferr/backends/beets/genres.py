"""``%genre_root{value}``: the top-level genre of ``value`` in beets lastgenre's bundled
genre tree (S-TAGS-CHOOSE TAGS-CHOOSE-9).

The tree is the ``genres-tree.yaml`` file beets ships in ``beetsplug/lastgenre``. It is
read as a file, without importing the plugin, which needs ``pylast`` (the optional
``lastgenre`` extra). Names are matched case-insensitively and returned title-cased, as
lastgenre writes them by default (``title_case: yes``); a value not in the tree comes back
unchanged.

beets dependency pinned by tests/backends/beets/test_genre_root.py: the file's location and
its shape (nested mappings and lists of genre names, most general first).
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

TREE_FILE = "genres-tree.yaml"


def tree_path() -> Path | None:
    spec = importlib.util.find_spec("beetsplug.lastgenre")
    if spec is None or not spec.submodule_search_locations:
        return None
    for location in spec.submodule_search_locations:
        candidate = Path(location) / TREE_FILE
        if candidate.is_file():
            return candidate
    return None


def _flatten(elem: Any, path: tuple[str, ...], out: dict[str, str]) -> None:
    if isinstance(elem, dict):
        for key, value in elem.items():  # pyright: ignore[reportUnknownVariableType]
            name = str(key).lower()  # pyright: ignore[reportUnknownArgumentType]
            out.setdefault(name, (path or (name,))[0])
            _flatten(value, (*path, name) if path else (name,), out)
    elif isinstance(elem, list):
        for sub in elem:  # pyright: ignore[reportUnknownVariableType]
            _flatten(sub, path, out)
    elif elem is not None:
        name = str(elem).lower()
        out.setdefault(name, path[0] if path else name)


@functools.cache
def roots() -> dict[str, str]:
    """Every genre in the tree (lowercase) -> its top-level genre (lowercase)."""
    path = tree_path()
    if path is None:
        return {}
    data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    _flatten(data, (), out)
    return out


def genre_root(value: str) -> str:
    root = roots().get(value.strip().lower())
    return value if root is None else root.title()
