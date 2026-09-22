# enlace.diagnose

Diagnose app compatibility with enlace.

Scans an app directory for patterns that would prevent or complicate mounting
under the enlace multi-app platform. Reports issues at three severity levels
(CRITICAL, MEDIUM, LOW) with specific file locations, explanations, and
suggested fixes.

Usage:

```default
from enlace.diagnose import diagnose_app
report = diagnose_app("/path/to/my_app")
print(report)
```

Or from the CLI:

```default
enlace diagnose /path/to/my_app
enlace diagnose /path/to/my_app --json
```

### Functions

| [`diagnose_app`](#enlace.diagnose.diagnose_app)(app_dir, \*[, app_name])   | Diagnose an app directory for enlace compatibility.               |
|------------------------------------------------------------------------------------------|-------------------------------------------------------------------|
| [`iter_diagnosers`](#enlace.diagnose.iter_diagnosers)()                       | All registered plugin diagnosers (after a lazy entry-point scan). |
| [`register_diagnoser`](#enlace.diagnose.register_diagnoser)(fn)                  | Register a plugin diagnoser `(app_dir, report) -> None`.          |

### Classes

| [`Category`](#enlace.diagnose.Category)(\*values)                         | Issue categories.                                    |
|---------------------------------------------------------------------------------------------|------------------------------------------------------|
| [`DiagnosticReport`](#enlace.diagnose.DiagnosticReport)(app_dir, app_name[, ...]) | Full diagnostic report for an app directory.         |
| [`Issue`](#enlace.diagnose.Issue)(severity, category, summary[, ...])  | A single compatibility issue found during diagnosis. |
| [`Severity`](#enlace.diagnose.Severity)(\*values)                         | Issue severity levels.                               |

### *class* enlace.diagnose.Category(\*values)

Bases: [`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Enum`](https://docs.python.org/3/library/enum.html#enum.Enum)

Issue categories.

### *class* enlace.diagnose.DiagnosticReport(app_dir, app_name, issues=<factory>, has_backend=False, has_frontend=False, backend_framework='', frontend_framework='', entry_point=None)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

Full diagnostic report for an app directory.

#### format_text()

Human-readable formatted report.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

#### *property* is_enlaceable *: [bool](https://docs.python.org/3/builtins/functions.html#bool)*

True if no CRITICAL issues were found.

### *class* enlace.diagnose.Issue(severity, category, summary, file_path=None, line_number=None, detail='', suggestion='', breaks_standalone=False)

Bases: [`object`](https://docs.python.org/3/builtins/functions.html#object)

A single compatibility issue found during diagnosis.

`category` is a `Category` for built-in checks, but plugin diagnosers
(registered via the `enlace.diagnosers` entry-point group) may pass a
plain string category — the report renders both.

### *class* enlace.diagnose.Severity(\*values)

Bases: [`str`](https://docs.python.org/3/builtins/stdtypes.html#str), [`Enum`](https://docs.python.org/3/library/enum.html#enum.Enum)

Issue severity levels.

### enlace.diagnose.diagnose_app(app_dir, , app_name='')

Diagnose an app directory for enlace compatibility.

Scans for hardcoded URLs, CORS middleware, SSR requirements, missing
entry points, and other patterns that prevent or complicate mounting
under the enlace platform. Plugin diagnosers registered via the
`enlace.diagnosers` entry-point group run after the built-in checks.

* **Parameters:**
  * **app_dir** – Path to the app directory to diagnose.
  * **app_name** ([`str`](https://docs.python.org/3/builtins/stdtypes.html#str)) – Override app name (defaults to directory name).
* **Return type:**
  [`DiagnosticReport`](#enlace.diagnose.DiagnosticReport)
* **Returns:**
  DiagnosticReport with all findings.

### enlace.diagnose.iter_diagnosers()

All registered plugin diagnosers (after a lazy entry-point scan).

* **Return type:**
  [`list`](https://docs.python.org/3/builtins/stdtypes.html#list)[[`Callable`](https://docs.python.org/3/library/typing.html#typing.Callable)[[[`Path`](https://docs.python.org/3/library/pathlib.html#pathlib.Path), [`DiagnosticReport`](#enlace.diagnose.DiagnosticReport)], [`None`](https://docs.python.org/3/builtins/constants.html#None)]]

### enlace.diagnose.register_diagnoser(fn)

Register a plugin diagnoser `(app_dir, report) -> None`.

Idempotent: registering the same callable twice is a no-op.

* **Return type:**
  [`None`](https://docs.python.org/3/builtins/constants.html#None)
