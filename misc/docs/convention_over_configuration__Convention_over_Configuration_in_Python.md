# Convention over Configuration in Python: A Design Guide for Multi-App Platforms

**Author:** Thor Whalen  
**Date:** 2026-04-08

---

## Executive Summary

**Convention over Configuration (CoC) is a design paradigm where a framework derives behavior from structure and naming, requiring developers to specify only what deviates from the norm.** For a Python-based multi-app composition platform, CoC can dramatically reduce boilerplate — placing `apps/my_tool/server.py` automatically mounts at `/api/my_tool/` — but poorly designed conventions create opaque "magic" that frustrates users and resists debugging.

This report distills lessons from two decades of CoC implementations across Rails, Django, pytest, Maven, Spring Boot, and dozens of other systems. It covers foundational patterns and their failure modes, Python-specific implementations (pytest, Django, setuptools, Celery, Flask), design principles for escape hatches and conflict detection, and the configuration format and validation ecosystem. It concludes with a concrete synthesis — a blueprint for a convention-over-configuration system tailored to a multi-app composition platform.

The core insight throughout: **the best CoC systems make the common case effortless while keeping every derived value inspectable and overridable**.

---

## 1. Foundations

### 1.1 Origin and Definition

Convention over Configuration was popularized by David Heinemeier Hansson through Ruby on Rails in 2004, though the concept traces to earlier technologies. The JavaBeans 1.01 specification relied on naming conventions to avoid requiring inheritance from a universal base class [1], Hibernate moved from verbose XML mapping files to convention-based class-to-table mapping, and EJB3 adopted the pattern in direct reaction to configuration complexity [2]. Maven made it a core design philosophy — a five-line `pom.xml` could compile, test, and package a JAR, compared to 100+ lines in Ant's `build.xml` [3].

DHH articulated the philosophy in The Rails Doctrine: conventions that eliminate "vain individuality" let developers "leapfrog the toils of mundane decisions" [4]. The key insight is that conventions are **compounding**: if a `Person` class maps to a `people` table, the same inflection rule lets `has_many :people` automatically find the `Person` class. Each convention unlocks deeper abstractions.

CoC is distinct from *zero* configuration. It occupies a middle ground: structure implies defaults, but every default has an explicit override path. The value proposition:

- **What it buys you:** Reduced boilerplate, faster onboarding, consistency across projects, ability to focus on application logic rather than wiring [2].
- **What it costs you:** Implicit behavior that can surprise users ("magic"), difficulty debugging when conventions interact unexpectedly, and a steeper learning curve for the *conventions themselves* even if they simplify the *code* [5][6].
- **When to reach for it:** When the convention will be applied *many times* across a codebase; when there is a natural, obvious mapping between structure and behavior; when the target audience is willing to learn the conventions [6].

Beyond Rails, CoC has been adopted widely. **Spring Boot** auto-configures beans based on classpath dependencies via `@Conditional` annotations [7]. **ASP.NET MVC** routes `/products` to `ProductsController.Index()` via reflection [6]. **Maven** enforces `src/main/java` and `src/test/java` by default [3]. **Ember.js** derives routing from filesystem structure [2]. In 2026, DHH repositioned Rails as "convention over configuration for agents," arguing that predictable file locations and consistent naming are exactly what AI agents need to navigate codebases [8].

### 1.2 Known Failure Modes

The literature consistently identifies several anti-patterns:

1. **Invisible conventions.** If a user cannot discover what conventions exist without reading source code, the system fails the discoverability test. ASP.NET MVC controllers must inherit from `Controller` and reside in a `Controllers/` folder — violate either convention silently, and you get a 404 with no explanation [9].

2. **Conventions that conflict with explicit code.** The Zen of Python's "explicit is better than implicit" represents a genuine tension with CoC [1]. The resolution is not to abandon conventions, but to make the *implicit* easily *inspectable*.

3. **Unintended scope expansion.** When ASP.NET uses reflection to find all `Controller` subclasses, accidentally referencing an assembly that contains controllers from another project can expose unintended endpoints — a reported security hole [6].

4. **Non-local reasoning.** Convention-driven systems often require understanding rules that apply *across* files and directories, not just within a single module. This makes it harder for IDE tooling ("find all references" returns nothing because wiring happens via reflection) [6].

