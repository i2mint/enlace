"""Internal helpers for enlace."""

from urllib.parse import quote


def url_path(path: str) -> str:
    """Percent-encode an app route or name for use in a URL enlace emits.

    App names come from directory names, which may hold a space, ``%``, ``#``,
    ``?`` or non-ASCII letters. Mounting works on the decoded path either way, but
    a ``Location``, an ``href`` or a launcher link must be a valid URL (#75). The
    names apps actually use (letters, digits, ``_ - . ~``) are returned unchanged.

    >>> url_path("/chord_analyzer/")
    '/chord_analyzer/'
    >>> url_path("/my app/")
    '/my%20app/'
    >>> url_path("/100%/")
    '/100%25/'
    >>> url_path("/café/")
    '/caf%C3%A9/'
    """
    return quote(path, safe="/")


def derive_display_name(dir_name: str) -> str:
    """Convert a directory name to a human-readable display name.

    >>> derive_display_name("chord_analyzer")
    'Chord Analyzer'
    >>> derive_display_name("todo")
    'Todo'
    """
    return dir_name.replace("_", " ").title()


def derive_route_prefix(dir_name: str) -> str:
    """Derive an API route prefix from a directory name.

    >>> derive_route_prefix("chord_analyzer")
    '/api/chord_analyzer'
    >>> derive_route_prefix("todo")
    '/api/todo'
    """
    return f"/api/{dir_name}"


def is_skippable(name: str) -> bool:
    """Return True if a directory name should be skipped during discovery.

    Directories starting with '_' or '.' are skipped.

    >>> is_skippable("_internal")
    True
    >>> is_skippable(".git")
    True
    >>> is_skippable("my_app")
    False
    """
    return name.startswith(("_", "."))
