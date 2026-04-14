"""Diagnose app compatibility with enlace.

Scans an app directory for patterns that would prevent or complicate mounting
under the enlace multi-app platform. Reports issues at three severity levels
(CRITICAL, MEDIUM, LOW) with specific file locations, explanations, and
suggested fixes.

Usage::

    from enlace.diagnose import diagnose_app
    report = diagnose_app("/path/to/my_app")
    print(report)

Or from the CLI::

    enlace diagnose /path/to/my_app
    enlace diagnose /path/to/my_app --json
"""

import ast
import json as json_module
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """Issue severity levels."""
    CRITICAL = "CRITICAL"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Category(str, Enum):
    """Issue categories."""
    HARDCODED_URL = "hardcoded_url"
    CORS = "cors"
    FRONTEND_FRAMEWORK = "frontend_framework"
    PORT_HARDCODED = "port_hardcoded"
    MISSING_ENTRY_POINT = "missing_entry_point"
    MISSING_FRONTEND = "missing_frontend"
    BARE_IMPORTS = "bare_imports"
    DATA_PATH = "data_path"
    SUBAPP_CORS = "subapp_cors"
    BASE_HTTP_MIDDLEWARE = "base_http_middleware"
    BASE_PATH = "base_path"
    WEBSOCKET = "websocket"
    SERVER_SIDE_RENDERING = "ssr"
    ENV_CONFIG = "env_config"


@dataclass
class Issue:
    """A single compatibility issue found during diagnosis."""
    severity: Severity
    category: Category
    summary: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    detail: str = ""
    suggestion: str = ""
    # True when the suggested fix would break the app's ability to run standalone.
    # The diagnostic should try to avoid this — prefer suggestions that keep both
    # modes working (e.g. env-var with current value as default).
    breaks_standalone: bool = False

    def to_dict(self) -> dict:
        d = {
            "severity": self.severity.value,
            "category": self.category.value,
            "summary": self.summary,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "breaks_standalone": self.breaks_standalone,
        }
        if self.file_path:
            d["file"] = self.file_path
        if self.line_number is not None:
            d["line"] = self.line_number
        return d


