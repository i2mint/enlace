# enlace.util

Internal helpers for enlace.

### Functions

| [`derive_display_name`](#enlace.util.derive_display_name)(dir_name)   | Convert a directory name to a human-readable display name.          |
|----------------------------------------------------------------------------------|---------------------------------------------------------------------|
| [`derive_route_prefix`](#enlace.util.derive_route_prefix)(dir_name)   | Derive an API route prefix from a directory name.                   |
| [`is_skippable`](#enlace.util.is_skippable)(name)              | Return True if a directory name should be skipped during discovery. |

### enlace.util.derive_display_name(dir_name)

Convert a directory name to a human-readable display name.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> derive_display_name("chord_analyzer")
'Chord Analyzer'
>>> derive_display_name("todo")
'Todo'
```

### enlace.util.derive_route_prefix(dir_name)

Derive an API route prefix from a directory name.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> derive_route_prefix("chord_analyzer")
'/api/chord_analyzer'
>>> derive_route_prefix("todo")
'/api/todo'
```

### enlace.util.is_skippable(name)

Return True if a directory name should be skipped during discovery.

Directories starting with ‘_’ or ‘.’ are skipped.

* **Return type:**
  [`bool`](https://docs.python.org/3/builtins/functions.html#bool)

```pycon
>>> is_skippable("_internal")
True
>>> is_skippable(".git")
True
>>> is_skippable("my_app")
False
```