5. **Configurable conventions creating infinite regress.** If the conventions themselves are configurable (pytest's `python_files` setting, for example), there is a risk of meta-configuration becoming as complex as the configuration it replaced. Good systems limit this to one level of indirection.

### 1.3 Making Conventions Discoverable

Best practices for discoverability include:

- **A `--show-config` or `--explain` command** that prints the resolved configuration, showing what the system inferred and from where. Maven's `mvn help:effective-pom`, Rails' `bin/rails routes`, and pytest's `--collect-only` are canonical examples. Symfony's `debug:config` command dumps both default and resolved values [10]. ASP.NET Core's `GetDebugView()` extension method returns a string showing every configuration value and its source provider [11].
- **Verbose/debug modes** that explain what was auto-configured and why. Spring Boot's `--debug` flag prints all `@Conditional` evaluations. Git's `--show-origin` and `--show-scope` flags show both the provenance and scope of every configuration value [12]:

    ```
    $ git config --list --show-origin --show-scope
    system  file:/etc/gitconfig         user.name=System Default
    global  file:/home/user/.gitconfig  user.name=Global User
    local   file:.git/config            user.name=Repo Specific
    ```

- **`--collect-only` or dry-run modes** that show what would be discovered without executing. pytest's `--collect-only` is the canonical example [13].
- **Structured error messages** that name the convention being violated. Instead of a bare 404, the system should say: `"No module named 'server.py' found in 'apps/my_tool/'. Expected file at 'apps/my_tool/server.py'."`
- **Graduated verbosity** following Ansible's model: `-v` shows task results, `-vv` adds input parameters, `-vvv` adds connection details, `-vvvv` shows protocol debugging [14]. This lets users choose their level of introspection.

**Transferable principle:** Every convention should be accompanied by a mechanism to reveal what it inferred. Each resolved value should know *where it came from* (convention, config file line N, environment variable, CLI flag).

---

## 2. Progressive Configuration and Layered Defaults

### 2.1 The Pattern

The pattern of "conventions provide defaults, but every default can be overridden at escalating levels" lacks a single canonical name but is widely known as **layered configuration** or **cascading configuration**. The 12-Factor App methodology formalized one slice — storing deploy-varying config in environment variables [15] — but real systems implement a richer hierarchy.

The nearly universal precedence order (lowest to highest priority):

```
hardcoded defaults
  → conventions (derived from filesystem)
    → config file defaults
      → environment-specific config file
        → environment variables
          → CLI flags
            → programmatic overrides
```

### 2.2 Examples Across Ecosystems

**Docker Compose** implements a five-level precedence: Dockerfile `ENV` < `env_file` attribute < `environment` attribute in `compose.yaml` < shell/`.env` interpolation < `docker compose run -e` flags [16].

**Vite** layers `.env` < `.env.local` < `.env.[mode]` < `.env.[mode].local` < existing process environment variables, with mode-specific files taking precedence over generic ones [17].

**Next.js** follows a similar pattern: `.env` → `.env.development` / `.env.production` → `.env.local`. Process-level environment variables take the highest precedence [18].

**ASP.NET Core** provides the most explicit layering: `appsettings.json` < `appsettings.{Environment}.json` < User Secrets (dev only) < environment variables < command-line arguments [19].

**pytest** takes a first-match-wins approach across config files (`pytest.toml` > `pytest.ini` > `pyproject.toml` > `tox.ini` > `setup.cfg`), with options **never merged** across files, and CLI flags overriding everything [20]. The `conftest.py` layering is particularly elegant: a `conftest.py` in a subdirectory overrides fixtures and hooks from parent directories, providing per-directory customization without global effect [13][21].

### 2.3 Design Principles for Layering

1. **Document the precedence order prominently.** Users cannot debug override conflicts if they don't know which source wins.
2. **Provide a "resolved view"** that shows the final merged configuration with annotations showing which source contributed each value.
3. **Make layer boundaries explicit.** Each override mechanism should be clearly distinct (file vs. env var vs. CLI flag), not ambiguously interleaved.
4. **Last-writer-wins is simpler than merge for scalars.** For scalar values, later layers should replace earlier ones entirely. For collections (lists of routes, plugin lists), define explicitly whether the behavior is replace, append, or merge.
5. **Default to deep merge for objects, but provide explicit replace-entirely semantics.** Shallow merge replaces entire nested objects — if a user overrides `{ server: { port: 8080 } }`, they lose the default `host` value. Microsoft's BuildXL explicitly provides both `merge` (deep recursive) and `override` (shallow) as separate operations [22]. Make the distinction explicit.

---

## 3. Configurable Conventions (Meta-Configuration)

### 3.1 The Pattern

Some systems let you configure the conventions themselves. This is the "meta" level: not "what is the route for this app?" but "how does the system *determine* the route?"

### 3.2 pytest: The Gold Standard

pytest's convention configuration is instructive because it is well-bounded:

```toml
# pyproject.toml — change "test" convention to "check"
[tool.pytest.ini_options]
python_files = ["check_*.py"]
python_classes = ["Check"]
python_functions = ["*_check"]
testpaths = ["tests", "integration"]
```

These settings change *how discovery works*, not just *what is discovered*. The design is carefully limited: you can change the naming patterns and search paths, but not the fundamental algorithm (walk directories → match files → import → match classes/functions → collect) [13][23].

**Key principle:** Configurable conventions should let you change the *mapping rules* but not the *discovery machinery*. The machinery is the framework's contract; the mapping rules are the user's vocabulary.

Similarly, **Django's `AUTH_USER_MODEL`** swaps the entire User model, affecting authentication, admin, permissions, and every ForeignKey reference [24]. **ESLint's flat config** (v9+) lets you define which files to lint using glob patterns [25]. **Maven** allows overriding source directories but explicitly warns against it [26].

### 3.3 Avoiding Infinite Regress

The risk with meta-configuration is recursive: if the conventions are configurable, where is the configuration for the conventions stored? Systems handle this with a single, fixed entry point:

- pytest always looks for `pytest.ini`, `pyproject.toml`, or `setup.cfg` in the project root — this is a non-configurable convention [20].
- Django requires `INSTALLED_APPS` in a settings module specified by `DJANGO_SETTINGS_MODULE` — the env var is the fixed entry point.
- Setuptools reads `pyproject.toml` — the filename and its location (project root) are fixed by PEP 517 [27].

**Principle:** Every configurable-convention system needs at least one *hardcoded* convention: the location and format of the meta-configuration file. This is one level of indirection only — configure the conventions, but not the mechanism that reads the meta-configuration.

**The ESLint cautionary tale.** ESLint's old `eslintrc` cascade system grew so complex — with `extends` chains, `overrides` with glob patterns, and `env` configurations — that its creator Nicholas Zakas admitted the interaction rules were "confusing even to us" [25]. This complexity led to a complete redesign (flat config). Django's `AUTH_USER_MODEL` has a hidden temporal constraint: it must be set before the first migration, and changing it later requires complex migration surgery [24]. These cases show that **meta-configuration creates coupling surface area that grows nonlinearly**.

**Design principles for meta-configuration:**

- **One level deep only.** Allow configuring conventions but not the mechanism that reads the meta-configuration.
- **Independent axes of override.** pytest provides separate controls for file naming, class naming, function naming, and directory paths. Users can change one without touching others.
- **Defaults must always work.** The zero-config path should produce working results.
- **Warn on deviation.** Good systems explain consequences when users override conventions.

---

## 4. Python Case Studies

### 4.1 pytest's Collection and Discovery

pytest's test discovery is the most sophisticated CoC system in the Python ecosystem.

**The collection tree.** pytest builds a hierarchy: `Session → Dir/Package → Module → Class → Function`. Each level is a "collector" node that can yield child items [28]. The tree is built by walking the filesystem using `scandir()`, applying naming conventions at each level. Each node is assigned a unique `::` -separated ID like `tests/test_api.py::TestUsers::test_create`.

**Convention defaults:**

- Directories: recurse into all subdirectories (unless excluded by `norecursedirs` — defaults include `.git`, `build`, `node_modules`)
- Files: match `test_*.py` and `*_test.py`
- Classes: match `Test*` (no `__init__` method)
- Functions: match `test_*`

**Override mechanisms (layered):**

1. **Configuration file:** Change `python_files`, `python_classes`, `python_functions`, `testpaths` in `pytest.ini` or `pyproject.toml` [23].
2. **conftest.py variables:** Set `collect_ignore` and `collect_ignore_glob` to exclude specific paths programmatically [13].
3. **`__test__` attribute:** Set `__test__ = False` on any class or module to exclude it from collection [23].
4. **Hook functions:** Implement `pytest_collect_file`, `pytest_collect_directory`, or `pytest_collection_modifyitems` to take full programmatic control [28].
5. **CLI flags:** `--ignore`, `--ignore-glob`, `--deselect` for one-off exclusions.

**The `conftest.py` mechanism** is pytest's most transferable pattern for a multi-app platform. These files serve three roles simultaneously: **fixture provider** (fixtures are available to all tests in the directory and below), **local plugin** (can implement hooks scoped to a directory subtree), and **configuration**. Tests search *upward* through parent directories for fixtures, and the first match wins — enabling fixture overriding at any directory level [29]:

```python
# root/conftest.py
@pytest.fixture
def db():
    return ProductionDB()

# root/integration/conftest.py — overrides for integration tests
@pytest.fixture
def db():
    return TestDB()
```

The hook system is equally instructive. Each hook call is a **1:N function call** across all registered implementations, with hook wrappers enabling around-advice patterns [30]. For a multi-app platform, this suggests a plugin architecture where apps can hook into discovery, routing, and composition at well-defined extension points.

```python
# Example: custom collector that discovers .yaml test specs
# conftest.py
import pytest

def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".yaml" and file_path.name.startswith("test"):
        return YamlFile.from_parent(parent, path=file_path)
```

**Known pain points:** rootdir detection is unintuitive when using separate source and build directories [31]; `conftest.py` scope confuses users (a root-level conftest can inadvertently modify `sys.path`) [32]; and the default `prepend` import mode requires unique test module names across the entire project, prompting the newer `importlib` mode [33]. The lesson: **filesystem-based discovery must handle import semantics carefully, especially in multi-app scenarios where name collisions are likely**.

### 4.2 Django's App Registry and Autodiscovery

Django's approach to CoC is more conservative — it relies heavily on *explicit registration* (`INSTALLED_APPS`) combined with *conventional file names* within registered apps. This two-phase approach (explicit registration of *apps*, conventional discovery of *components within apps*) is a pragmatic compromise that avoids the "unintended scope expansion" problem.

**The registry pattern.** Django maintains a central `Apps` registry that stores `AppConfig` instances for each installed app [34]. App initialization follows a strict three-stage process: import the app package, import its `models` submodule, then call `AppConfig.ready()` [34]. This staging prevents circular imports but creates timing constraints that surprise developers.

**Convention-driven discovery within apps:**

- `models.py` → automatically imported; model classes registered via metaclass
- `admin.py` → auto-imported by `admin.autodiscover()` when `django.contrib.admin` is in `INSTALLED_APPS` [35]
- `templatetags/` → makes template tags available [36]
- `management/commands/` → registers CLI commands [37]
- `urls.py` → *not* auto-imported; must be explicitly included via `include()` in the root URLconf

The `autodiscover_modules()` function is particularly relevant — it iterates all app configs and attempts to import a named submodule from each, silently skipping apps that lack it [38]:

```python
# Django's autodiscover pattern (simplified)
from django.utils.module_loading import autodiscover_modules

def autodiscover():
    """Import 'server' module from each installed app."""
    autodiscover_modules('server')
```

**Conflict detection.** Django's model registry detects conflicting model names within an app and raises `RuntimeError` with a clear message: `"Conflicting 'model_name' models in application 'app_label': X and Y."` This is a good example of fail-fast conflict detection.

**Pain points to avoid:** app ordering in `INSTALLED_APPS` silently affects template override precedence [39]; circular imports during loading require string-based ForeignKey references [40]; and URL routing requiring explicit `include()` wiring is inconsistent with the plug-and-play model of other conventions [34]. For a multi-app platform, this argues for **making all discovery mechanisms consistent**: if apps are auto-discovered, their routes should be too.

The `AppConfig.ready()` hook is Django's escape hatch: any startup code that doesn't fit conventions goes here. It runs after the registry is fully populated, providing a safe place for signal connections, monkey-patching, or custom registration [34].

### 4.3 Setuptools Package Discovery

Modern setuptools (≥61) demonstrates CoC at the packaging level. It automatically detects packages using two supported layouts [27][41]:

**src-layout** (preferred):
```
project_root/
├── pyproject.toml
└── src/
    └── mypkg/
        ├── __init__.py
        └── module.py
```

**flat-layout:**
```
project_root/
├── pyproject.toml
└── mypkg/
    ├── __init__.py
    └── module.py
```

The convention: any directory containing an `__init__.py` under `src/` (or the project root) is a package. The override: `[tool.setuptools.packages.find]` in `pyproject.toml` with `where`, `include`, and `exclude` directives [41].

```toml
[tool.setuptools.packages.find]
where = ["src"]
include = ["my_package*"]
exclude = ["my_package.tests*"]
```

**Safety mechanism:** For flat-layout, setuptools *refuses* to create distributions with multiple top-level packages, to prevent accidental inclusion of maintenance scripts or test code [41]. This is an example of a convention that *constrains* rather than *enables*, protecting users from themselves.

**Flit** takes a more opinionated approach, supporting only a single importable module or package and reading version from `__version__` in the source [42]. **Hatchling** and **PDM** follow similar auto-discovery patterns with explicit override capabilities [43].

**Transferable lesson:** Choose one canonical layout and enforce it, with clear error messages when apps deviate. When automatic discovery could have dangerous side effects (exposing unintended code), add a safety check that requires explicit opt-in for unusual cases.

### 4.4 Other Python CoC Examples

**Celery's autodiscover** closely mirrors Django's pattern — `app.autodiscover_tasks()` scans a list of packages for a conventional `tasks.py` module (customizable via `related_name`) [44]:

```python
app = Celery('myproject')
app.autodiscover_tasks(['foo', 'bar', 'baz'])
# Imports foo.tasks, bar.tasks; baz.tasks skipped if absent
```

**Known pain points:** Discovery is non-recursive by default: `foo.subpkg.tasks` is *not* found unless `foo.subpkg` is listed explicitly [45]. Import errors within `tasks.py` are silently swallowed if they resemble "module not found" errors, making debugging extremely difficult [46]. The `related_name` parameter allows changing the convention (`tasks.py` → `jobs.py`), but this is per-call, not configurable globally.

**Anti-pattern lesson:** Silent error swallowing during convention-based discovery is one of the worst failure modes. The system should distinguish between "module doesn't exist" (expected, skip) and "module exists but has import errors" (unexpected, report).

**Scrapy's SpiderLoader** reads `SPIDER_MODULES` from settings and recursively walks submodules to find `Spider` subclasses [47]. A notable failure mode: all spider modules load into memory at startup even when running a single spider, and a broken import in any spider file prevents all spiders from loading.

**Flask Blueprints** are explicitly registered (no auto-discovery). This is a deliberate choice favoring explicitness, but it means adding a new blueprint always requires touching two files [48]. Community patterns exist for autodiscovery of a `blueprints/` directory, but they are not standardized.

**Click's `MultiCommand`** enables filesystem-based command discovery [49], and **Pydantic Settings** maps field names to environment variables by convention (`auth_key` → `AUTH_KEY`), with `env_prefix` adding namespace prefixes [50]. The priority order — init kwargs > env vars > dotenv file > secrets directory > defaults — is customizable via `settings_customise_sources()`.

---

## 5. Design Principles for Override Systems

### 5.1 The Escape Hatch Pattern

A well-designed CoC system provides escape hatches at multiple levels:

| Level | Mechanism | Example |
|-------|-----------|---------|
| **Selective override** | Override one derived value | `__route__ = "/api/tools/my-tool/v2"` on a module |
| **Pattern override** | Change the derivation rule | `route_from = "docstring"` in config |
| **Full bypass** | Skip conventions entirely, provide explicit config | A complete manifest file that lists all apps and routes |

**Principle:** Escape hatches should be *incremental*. A user who needs to change one app's route should not be forced to configure all routes explicitly. This is the "partial override" principle.

The **eject anti-pattern** is the canonical failure of this principle. Create React App's `npm run eject` is a one-way operation that copies all hidden configuration into the project, destroying future upstream updates. Tools like CRACO emerged specifically to avoid it [51]. The lesson: **binary choices between "zero control" and "total ownership" are design failures**. pytest's granular `conftest.py`, Docker Compose's override files, and Git's layered scopes all demonstrate that incremental, partial overrides are achievable and essential.

### 5.2 Exposing Derived Configuration

The system should provide a way to answer the question: "What did the system infer, and why?" The gold standard is **Git's `--show-origin`** — each value shows both its provenance (which file) and scope (system/global/local) [12]. Other excellent examples include **`docker compose config`** (renders the fully resolved Compose model with all merges applied) [52], **`nginx -T`** (dumps the entire resolved configuration with `include` files inlined) [53], **`pip config debug`** (lists all possible config file locations and whether each exists) [54], and **ESLint's `--print-config`** (outputs fully resolved configuration for a specific file) [55].

A good `--show-config` command has five properties:

1. Shows **provenance** (where each value came from)
2. Distinguishes **defaults from overrides**
3. Reveals **what was auto-discovered**
4. Makes the **resolution order** clear
5. Supports **machine-readable output** (JSON/YAML) for tooling

```python
# Programmatic API
config = platform.resolve_config()
for app in config.apps:
    print(f"{app.name}: route={app.route} (source={app.route_source})")
```

### 5.3 Validation and Conflict Detection

**Conflict detection strategies:**

1. **Fail-fast on duplicates.** If two apps resolve to the same route prefix, raise an error at discovery time, not at request time. Django does this for model names.
2. **Report all conflicts at once.** Don't stop at the first conflict — collect all of them and present a summary.
3. **Distinguish errors from warnings.** A duplicate route is an error; a module with both `server.py` and `app.py` is a warning (ambiguity that should be resolved).

Research by Yin et al. (SOSP 2011) found that **38–54% of configuration errors** in production systems are caused by illegal parameters violating format or semantic rules, and only **7–15% of systems** provide explicit error messages pinpointing the issue [56]. Most systems dramatically underinvest in configuration error reporting.

**Ambiguity resolution with explicit priority:**

```python
# Explicit priority list for entry point detection
ENTRY_POINT_PRIORITY = ['server.py', 'app.py', 'main.py']

def find_entry_point(app_dir: Path) -> Optional[Path]:
    """Return the first matching entry point, or None."""
    for name in ENTRY_POINT_PRIORITY:
        candidate = app_dir / name
        if candidate.exists():
            return candidate
    return None
```

For a multi-app platform, conflict detection should follow four principles: **show what conflicted** (e.g., "Apps 'auth' and 'users' both resolve to route prefix `/api/auth/`"), **show where each value came from** (convention vs. config file, with file paths and line numbers), **suggest resolution** (e.g., "Set `route_prefix` in one app's config to disambiguate"), and **reference documentation** with a URL to the relevant configuration guide.

### 5.4 Discoverability as Documentation

Auto-generated documentation transforms opaque conventions into transparent features. **OpenAPI/Swagger** generates documentation directly from code structure [57]. **Django's `inspectdb`** reverse-engineers database conventions into model code. **Django REST Framework's browsable API** makes the API its own documentation.

For a multi-app platform, three discoverability mechanisms are essential: (1) a **`--show-config` command** with provenance annotations; (2) **scaffold commands** that generate the conventional directory structure (`platform init my-app` creates `apps/my_app/server.py` with boilerplate), teaching conventions by example; and (3) **schema-generated documentation** where the Pydantic config model auto-generates a reference of all configurable values, their types, defaults, and descriptions.

---

## 6. Configuration Formats and Validation

### 6.1 Format Comparison

| Format | Strengths | Weaknesses | Best for |
|--------|-----------|------------|----------|
| **TOML** | Human-readable, typed values, PEP 517 standard, in stdlib since 3.11 | No anchors/references, less expressive than YAML, no write support in stdlib | Python-native tools, `pyproject.toml` integration |
| **YAML** | Anchors, merge keys, multi-document, widely used in DevOps | Indentation-sensitive, type coercion gotchas (`yes` → `True`, the "Norway Problem"), security concerns with `yaml.load()` | Kubernetes-adjacent tools, complex hierarchical config |
| **Python dicts** | Full language power, IDE support, type checking | Not declarative, executes arbitrary code, harder to parse statically | Plugin systems where config needs computation |

For a Python-native platform, **TOML is the natural choice**: it's typed, standardized (PEP 680 added `tomllib` to stdlib in 3.11), safe by design (no code execution), and aligns with the `pyproject.toml` ecosystem [58]. YAML's type coercion gotchas — where the country code `NO` becomes `False` — caused real-world data corruption [59][60]. Python config files (like Django's `settings.py`) are best reserved for advanced "escape hatch" scenarios where users need computed configuration.

### 6.2 Configuration Libraries

**Pydantic Settings** (`pydantic-settings`):

- *What it buys you:* Type-safe configuration with validation, automatic env var loading, IDE autocompletion, nested model support. Field names automatically map to env vars (`db_host` → `APP_DB_HOST` with `env_prefix`), nested models use delimiter-based flattening (`APP_DATABASE__HOST`), and the priority order is customizable via `settings_customise_sources()`.
- *What it costs you:* Limited to env vars and `.env` files out of the box; no built-in multi-environment switching or file format support.
- *When to reach for it:* FastAPI projects; when config is simple and type safety is paramount [50][61].

**Dynaconf:**

- *What it buys you:* Multi-format file support (TOML, YAML, JSON, INI, Python), layered environments, external loaders (Redis, Vault), Django/Flask integration, Pydantic integration for validation.
- *What it costs you:* Larger API surface, more concepts to learn, less type-safe by default without Pydantic integration.
- *When to reach for it:* Complex applications with multiple deployment environments, secrets management needs, or legacy config files [62][61].

**Hydra + OmegaConf:**

- *What it buys you:* Configuration composition (merge fragments), sweep/multirun for experiments, CLI-driven overrides (`python app.py db=postgresql db.port=3307`). Built on OmegaConf with variable interpolation (`${server.host}`), structured configs via dataclasses.
- *What it costs you:* YAML-only, opinionated working directory changes, steep learning curve.
- *When to reach for it:* ML/research pipelines with many configuration dimensions [63][64].

**OmegaConf** provides the merge semantics underlying Hydra, with `MISSING` sentinels for mandatory fields and `set_readonly`/`set_struct` flags for immutability and structural validation [64]. **confuse** (from the beets project) offers OS-aware config directory discovery and lazy type validation on access [65].

### 6.3 Schema-Validated Configuration

The "Parse, Don't Validate" principle (Alexis King, 2019) applied to configuration means: read raw data (TOML/YAML/env), parse it into a strongly-typed validated object, and have the rest of the application work only with the typed result [66]. Pydantic implements this pattern perfectly — `BaseSettings` either produces a valid config object or raises `ValidationError` at startup with structured errors showing field path, expected type, received value, and error type.

For a multi-app platform, Pydantic-based config provides four benefits simultaneously: **fail-fast validation** catches errors before the platform starts serving; **self-documenting schemas** via `model_json_schema()` generate JSON Schema with all field descriptions, types, and constraints; **IDE autocomplete** works naturally; and **`extra='forbid'`** catches typos in configuration keys that would otherwise be silently ignored.

Beyond Pydantic, **attrs + cattrs** provides a functional composition approach where structuring rules are separate from models [67], and **marshmallow** offers mature serialization with an extensive ecosystem.

---

## 7. Synthesis: A Convention System for Multi-App Composition

Drawing from the case studies above, here is a concrete blueprint for a convention-over-configuration system tailored to a multi-app platform.

### 7.1 The Convention Stack

```
apps/
├── my_tool/
│   ├── server.py          # Convention: entry point
│   ├── app.toml           # Override: explicit config (optional)
│   └── static/            # Convention: served at /static/my_tool/
├── dashboard/
│   ├── server.py
│   └── app.toml
└── platform.toml           # Global overrides and meta-config
```

**Default conventions:**

- Directory name → route prefix: `my_tool/` → `/api/my_tool/`
- Entry point: first of `server.py`, `app.py`, `main.py` found
- Display name: derived from directory name with `_` → space, title-cased
- Directories starting with `_` or `.` are skipped

### 7.2 The Override Hierarchy (lowest to highest priority)

1. Hardcoded defaults (in the platform code)
2. Conventions (derived from filesystem)
3. `app.toml` (per-app overrides)
4. `platform.toml` (global overrides)
5. Environment variables (`PLATFORM_APPS__MY_TOOL__ROUTE=/custom/path`)
6. CLI flags (`--app my_tool --route /custom/path`)

### 7.3 Meta-Configuration

```toml
# platform.toml
[conventions]
entry_points = ["server.py", "app.py"]      # ordered priority
route_from = "directory_name"                 # or "module_attribute", "docstring"
route_transform = "lowercase_underscore"      # naming transform
apps_dir = "apps"                             # where to look
```

### 7.4 Discoverability

```bash
$ myplatform show-config --verbose
Platform Configuration (resolved)
==================================

Meta-conventions (from platform.toml):
  entry_points: ['server.py', 'app.py']
  route_from: directory_name
  apps_dir: apps

Discovered Apps:
  my_tool
    route:  /api/my_tool/     [convention: directory_name]
    entry:  apps/my_tool/server.py  [convention: first match]
    
  dashboard  
    route:  /api/dash/         [override: app.toml line 2]
    entry:  apps/dashboard/server.py  [convention: first match]

Conflicts: None
Warnings: None
```

### 7.5 The Discovery Algorithm

Modeled on pytest's collector pattern, with Pydantic for validation and provenance tracking:

```python
from pathlib import Path
from typing import Protocol, Optional
from pydantic import BaseModel, Field
import tomllib

class AppConfig(BaseModel):
    """Resolved configuration for a single app."""
    name: str
    route_prefix: str
    module_path: str
    source: str = Field(description="Where this config came from")

class PlatformConfig(BaseModel):
    """Resolved configuration for the entire platform."""
    apps: list[AppConfig]
    
    def check_conflicts(self) -> list[str]:
        routes = {}
        errors = []
        for app in self.apps:
            if app.route_prefix in routes:
                errors.append(
                    f"Route conflict: '{app.route_prefix}' claimed by "
                    f"both '{app.name}' and '{routes[app.route_prefix]}'"
                )
            routes[app.route_prefix] = app.name
        return errors

class AppDiscoverer(Protocol):
    """Protocol for app discovery strategies."""
    def discover(self, apps_dir: Path) -> list[AppConfig]: ...

class ConventionDiscoverer:
    """Filesystem-convention-based app discovery."""
    
    def __init__(
        self,
        entry_points: list[str] = ("server.py", "app.py"),
        route_from: str = "directory_name",
    ):
        self.entry_points = entry_points
        self.route_from = route_from
    
    def discover(self, apps_dir: Path) -> list[AppConfig]:
        apps = []
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir() or app_dir.name.startswith(('_', '.')):
                continue
            
            entry = self._find_entry_point(app_dir)
            if entry is None:
                continue  # Not an app directory
            
            config = AppConfig(
                name=app_dir.name,
                route_prefix=f"/api/{app_dir.name}/",
                module_path=self._to_module_path(entry),
                source="convention",
            )
            
            # Apply per-app overrides
            override_file = app_dir / "app.toml"
            if override_file.exists():
                config = self._apply_overrides(config, override_file)
            
            apps.append(config)
        return apps
    
    def _find_entry_point(self, app_dir: Path) -> Optional[Path]:
        for name in self.entry_points:
            candidate = app_dir / name
            if candidate.exists():
                return candidate
        return None
    
    def _to_module_path(self, path: Path) -> str:
        return str(path.with_suffix('')).replace('/', '.')
    
    def _apply_overrides(
        self, config: AppConfig, toml_path: Path
    ) -> AppConfig:
        with open(toml_path, 'rb') as f:
            overrides = tomllib.load(f)
        updates = {}
        if 'route' in overrides:
            updates['route_prefix'] = overrides['route']
            updates['source'] = f"override: {toml_path}"
        return config.model_copy(update=updates)
```

### 7.6 Recommended Library Stack

For a platform where convention-derived configuration is the primary source and explicit config files are the override mechanism:

1. **Discovery layer:** Custom Python code that walks the `apps/` directory, applies naming conventions, and builds a configuration tree (modeled on pytest's collector).
2. **Override layer:** TOML files (`app.toml` per app, `platform.toml` global) parsed with `tomllib`.
3. **Validation layer:** Pydantic models that define the schema for resolved configuration, providing type checking and clear error messages.
4. **Environment layer:** Pydantic Settings or Dynaconf for env-var overrides in deployment.

---

## 8. Anti-Patterns Summary

| Anti-pattern | Example | Mitigation |
|-------------|---------|------------|
| **Silent failure** | Celery swallows ImportErrors during autodiscovery [46] | Distinguish "not found" from "found but broken" |
| **Unintended discovery** | ASP.NET finds controllers in referenced assemblies [6] | Restrict discovery to explicit directories |
| **No provenance** | Config value present but unknown origin | Track and report the source of each resolved value |
| **All-or-nothing override** | CRA `eject` forces total ownership [51] | Support incremental/partial overrides at every level |
| **Undiscoverable conventions** | Must read source code to learn what conventions exist | `--show-config`, `--explain`, scaffold commands |
| **Silent conflict** | Two apps claim the same route, last one wins | Fail-fast with clear error listing all conflicts |
| **Infinite regress** | Config for the config for the config... | One hardcoded entry point (fixed filename/location) |
| **Deep merge surprises** | Shallow merge drops unmentioned nested keys | Default deep merge; explicit replace-entirely when needed |

---

## 9. Key Takeaways

1. **Conventions must be discoverable.** A `show-config` command that prints what was inferred, from where, is non-negotiable. Verbose/debug modes and dry-run commands are the primary tools.

2. **Override granularity matters.** The user who needs to change one app's route should not be forced to configure everything explicitly. Partial overrides — attribute-level → file-level → full bypass — are essential.

3. **The entry point for meta-configuration must be fixed.** Every chain of configurable conventions needs one non-configurable anchor (a known file name in a known location). Configure the conventions, not the mechanism that reads the meta-configuration.

4. **Silent failures are the worst failure mode.** When discovery skips something, the user must be able to find out *why*. Distinguish "not found" from "found but broken."

5. **Separate registration from discovery.** Django's two-phase approach (register apps explicitly in `INSTALLED_APPS`, then discover components *within* registered apps by convention) avoids the "accidental discovery" class of bugs.

6. **Conflict detection must be eager.** Check for naming collisions, ambiguities, and invalid configurations at startup, not at request time. Report all conflicts at once with provenance, not just the first one.

7. **Parse configuration into typed objects at startup.** Pydantic's `BaseSettings` pattern — parse raw config into validated, immutable, self-documenting typed objects or fail immediately with structured errors — eliminates an entire class of runtime configuration bugs and auto-generates schema documentation as a side effect.

---

## REFERENCES

[1] Wikipedia, "Convention over configuration." [Online]. Available: [Convention over configuration - Wikipedia](https://en.wikipedia.org/wiki/Convention_over_configuration)

[2] Devopedia, "Convention over Configuration." [Online]. Available: [Convention over Configuration](https://devopedia.org/convention-over-configuration)

[3] Baeldung, "Maven Directory Structure." [Online]. Available: [Maven](https://www.baeldung.com/maven-directory-structure)

[4] D. H. Hansson, "The Rails Doctrine." [Online]. Available: [Rails Doctrine](https://rubyonrails.org/doctrine)

[5] J. Miller, "Patterns in Practice — Convention Over Configuration," *MSDN Magazine*, Feb 2009. [Online]. Available: [Patterns in Practice](https://learn.microsoft.com/en-us/archive/msdn-magazine/2009/february/patterns-in-practice-convention-over-configuration)

[6] M. Heath, "Convention Over Configuration," May 2017. [Online]. Available: [Mark Heath blog](https://markheath.net/post/convention-over-configuration)

[7] TrendingSource, "Understanding Spring Boot: Convention over Configuration." [Online]. Available: [Spring Boot CoC](https://trendingsource.github.io/2024-01-10-understanding-spring-boot-the-benefits-of-opinionated-convention-over-configuration/)

[8] Paddo, "Your Architecture is Showing." [Online]. Available: [Architecture](https://paddo.dev/blog/your-architecture-is-showing/)

[9] C-SharpCorner, "Software Design Paradigm — Convention Over Configuration," Jan 2017. [Online]. Available: [Convention over Configuration in ASP.NET MVC](https://www.c-sharpcorner.com/article/software-design-paradigm-convention-over-configuration/)

[10] Symfony documentation, "Debug Configuration Reference." [Online]. Available: [Symfony debug:config](https://symfony.com/doc/current/reference/configuration/debug.html)

[11] A. Lock, "Debugging configuration values in ASP.NET Core," Mar 2021. [Online]. Available: [GetDebugView()](https://andrewlock.net/debugging-configuration-values-in-aspnetcore/)

[12] Git, "git-config reference." [Online]. Available: [git-config](https://git-scm.com/docs/git-config)

[13] pytest documentation, "Changing standard (Python) test discovery." [Online]. Available: [pytest discovery](https://docs.pytest.org/en/stable/example/pythoncollection.html)

[14] Ansible, "ansible-playbook CLI reference." [Online]. Available: [ansible-playbook](https://docs.ansible.com/projects/ansible/latest/cli/ansible-playbook.html)

[15] 12factor.net, "III. Config." [Online]. Available: [12-Factor Config](https://12factor.net/config)

[16] Docker, "Environment variables precedence." [Online]. Available: [Docker env precedence](https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/)

[17] Vite, "Env Variables and Modes." [Online]. Available: [Vite env](https://vite.dev/guide/env-and-mode)

[18] Next.js documentation, "Environment Variables." [Online]. Available: [Next.js env](https://nextjs.org/docs/pages/guides/environment-variables)

[19] codewithmukesh, "Environment-based Configuration in ASP.NET Core." [Online]. Available: [ASP.NET Core config](https://codewithmukesh.com/blog/environment-based-configuration-aspnet-core/)

[20] pytest documentation, "Configuration — customize settings." [Online]. Available: [pytest config](https://docs.pytest.org/en/stable/reference/customize.html)

[21] pytest documentation, "How to use fixtures." [Online]. Available: [pytest fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html)

[22] Microsoft BuildXL, "Merge and Override." [Online]. Available: [BuildXL](https://github.com/microsoft/BuildXL/blob/main/Documentation/Wiki/DScript/Merge-and-Override.md)

[23] pytest documentation, "Writing plugins." [Online]. Available: [pytest plugins](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)

[24] Django, "Customizing authentication." [Online]. Available: [Django auth](https://docs.djangoproject.com/en/6.0/topics/auth/customizing/)

[25] N. C. Zakas, "ESLint's new config system, Part 1." [Online]. Available: [ESLint config](https://eslint.org/blog/2022/08/new-config-system-part-1/)

[26] Maven, "Guide: Using one source directory." [Online]. Available: [Maven guide](https://people.apache.org/~ltheussl/maven-stage-site/guides/mini/guide-using-one-source-directory.html)

[27] setuptools documentation, "Configuring setuptools using pyproject.toml files." [Online]. Available: [setuptools pyproject.toml](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)

[28] DeepWiki, "Test Discovery and Collection | pytest-dev/pytest." [Online]. Available: [pytest DeepWiki](https://deepwiki.com/pytest-dev/pytest/2.1-test-discovery-and-collection)

[29] pytest documentation, "pytest fixtures: explicit, modular, scalable." [Online]. Available: [pytest fixtures](https://docs.pytest.org/en/6.2.x/fixture.html)

[30] pytest documentation, "Writing hook functions." [Online]. Available: [pytest hooks](https://docs.pytest.org/en/7.1.x/how-to/writing_hook_functions.html)

[31] GitHub, "pytest rootdir issues (#4594)." [Online]. Available: [pytest rootdir](https://github.com/pytest-dev/pytest/issues/4594)

[32] GitHub, "conftest.py in root directory (#2269)." [Online]. Available: [conftest sys.path](https://github.com/pytest-dev/pytest/issues/2269)

[33] pytest documentation, "Python path and import modes." [Online]. Available: [pytest pythonpath](https://docs.pytest.org/en/stable/explanation/pythonpath.html)

[34] Django documentation, "Applications." [Online]. Available: [Django Applications](https://docs.djangoproject.com/en/5.0/ref/applications/)

[35] Django documentation, "The Django admin site." [Online]. Available: [Django admin](https://docs.djangoproject.com/en/4.2/ref/contrib/admin/)

[36] Django documentation, "Custom template tags and filters." [Online]. Available: [Django templatetags](https://docs.djangoproject.com/en/6.0/howto/custom-template-tags/)

[37] Django documentation, "Custom management commands." [Online]. Available: [Django commands](https://docs.djangoproject.com/en/6.0/howto/custom-management-commands/)

[38] Django source, "django.utils.module_loading." [Online]. Available: [Django module_loading](https://docs.djangoproject.com/en/5.0/_modules/django/utils/module_loading/)

[39] Django Trac, "Ticket #25153 — INSTALLED_APPS order." [Online]. Available: [Django ordering](https://code.djangoproject.com/ticket/25153)

[40] alpharithms, "Django Circular Import Errors." [Online]. Available: [Django circular imports](https://www.alpharithms.com/django-circular-imports-153910/)

[41] setuptools documentation, "Package Discovery and Namespace Packages." [Online]. Available: [setuptools discovery](https://setuptools.pypa.io/en/latest/userguide/package_discovery.html)

[42] Flit documentation, "pyproject.toml reference." [Online]. Available: [Flit](https://flit.pypa.io/en/latest/pyproject_toml.html)

[43] Python Packaging User Guide, "Project Summaries." [Online]. Available: [Packaging tools](https://packaging.python.org/en/latest/key_projects/)

[44] Celery documentation, "First steps with Django." [Online]. Available: [Celery autodiscover](https://docs.celeryq.dev/en/v5.5.3/django/first-steps-with-django.html)

[45] GitHub Discussion, "Recursive autodiscover_tasks," celery/celery #7179. [Online]. Available: [Celery recursive discovery](https://github.com/celery/celery/discussions/7179)

[46] GitHub Discussion, "autodiscover_tasks should re-raise non-task ImportErrors," celery/celery #8620. [Online]. Available: [Celery ImportError handling](https://github.com/celery/celery/discussions/8620)

[47] Scrapy documentation, "Settings." [Online]. Available: [Scrapy settings](https://docs.scrapy.org/en/latest/topics/settings.html)

[48] Real Python, "Use a Flask Blueprint to Architect Your Applications." [Online]. Available: [Flask Blueprints](https://realpython.com/flask-blueprint/)

[49] Click documentation, "Commands and Groups." [Online]. Available: [Click MultiCommand](https://pocoo-click.readthedocs.io/en/latest/commands/)

[50] Pydantic documentation, "Settings Management." [Online]. Available: [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)

[51] Create React App, "Available Scripts — eject." [Online]. Available: [CRA eject](https://create-react-app.dev/docs/available-scripts/)

[52] Docker, "docker compose config." [Online]. Available: [compose config](https://docs.docker.com/reference/cli/docker/compose/config/)

[53] nginx, "Command-line switches." [Online]. Available: [nginx -T](https://nginx.org/en/docs/switches.html)

[54] pip, "pip config reference." [Online]. Available: [pip config](https://pip.pypa.io/en/stable/cli/pip_config/)

[55] ESLint, "Command Line Interface Reference." [Online]. Available: [ESLint CLI](https://eslint.org/docs/latest/use/command-line-interface)

[56] D. Yin et al., "An Empirical Study on Configuration Errors in Commercial and Open Source Systems," SOSP 2011. [Online]. Available: [Yin et al. 2011](https://www.sigops.org/s/conferences/sosp/2011/current/2011-Cascais/printable/12-yin.pdf)

[57] Swagger, "Documenting APIs with Swagger." [Online]. Available: [Swagger](https://swagger.io/resources/articles/documenting-apis-with-swagger/)

[58] Python Packaging, "Writing your pyproject.toml." [Online]. Available: [pyproject.toml guide](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)

[59] R. van Asseldonk, "The YAML document from hell," Jan 2023. [Online]. Available: [YAML hell](https://ruudvanasseldonk.com/2023/01/11/the-yaml-document-from-hell)

[60] InfoWorld, "7 YAML gotchas to avoid." [Online]. Available: [YAML gotchas](https://www.infoworld.com/article/2336307/7-yaml-gotchas-to-avoidand-how-to-avoid-them.html)

[61] Leapcell, "Pydantic BaseSettings vs. Dynaconf." [Online]. Available: [Pydantic vs Dynaconf](https://leapcell.io/blog/pydantic-basesettings-vs-dynaconf-a-modern-guide-to-application-configuration)

[62] dynaconf documentation, "Getting Started." [Online]. Available: [Dynaconf](https://www.dynaconf.com/)

[63] Hydra, "Getting Started." [Online]. Available: [Hydra](https://hydra.cc/docs/intro/)

[64] OmegaConf, "Usage documentation." [Online]. Available: [OmegaConf](https://omegaconf.readthedocs.io/en/2.3_branch/usage.html)

[65] confuse, "Documentation." [Online]. Available: [confuse](https://confuse.readthedocs.io/en/latest/)

[66] A. King, "Parse, don't validate," Nov 2019. [Online]. Available: [Parse don't validate](https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/)

[67] cattrs, "Documentation." [Online]. Available: [cattrs](https://catt.rs/)

[68] Software Engineering (Vazexqi), "Convention over Configuration Pattern." [Online]. Available: [CoC Pattern](http://softwareengineering.vazexqi.com/files/pattern.html)

[69] Avo, "Convention over Configuration (glossary)." [Online]. Available: [Avo CoC](https://avohq.io/glossary/convention-over-configuration)

[70] C. Leifer, "Looking at registration patterns in Django." [Online]. Available: [Django registration](https://charlesleifer.com/blog/looking-registration-patterns-django/)
