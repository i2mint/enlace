# enlace.util

Internal helpers for enlace.

### Functions

| [`derive_display_name`](#enlace.util.derive_display_name)(dir_name)   | Convert a directory name to a human-readable display name.          |
|----------------------------------------------------------------------------------|---------------------------------------------------------------------|
| [`derive_route_prefix`](#enlace.util.derive_route_prefix)(dir_name)   | Derive an API route prefix from a directory name.                   |
| [`is_skippable`](#enlace.util.is_skippable)(name)              | Return True if a directory name should be skipped during discovery. |
| [`url_path`](#enlace.util.url_path)(path)                  | Percent-encode an app route or name for use in a URL enlace emits.  |

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

### enlace.util.url_path(path)

Percent-encode an app route or name for use in a URL enlace emits.

App names come from directory names, which may hold a space, `%`, `#`,
`?` or non-ASCII letters. Mounting works on the decoded path either way, but
a `Location`, an `href` or a launcher link must be a valid URL (#75). The
names apps actually use (letters, digits, `_ - . ~`) are returned unchanged.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> url_path("/chord_analyzer/")
'/chord_analyzer/'
>>> url_path("/my app/")
'/my%20app/'
>>> url_path("/100%/")
'/100%25/'
>>> url_path("/café/")
'/caf%C3%A9/'
```
