"""Internal helpers for enlace."""


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