@dataclass
class DiagnosticReport:
    """Full diagnostic report for an app directory."""
    app_dir: Path
    app_name: str
    issues: list[Issue] = field(default_factory=list)
    # Detected characteristics (informational)
    has_backend: bool = False
    has_frontend: bool = False
    backend_framework: str = ""
    frontend_framework: str = ""
    entry_point: Optional[str] = None

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.CRITICAL)

    @property
    def medium_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.MEDIUM)

    @property
    def low_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.LOW)

    @property
    def is_enlaceable(self) -> bool:
        """True if no CRITICAL issues were found."""
        return self.critical_count == 0

    def to_dict(self) -> dict:
        return {
            "app_dir": str(self.app_dir),
            "app_name": self.app_name,
            "enlaceable": self.is_enlaceable,
            "has_backend": self.has_backend,
            "has_frontend": self.has_frontend,
            "backend_framework": self.backend_framework,
            "frontend_framework": self.frontend_framework,
            "entry_point": self.entry_point,
            "summary": {
                "critical": self.critical_count,
                "medium": self.medium_count,
                "low": self.low_count,
            },
            "issues": [i.to_dict() for i in self.issues],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json_module.dumps(self.to_dict(), indent=indent)

    def format_text(self) -> str:
        """Human-readable formatted report."""
        lines: list[str] = []
        lines.append(f"Enlace Compatibility Report: {self.app_name}")
        lines.append("=" * (len(lines[0])))
        lines.append("")

        # App overview
        lines.append(f"Directory:  {self.app_dir}")
        lines.append(f"Backend:    {'yes' if self.has_backend else 'no'}"
                      + (f" ({self.backend_framework})" if self.backend_framework else ""))
        lines.append(f"Frontend:   {'yes' if self.has_frontend else 'no'}"
                      + (f" ({self.frontend_framework})" if self.frontend_framework else ""))
        if self.entry_point:
            lines.append(f"Entry:      {self.entry_point}")
        lines.append("")

        # Verdict
        if self.is_enlaceable:
            lines.append("Verdict: COMPATIBLE (no critical issues)")
        else:
            lines.append(f"Verdict: BLOCKED ({self.critical_count} critical issue(s))")
        lines.append("")

        # Issues by severity
        for sev in (Severity.CRITICAL, Severity.MEDIUM, Severity.LOW, Severity.INFO):
            sev_issues = [i for i in self.issues if i.severity == sev]
            if not sev_issues:
                continue
            lines.append(f"--- {sev.value} ({len(sev_issues)}) ---")
            lines.append("")
            for issue in sev_issues:
                loc = ""
                if issue.file_path:
                    loc = f"  [{issue.file_path}"
                    if issue.line_number is not None:
                        loc += f":{issue.line_number}"
                    loc += "]"
                lines.append(f"  [{issue.category.value}] {issue.summary}{loc}")
                if issue.detail:
                    for detail_line in issue.detail.splitlines():
                        lines.append(f"    {detail_line}")
                if issue.suggestion:
                    lines.append(f"    Fix: {issue.suggestion}")
                if issue.breaks_standalone:
                    lines.append(f"    WARNING: This fix would break standalone operation.")
                lines.append("")

        return "\n".join(lines)

    def __str__(self) -> str:
        return self.format_text()


# ---------------------------------------------------------------------------
# Checker functions — each inspects one aspect and appends to report.issues
# ---------------------------------------------------------------------------

def _check_entry_points(
    app_dir: Path,
    report: DiagnosticReport,
    *,
    entry_point_names: tuple[str, ...] = ("server.py", "app.py", "main.py"),
):
    """Check for conventional backend entry points."""
    for name in entry_point_names:
        candidate = app_dir / name
        if candidate.is_file():
            report.has_backend = True
            report.entry_point = name
            return

    # Check subdirectories (e.g., backend/main.py)
    for subdir_name in ("backend", "src", "api"):
        subdir = app_dir / subdir_name
        if not subdir.is_dir():
            continue
        for name in entry_point_names:
            candidate = subdir / name
            if candidate.is_file():
                report.has_backend = True
                report.entry_point = f"{subdir_name}/{name}"
                report.issues.append(Issue(
                    severity=Severity.MEDIUM,
                    category=Category.MISSING_ENTRY_POINT,
                    summary=f"Backend entry point is in subdirectory '{subdir_name}/'",
                    file_path=f"{subdir_name}/{name}",
                    detail=(
                        f"enlace looks for entry points ({', '.join(entry_point_names)}) "
                        f"in the app's root directory by convention. It found one in "
                        f"'{subdir_name}/' — enlace just needs a hint to find it."
                    ),
                    suggestion=(
                        f"Create an app.toml in the app root (no app code changes needed):\n"
                        f"  entry_point = \"{subdir_name}/{name}\""
                    ),
                ))
                return

    if not report.has_frontend:
        report.issues.append(Issue(
            severity=Severity.LOW,
            category=Category.MISSING_ENTRY_POINT,
            summary="No backend entry point found",
            detail=(
                f"Looked for {', '.join(entry_point_names)} in the app root "
                f"and in backend/, src/, api/ subdirectories."
            ),
            suggestion=(
                "Create a server.py with a FastAPI `app` object, or use app.toml "
                "to specify a custom entry_point."
            ),
        ))


def _check_frontend(app_dir: Path, report: DiagnosticReport):
    """Check for frontend directory and detect framework."""
    frontend_dir = app_dir / "frontend"
    # Also check common build output dirs
    for candidate_name in ("frontend", "dist", "out", "build", "public"):
        candidate = app_dir / candidate_name
        if candidate.is_dir() and (candidate / "index.html").exists():
            report.has_frontend = True
            report.frontend_framework = "static"
            return

    # Check for framework-specific configs at app level
    package_json = app_dir / "package.json"
    if not package_json.exists():
        # Check in frontend/ subdir
        for subdir in ("frontend", "client", "web", "app"):
            package_json = app_dir / subdir / "package.json"
            if package_json.exists():
                break
        else:
            package_json = None

    if package_json and package_json.exists():
        report.has_frontend = True
        try:
            pkg = json_module.loads(package_json.read_text())
        except Exception:
            return

        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        framework = _detect_js_framework(deps)
        if framework:
            report.frontend_framework = framework

    # Also check for next.config.* files
    for config_name in ("next.config.ts", "next.config.js", "next.config.mjs"):
        if (app_dir / config_name).exists() or any(
            (app_dir / d / config_name).exists()
            for d in ("frontend", "client", "web", "app")
        ):
            report.has_frontend = True
            report.frontend_framework = "next.js"
            break

    # Nuxt
    if (app_dir / "nuxt.config.ts").exists() or (app_dir / "nuxt.config.js").exists():
        report.has_frontend = True
        report.frontend_framework = "nuxt"

    # SvelteKit
    if (app_dir / "svelte.config.js").exists():
        report.has_frontend = True
        report.frontend_framework = "sveltekit"

    # Vite (generic)
    for d in [app_dir] + [app_dir / s for s in ("frontend", "client")]:
        if (d / "vite.config.ts").exists() or (d / "vite.config.js").exists():
            report.has_frontend = True
            if not report.frontend_framework:
                report.frontend_framework = "vite"
            break


def _detect_js_framework(deps: dict) -> str:
    """Detect frontend framework from package.json dependencies."""
    if "next" in deps:
        return "next.js"
    if "nuxt" in deps:
        return "nuxt"
    if "@sveltejs/kit" in deps:
        return "sveltekit"
    if "vite" in deps:
        return "vite"
    if "react" in deps:
        return "react"
    if "vue" in deps:
        return "vue"
    if "svelte" in deps:
        return "svelte"
    if "@angular/core" in deps:
        return "angular"
    return ""


def _check_ssr_framework(app_dir: Path, report: DiagnosticReport):
    """Check if frontend framework requires a server runtime (SSR).

    Frameworks like Next.js, Nuxt, and SvelteKit need their own Node.js
    process unless configured for static export.
    """
    fw = report.frontend_framework
    if fw not in ("next.js", "nuxt", "sveltekit"):
        return

    # Check if static export is configured
    static_export_configured = False

    if fw == "next.js":
        static_export_configured = _next_has_static_export(app_dir)
        if not static_export_configured:
            report.issues.append(Issue(
                severity=Severity.MEDIUM,
                category=Category.SERVER_SIDE_RENDERING,
                summary="Next.js frontend requires server runtime (not static-exportable as-is)",
                detail=(
                    "Next.js needs a Node.js process for SSR/ISR. enlace currently "
                    "serves frontends as static files via StaticFiles mount. "
                    "If the app is a pure client-side SPA (no getServerSideProps, "
                    "no API routes in app/api/), static export is likely viable."
                ),
                suggestion=(
                    "If the app is a pure client-side SPA, add to next.config.ts:\n"
                    "  output: 'export',\n"
                    "  basePath: process.env.NEXT_PUBLIC_BASE_PATH || '',\n"
                    "Then build for enlace: NEXT_PUBLIC_BASE_PATH=/{app_name} npm run build\n"
                    "The `out/` directory becomes the static frontend enlace serves.\n"
                    "Standalone (no env var): `npm run dev` works at / as before.\n"
                    "If SSR is truly needed, enlace would need a reverse proxy mount."
                ),
            ))

    elif fw == "nuxt":
        report.issues.append(Issue(
            severity=Severity.MEDIUM,
            category=Category.SERVER_SIDE_RENDERING,
            summary="Nuxt frontend may require server runtime",
            suggestion=(
                "Configure Nuxt for static generation: set `ssr: false` or "
                "`target: 'static'` in nuxt.config.ts, then `nuxt generate`."
            ),
        ))

    elif fw == "sveltekit":
        report.issues.append(Issue(
            severity=Severity.MEDIUM,
            category=Category.SERVER_SIDE_RENDERING,
            summary="SvelteKit frontend may require server runtime",
            suggestion=(
                "Use `@sveltejs/adapter-static` for static output. "
                "Set `export const prerender = true` in root layout."
            ),
        ))

    # Check for basePath / base configuration
    _check_base_path(app_dir, report)


def _next_has_static_export(app_dir: Path) -> bool:
    """Check if a Next.js app has output: 'export' configured."""
    for root in [app_dir] + [app_dir / d for d in ("frontend", "client", "web")]:
        for config_name in ("next.config.ts", "next.config.js", "next.config.mjs"):
            config_file = root / config_name
            if config_file.exists():
                try:
                    text = config_file.read_text()
                    if re.search(r"""output\s*:\s*['"]export['"]""", text):
                        return True
                except Exception:
                    pass
    return False


def _check_base_path(app_dir: Path, report: DiagnosticReport):
    """Check if SPA framework has basePath configured for subpath mounting."""
    fw = report.frontend_framework

    if fw == "next.js":
        has_base = False
        for root in [app_dir] + [app_dir / d for d in ("frontend", "client", "web")]:
            for config_name in ("next.config.ts", "next.config.js", "next.config.mjs"):
                config_file = root / config_name
                if config_file.exists():
                    try:
                        text = config_file.read_text()
                        if re.search(r"basePath\s*:", text):
                            has_base = True
                    except Exception:
                        pass
        if not has_base:
            report.issues.append(Issue(
                severity=Severity.LOW,
                category=Category.BASE_PATH,
                summary="No basePath configured in Next.js config",
                detail=(
                    "Under enlace, the frontend is served at /{app_name}/. "
                    "Without basePath, Next.js internal routing and asset loading "
                    "will use root-relative paths that won't resolve correctly. "
                    "However, adding a hardcoded basePath would break standalone "
                    "operation (where the app is served at /)."
                ),
                suggestion=(
                    "Make basePath configurable via environment variable:\n"
                    "  basePath: process.env.NEXT_PUBLIC_BASE_PATH || ''\n"
                    "Then build for enlace with:\n"
                    "  NEXT_PUBLIC_BASE_PATH=/{app_name} npm run build\n"
                    "Standalone (no env var): serves at / as before.\n"
                    "Note: a hardcoded basePath (without env var) would break standalone."
                ),
            ))

    elif fw in ("vite", "react"):
        # Check for vite base config
        for root in [app_dir] + [app_dir / d for d in ("frontend", "client")]:
            for config_name in ("vite.config.ts", "vite.config.js"):
                config_file = root / config_name
                if config_file.exists():
                    try:
                        text = config_file.read_text()
                        if not re.search(r"base\s*:", text):
                            report.issues.append(Issue(
                                severity=Severity.LOW,
                                category=Category.BASE_PATH,
                                summary="No `base` configured in Vite config",
                                suggestion=(
                                    "Make base configurable via environment variable:\n"
                                    "  base: process.env.VITE_BASE_PATH || '/'\n"
                                    "Build for enlace: VITE_BASE_PATH=/{app_name}/ npm run build\n"
                                    "Standalone: serves at / as before.\n"
                                    "Note: a hardcoded base (without env var) would break standalone."
                                ),
                            ))
                    except Exception:
                        pass
                    return


def _check_python_backend(app_dir: Path, report: DiagnosticReport):
    """Scan Python files for enlace compatibility issues."""
    py_dirs = [app_dir]
    for subdir_name in ("backend", "src", "api"):
        subdir = app_dir / subdir_name
        if subdir.is_dir():
            py_dirs.append(subdir)

    py_files: list[Path] = []
    for d in py_dirs:
        py_files.extend(d.glob("*.py"))

    if not py_files:
        return

    report.backend_framework = "python"

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel = _relative(py_file, app_dir)

        # Detect FastAPI
        if "FastAPI" in source or "fastapi" in source:
            report.backend_framework = "fastapi"

        # Detect Flask
        if "Flask(" in source:
            report.backend_framework = "flask"
            report.issues.append(Issue(
                severity=Severity.INFO,
                category=Category.MISSING_ENTRY_POINT,
                summary="Flask app detected — enlace supports ASGI apps",
                file_path=rel,
                detail=(
                    "Flask is a WSGI framework. enlace expects ASGI apps (FastAPI, "
                    "Starlette, etc.). Flask can work through asgiref's WsgiToAsgi "
                    "adapter, but native FastAPI is preferred."
                ),
                suggestion=(
                    "Port to FastAPI for best enlace integration, or wrap with "
                    "`from asgiref.wsgi import WsgiToAsgi; app = WsgiToAsgi(flask_app)`"
                ),
            ))

        _scan_python_for_cors(source, rel, report)
        _scan_python_for_base_http_middleware(source, rel, report)
        _scan_python_for_hardcoded_ports(source, rel, report)
        _scan_python_for_hardcoded_urls(source, rel, report)
        _scan_python_for_bare_imports(source, rel, py_file, app_dir, report)


def _scan_python_for_cors(source: str, rel: str, report: DiagnosticReport):
    """Detect CORS middleware in Python backend files."""
    for i, line in enumerate(source.splitlines(), 1):
        if "CORSMiddleware" in line and ("add_middleware" in source):
            report.issues.append(Issue(
                severity=Severity.MEDIUM,
                category=Category.SUBAPP_CORS,
                summary="Sub-app adds its own CORS middleware",
                file_path=rel,
                line_number=i,
                detail=(
                    "Under enlace, the parent app handles CORS for all sub-apps. "
                    "A sub-app also adding CORSMiddleware can cause double "
                    "Access-Control-* headers. enlace will still mount the app, "
                    "but the duplicate headers may confuse some browsers."
                ),
                suggestion=(
                    "Make CORS conditional so the app works both standalone and "
                    "under enlace. For example:\n"
                    "  import os\n"
                    "  if not os.environ.get('ENLACE_MANAGED'):\n"
                    "      app.add_middleware(CORSMiddleware, ...)\n"
                    "This keeps standalone operation intact while avoiding double "
                    "headers under enlace."
                ),
            ))
            return  # One finding per file


def _scan_python_for_base_http_middleware(
    source: str, rel: str, report: DiagnosticReport
):
    """Detect BaseHTTPMiddleware usage (known Starlette bugs)."""
    for i, line in enumerate(source.splitlines(), 1):
        if "BaseHTTPMiddleware" in line:
            report.issues.append(Issue(
                severity=Severity.MEDIUM,
                category=Category.BASE_HTTP_MIDDLEWARE,
                summary="Uses BaseHTTPMiddleware (known Starlette bugs)",
                file_path=rel,
                line_number=i,
                detail=(
                    "BaseHTTPMiddleware has unfixable bugs: exception swallowing, "
                    "ContextVar corruption, synchronous background task execution. "
                    "The Starlette maintainer has called it unfixable."
                ),
                suggestion=(
                    "Replace with pure ASGI middleware: a class with "
                    "__init__(self, app) and __call__(self, scope, receive, send)."
                ),
            ))
            return


def _scan_python_for_hardcoded_ports(
    source: str, rel: str, report: DiagnosticReport
):
    """Detect hardcoded port numbers in uvicorn.run() calls."""
    for i, line in enumerate(source.splitlines(), 1):
        if "uvicorn.run" in line or "uvicorn.run" in source[max(0, source.find(line)-200):]:
            if re.search(r'port\s*=\s*\d+', line):
                report.issues.append(Issue(
                    severity=Severity.LOW,
                    category=Category.PORT_HARDCODED,
                    summary="Hardcoded port in uvicorn.run()",
                    file_path=rel,
                    line_number=i,
                    detail=(
                        "Under enlace, this __main__ block is never executed "
                        "(enlace imports the app object directly). No fix needed, "
                        "but be aware this block is dead code under enlace."
                    ),
                    suggestion="No action required — dead code under enlace.",
                ))
                return


def _scan_python_for_hardcoded_urls(
    source: str, rel: str, report: DiagnosticReport
):
    """Detect hardcoded localhost URLs in Python code."""
    url_pattern = re.compile(
        r"""(?:['"])https?://localhost(?::\d+)?[/\w]*(?:['"])"""
    )
    for i, line in enumerate(source.splitlines(), 1):
        match = url_pattern.search(line)
        if match:
            # Skip if inside a comment
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # Skip if inside uvicorn.run (already handled)
            if "uvicorn.run" in line:
                continue

            url = match.group().strip('"').strip("'")

            # Distinguish CORS allow_origins (handled by CORS checker) from
            # other hardcoded URLs
            if "allow_origins" in line or "CORS" in line.upper():
                # Already covered by the CORS checker — skip to avoid duplicate
                continue

            report.issues.append(Issue(
                severity=Severity.MEDIUM,
                category=Category.HARDCODED_URL,
                summary=f"Hardcoded localhost URL: {url}",
                file_path=rel,
                line_number=i,
                detail=(
                    "Hardcoded URLs may not resolve under enlace because the app "
                    "will be served at a different origin/port."
                ),
                suggestion=(
                    f"Make it configurable with the current value as default so "
                    f"standalone operation is preserved:\n"
                    f"  import os\n"
                    f"  MY_URL = os.environ.get('MY_URL', '{url}')"
                ),
            ))


def _scan_python_for_bare_imports(
    source: str, rel: str, py_file: Path, app_dir: Path, report: DiagnosticReport,
):
    """Detect bare imports that could collide on sys.path."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return

    # Collect sibling module names (files in same dir)
    sibling_names = {
        p.stem for p in py_file.parent.glob("*.py") if p.stem != "__init__"
    }

    bare_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            # It's an absolute import — check if it refers to a local sibling
            top_module = node.module.split(".")[0]
            if top_module in sibling_names and top_module not in {
                "os", "sys", "json", "re", "pathlib", "typing", "datetime",
                "collections", "functools", "itertools", "math", "uuid",
                "dataclasses", "enum", "abc", "io", "logging", "copy",
                "contextlib", "inspect", "importlib", "textwrap", "string",
                "pydantic", "fastapi", "starlette", "uvicorn", "numpy", "pandas",
            }:
                bare_imports.append(node.module)

    if bare_imports:
        unique = sorted(set(bare_imports))
        report.issues.append(Issue(
            severity=Severity.LOW,
            category=Category.BARE_IMPORTS,
            summary=f"Bare imports that resolve to local siblings: {', '.join(unique)}",
            file_path=rel,
            detail=(
                "These imports work because enlace adds the parent directory to "
                "sys.path, but a name collision with any other package could cause "
                "silent bugs."
            ),
            suggestion=(
                "Consider using relative imports or a package structure with "
                "__init__.py to make the import resolution unambiguous."
            ),
        ))


def _check_js_frontend(app_dir: Path, report: DiagnosticReport):
    """Scan JavaScript/TypeScript files for compatibility issues."""
    frontend_dirs: list[Path] = []
    for name in ("frontend", "client", "web", "app", "src"):
        candidate = app_dir / name
        if candidate.is_dir():
            frontend_dirs.append(candidate)
    if not frontend_dirs:
        frontend_dirs = [app_dir]

    js_files: list[Path] = []
    for d in frontend_dirs:
        for pattern in ("**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"):
            js_files.extend(d.glob(pattern))

    # Filter out node_modules, .next, dist, etc.
    js_files = [
        f for f in js_files
        if not any(
            part.startswith(("node_modules", ".next", "dist", ".nuxt", ".svelte-kit"))
            for part in f.parts
        )
    ]

    for js_file in js_files:
        try:
            source = js_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel = _relative(js_file, app_dir)

        _scan_js_for_hardcoded_urls(source, rel, report)
        _scan_js_for_websocket(source, rel, report)


def _scan_js_for_hardcoded_urls(
    source: str, rel: str, report: DiagnosticReport
):
    """Detect hardcoded API base URLs in JS/TS code."""
    # Pattern: string literal containing http://localhost with optional port
    url_pattern = re.compile(
        r"""(?:['"`])https?://localhost(?::(\d+))?(/[^'"`\s]*)?(?:['"`])"""
    )
    for i, line in enumerate(source.splitlines(), 1):
        # Skip comments
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue

        match = url_pattern.search(line)
        if match:
            url = match.group().strip("'\"` ")
            report.issues.append(Issue(
                severity=Severity.CRITICAL,
                category=Category.HARDCODED_URL,
                summary=f"Hardcoded localhost URL in frontend: {url}",
                file_path=rel,
                line_number=i,
                detail=(
                    "Frontend makes requests to a hardcoded localhost address. "
                    "Under enlace, the API is at /api/{app_name}/ on the same "
                    "origin, so fetch calls should use relative paths."
                ),
                suggestion=(
                    "Make the URL configurable with the current value as default, "
                    "so the app still works standalone:\n"
                    f"  const API_BASE = process.env.NEXT_PUBLIC_API_BASE || \"{url}\";\n"
                    "Then, when building for enlace, set the env var:\n"
                    "  NEXT_PUBLIC_API_BASE=/api/{app_name} npm run build\n"
                    "Standalone (no env var): uses the original localhost URL unchanged."
                ),
            ))

    # Also check for env var patterns that reference localhost
    env_pattern = re.compile(r"""NEXT_PUBLIC_API|VITE_API|REACT_APP_API""")
    for i, line in enumerate(source.splitlines(), 1):
        if env_pattern.search(line) and "localhost" in line:
            report.issues.append(Issue(
                severity=Severity.INFO,
                category=Category.ENV_CONFIG,
                summary="Environment variable defaults to localhost (already configurable)",
                file_path=rel,
                line_number=i,
                detail=(
                    "The URL is already read from an environment variable — good. "
                    "The default falls back to localhost, which is correct for "
                    "standalone development."
                ),
                suggestion=(
                    "When building for enlace, set the env var to the enlace path:\n"
                    "  NEXT_PUBLIC_API_BASE=/api/{app_name} npm run build\n"
                    "No code change needed — just a build-time override."
                ),
            ))


def _scan_js_for_websocket(source: str, rel: str, report: DiagnosticReport):
    """Detect WebSocket usage that may need special routing."""
    ws_pattern = re.compile(
        r"""new\s+WebSocket\s*\(|\.connect\s*\(\s*['"`]ws[s]?://"""
    )
    for i, line in enumerate(source.splitlines(), 1):
        if ws_pattern.search(line):
            report.issues.append(Issue(
                severity=Severity.MEDIUM,
                category=Category.WEBSOCKET,
                summary="WebSocket connection detected",
                file_path=rel,
                line_number=i,
                detail=(
                    "WebSocket connections need special routing consideration "
                    "under enlace. The ASGI mount handles WebSocket upgrade "
                    "natively, but the connection URL must use the correct path."
                ),
                suggestion=(
                    "Ensure WebSocket URL uses relative path and the enlace "
                    "route prefix: ws://{host}/api/{app_name}/ws"
                ),
            ))
            return  # One per file is enough


def _check_env_files(app_dir: Path, report: DiagnosticReport):
    """Check .env files for hardcoded configuration."""
    for env_name in (".env", ".env.local", ".env.development", ".env.production"):
        for root in [app_dir] + [app_dir / d for d in ("frontend", "backend", "client")]:
            env_file = root / env_name
            if not env_file.exists():
                continue
            try:
                text = env_file.read_text()
            except Exception:
                continue

            rel = _relative(env_file, app_dir)
            for i, line in enumerate(text.splitlines(), 1):
                if "localhost" in line and not line.lstrip().startswith("#"):
                    report.issues.append(Issue(
                        severity=Severity.INFO,
                        category=Category.ENV_CONFIG,
                        summary=f"Environment file references localhost: {line.strip()[:60]}",
                        file_path=rel,
                        line_number=i,
                        detail=(
                            "This is normal for standalone development. When "
                            "deploying under enlace, override these values via "
                            "a .env.production or build-time env vars."
                        ),
                        suggestion=(
                            "No change needed for standalone. For enlace, create a "
                            ".env.production (or set at build time) with the enlace "
                            "paths, e.g. NEXT_PUBLIC_API_BASE=/api/{app_name}"
                        ),
                    ))


def _check_node_backend(app_dir: Path, report: DiagnosticReport):
    """Check for Node.js/Express backend (non-Python)."""
    for name in ("server.js", "index.js", "app.js"):
        for root in [app_dir] + [app_dir / d for d in ("backend", "src", "api")]:
            js_entry = root / name
            if not js_entry.exists():
                continue
            try:
                source = js_entry.read_text()
            except Exception:
                continue

            if "express" in source.lower() or "require('express')" in source:
                if not report.backend_framework:
                    report.backend_framework = "express"
                report.issues.append(Issue(
                    severity=Severity.MEDIUM,
                    category=Category.MISSING_ENTRY_POINT,
                    summary="Express/Node.js backend detected — enlace expects Python (ASGI)",
                    file_path=_relative(js_entry, app_dir),
                    detail=(
                        "enlace composes ASGI applications (FastAPI, Starlette). "
                        "Node.js backends need to be either ported to Python or "
                        "run as a separate process with enlace proxying to them."
                    ),
                    suggestion=(
                        "Port the Express routes to FastAPI (see enlace's "
                        "twp-migrate-app skill for a recipe). Most Express → FastAPI "
                        "ports are straightforward 1:1 route translations."
                    ),
                ))
                return


def _check_html_for_issues(app_dir: Path, report: DiagnosticReport):
    """Scan HTML files for hardcoded URLs and absolute asset paths."""
    html_dirs: list[Path] = []
    for name in ("frontend", "public", "dist", "out", "build"):
        candidate = app_dir / name
        if candidate.is_dir():
            html_dirs.append(candidate)
    if not html_dirs:
        html_dirs = [app_dir]

    html_files: list[Path] = []
    for d in html_dirs:
        html_files.extend(d.glob("**/*.html"))
    html_files = [
        f for f in html_files
        if "node_modules" not in str(f) and ".next" not in str(f)
    ]

    for html_file in html_files:
        try:
            source = html_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel = _relative(html_file, app_dir)

        # Check for hardcoded localhost in script srcs, fetch, etc.
        url_pattern = re.compile(r"""https?://localhost(?::\d+)?[/\w.-]*""")
        for i, line in enumerate(source.splitlines(), 1):
            match = url_pattern.search(line)
            if match:
                report.issues.append(Issue(
                    severity=Severity.CRITICAL,
                    category=Category.HARDCODED_URL,
                    summary=f"Hardcoded localhost URL in HTML: {match.group()[:60]}",
                    file_path=rel,
                    line_number=i,
                    suggestion="Use relative paths instead of absolute localhost URLs.",
                ))


def _check_data_paths(app_dir: Path, report: DiagnosticReport):
    """Check if data storage uses conventional paths."""
    for subdir_name in ("backend", "src", "api", ""):
        root = app_dir / subdir_name if subdir_name else app_dir
        for py_file in root.glob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            rel = _relative(py_file, app_dir)

            # Detect Path(__file__).parent / "data" pattern
            if re.search(r"""Path\s*\(\s*__file__\s*\).*['"]data['"]""", source):
                report.issues.append(Issue(
                    severity=Severity.LOW,
                    category=Category.DATA_PATH,
                    summary="Data stored relative to script location",
                    file_path=rel,
                    detail=(
                        "Data is stored alongside the code. This works under enlace "
                        "(the module path is preserved), but differs from the papp "
                        "convention of ~/.local/share/papp/{app_name}/."
                    ),
                    suggestion=(
                        "Consider using a configurable data directory, e.g.:\n"
                        "  DATA_DIR = Path(os.environ.get('APP_DATA_DIR', "
                        "Path.home() / '.local/share/papp/{app_name}'))"
                    ),
                ))
                return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _relative(file_path: Path, base: Path) -> str:
    """Return a clean relative path string, or the full path if not relative."""
    try:
        return str(file_path.relative_to(base))
    except ValueError:
        return str(file_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def diagnose_app(app_dir, *, app_name: str = "") -> DiagnosticReport:
    """Diagnose an app directory for enlace compatibility.

    Scans for hardcoded URLs, CORS middleware, SSR requirements, missing
    entry points, and other patterns that prevent or complicate mounting
    under the enlace platform.

    Args:
        app_dir: Path to the app directory to diagnose.
        app_name: Override app name (defaults to directory name).

    Returns:
        DiagnosticReport with all findings.
    """
    app_dir = Path(app_dir).resolve()
    if not app_name:
        app_name = app_dir.name

    report = DiagnosticReport(app_dir=app_dir, app_name=app_name)

    # Run all checks
    _check_frontend(app_dir, report)
    _check_entry_points(app_dir, report)
    _check_python_backend(app_dir, report)
    _check_node_backend(app_dir, report)
    _check_js_frontend(app_dir, report)
    _check_html_for_issues(app_dir, report)
    _check_ssr_framework(app_dir, report)
    _check_env_files(app_dir, report)
    _check_data_paths(app_dir, report)

    # Sort issues by severity
    severity_order = {
        Severity.CRITICAL: 0,
        Severity.MEDIUM: 1,
        Severity.LOW: 2,
        Severity.INFO: 3,
    }
    report.issues.sort(key=lambda i: severity_order[i.severity])

    return report
