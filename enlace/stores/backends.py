"""MutableMapping-backed store factories for enlace.

The file backend is the MVP default: one directory per named store under a
platform root (``~/.enlace/platform_store/`` by default). Values are JSON.

When ``dol`` is installed (via ``enlace[auth]``), ``make_file_store_factory``
uses ``dol.Files`` + a JSON codec. When it isn't, we fall back to a tiny
stdlib implementation so the core package keeps working.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Callable

StoreFactory = Callable[[str], MutableMapping]


class _FileDict(MutableMapping):
    """Minimal JSON-file-per-key MutableMapping. Used when dol isn't available."""

    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._root / key

    def __getitem__(self, key: str):
        p = self._path(key)
        if not p.exists():
            raise KeyError(key)
        with p.open("rb") as f:
            return json.loads(f.read())

    def __setitem__(self, key: str, value) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value).encode("utf-8")
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("wb") as f:
            f.write(data)
        os.replace(tmp, p)

    def __delitem__(self, key: str) -> None:
        p = self._path(key)
        if not p.exists():
            raise KeyError(key)
        p.unlink()

    def __iter__(self) -> Iterator[str]:
        for p in self._root.rglob("*"):
            if p.is_file() and not p.name.endswith(".tmp"):
                yield str(p.relative_to(self._root))

    def __len__(self) -> int:
        return sum(1 for _ in iter(self))

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._path(key).exists()


def _make_dol_factory(root: Path) -> StoreFactory:
    """Build a factory using dol.Files with a JSON codec.

    dol.Files uses absolute filesystem paths as keys but doesn't auto-create
    parent directories on write; we wrap setitem to ``mkdir -p`` the parent
    so per-user subpaths like ``alice/chord/settings`` work out of the box.
    """
    from dol import Files, wrap_kvs  # type: ignore

    class _MkdirFiles(Files):
        def __setitem__(self, k, v):
            Path(k).parent.mkdir(parents=True, exist_ok=True)
            super().__setitem__(k, v)

    def factory(name: str) -> MutableMapping:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        base = _MkdirFiles(str(d))

        def _postget(k, v):
            if isinstance(v, (bytes, bytearray)):
                v = v.decode("utf-8")
            return json.loads(v)

        def _preset(k, v):
            return json.dumps(v).encode("utf-8")

        return wrap_kvs(base, postget=_postget, preset=_preset)

    return factory


def make_file_store_factory(root: str, *, use_dol: bool = False) -> StoreFactory:
    """Return a ``StoreFactory`` backed by JSON files under ``root``.

    ``factory(name)`` returns a ``MutableMapping`` rooted at ``root/name/``.

    Defaults to a small stdlib implementation that auto-creates parent
    directories on write. Pass ``use_dol=True`` to use ``dol.Files`` instead
    (pulls in the soft dep and expects flat keys).
    """
    root_path = Path(os.path.expanduser(root))
    if use_dol:
        try:
            return _make_dol_factory(root_path)
        except ImportError:
            pass

    def factory(name: str) -> MutableMapping:
        return _FileDict(root_path / name)

    return factory
