# Multi-app user data persistence with repository abstractions

**The cleanest architecture for a personal app platform routes all data through Python's `MutableMapping` interface, partitions storage by `(user_id, app_id)` key prefixes injected via pure ASGI middleware, and swaps backends — from local files to S3 — without touching app code.** This approach, built on the `dol` library's philosophy of infrastructure-isolated data access, gives each mounted sub-app a "pre-scoped" dict-like store that knows nothing about authentication. The pattern works because key-value stores compose naturally: wrap any backend behind `store[key] = value`, transform keys with prefixes and codecs, and nest stores into a "Mall" (mapping of mappings) to model multi-dimensional `stores[user_id][app_id][data_key]` access. What follows is a comprehensive guide to implementing this architecture across backend selection, frontend sync, security, and migration — with concrete code and clear dev-vs-production recommendations throughout.

*Author: Thor Whalen*

---

## How multi-tenant key-value partitioning works with MutableMapping

Multi-tenant SaaS platforms use three canonical data isolation models: **silo** (separate stores per tenant), **bridge** (shared infrastructure with logical namespace separation), and **pool** (shared everything, differentiated by tenant ID in keys). For a key-value platform built on `MutableMapping`, the bridge model using key-prefix isolation hits the sweet spot — strong logical isolation, minimal operational overhead, and natural fit with dict-like access patterns.

The core mechanism is a `PrefixedStore` wrapper that transparently prepends `{user_id}/{app_id}/` to every key operation. When an app calls `store["settings"]`, the underlying backend sees `user_42/my_app/settings`. The `dol` library provides this natively through `wrap_kvs` and `KeyCodecs`, which handle key transformation as composable layers:

```python
from dol import wrap_kvs

def make_user_app_store(backend, user_id, app_id):
    """Create a pre-scoped store for a specific user and app."""
    prefix = f"{user_id}/{app_id}/"
    return wrap_kvs(
        backend,
        id_of_key=lambda k: prefix + k,
        key_of_id=lambda _id: _id[len(prefix):] if _id.startswith(prefix) else None
    )
```

The **Mall pattern** — a mapping of mappings where outer keys select sub-stores — formalizes this into a clean hierarchy. Conceptually, a Mall is a `MutableMapping` whose values are themselves `MutableMapping` instances. This pattern emerges naturally in filesystem directory nesting (`root/user_id/app_id/`), S3 prefix hierarchies (`bucket/user_id/app_id/key`), and Redis key namespacing (`user_id:app_id:key`). A practical Mall implementation uses a factory function to create scoped sub-stores on demand:

```python
from collections.abc import MutableMapping

class Mall(MutableMapping):
    """Mapping of mappings: mall[user_id] returns a per-user MutableMapping."""
    def __init__(self, store_factory):
        self._factory = store_factory
        self._cache = {}

    def __getitem__(self, key):
        if key not in self._cache:
            self._cache[key] = self._factory(key)
        return self._cache[key]

    def __setitem__(self, key, value): self._cache[key] = value
    def __delitem__(self, key): self._cache.pop(key, None)
    def __iter__(self): yield from self._cache
    def __len__(self): return len(self._cache)
```

The tradeoffs between isolation models map cleanly to this interface:

| Strategy | Isolation strength | Ops complexity | MutableMapping fit |
|---|---|---|---|
| Key prefix on shared backend | Moderate (logical) | Low | Natural — one backend, key transforms |
| Separate directory/bucket per tenant | Strong (physical) | Medium | Natural — one store instance per tenant |
| Separate database per tenant | Strongest | High | Natural but resource-heavy |

For a platform with **<100 users**, key-prefix isolation on a shared backend is the right choice. It avoids managing N separate storage instances while providing clean logical separation. The critical caveat: prefix isolation is only as safe as your key validation — a point explored in depth in the security section below.

## Per-user store injection through pure ASGI middleware

The platform's existing auth middleware sets `scope["user"]` and `scope["state"]["user_id"]` as a cross-cutting concern. The cleanest way to extend this with per-user stores is a second pure ASGI middleware layer that reads the user ID and attaches a pre-scoped `MutableMapping` to the request scope. Apps never see authentication — they receive a store that "just works."

Starlette's documentation explicitly endorses this pattern: "If you need to share information with the underlying app or endpoints, you may store it into the scope dictionary." Pure ASGI middleware avoids the well-documented `BaseHTTPMiddleware` bug where `contextvars.ContextVar` changes don't propagate correctly through Starlette's task group handling.

The recommended hybrid approach layers pure ASGI middleware with a thin FastAPI `Depends()` wrapper for type safety:

```python
from starlette.types import ASGIApp, Receive, Scope, Send
from fastapi import Request, Depends, HTTPException
from typing import Annotated

# Layer 1: Pure ASGI store injection (runs after auth middleware)
class StoreInjectionMiddleware:
    def __init__(self, app: ASGIApp, mall: Mall):
        self.app = app
        self.mall = mall

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            user_id = scope.get("state", {}).get("user_id")
            if user_id:
                scope.setdefault("state", {})
                scope["state"]["store"] = self.mall[user_id]
        await self.app(scope, receive, send)

# Layer 2: FastAPI dependency for typed extraction
def get_store(request: Request) -> MutableMapping:
    store = getattr(request.state, "store", None)
    if store is None:
        raise HTTPException(401, "Authentication required")
    return store

UserStore = Annotated[MutableMapping, Depends(get_store)]

# Layer 3: Handler knows nothing about auth
@app.get("/data/{key}")
async def get_data(key: str, store: UserStore):
    return {"value": store.get(key)}
```

Four injection patterns were evaluated. **Factory functions** require handlers to extract `user_id` themselves, violating the "app shouldn't know about auth" constraint. **Context variables** offer implicit access from deep call chains but suffer from Starlette's task-group propagation bugs. **FastAPI `Depends()` alone** is framework-specific and doesn't work for pure ASGI sub-apps. The **middleware + Depends hybrid** combines the universality of ASGI scope injection with FastAPI's type-checked dependency system, making it both framework-agnostic at the middleware layer and ergonomic at the handler layer.

## Storage backends for development and production

### Local development: start with SQLite

Five local storage options were evaluated against MutableMapping compatibility, concurrency safety, and debuggability:

| Option | MutableMapping native? | Concurrent safe? | Debuggable? | Recommendation |
|---|---|---|---|---|
| Directory + JSON files | Needs wrapper | File-level locking | ✅ Human-readable | Good for prototyping |
| Python `shelve` | ✅ Built-in | ❌ No concurrent writes | ❌ Binary | Testing only |
| SQLite (single DB, WAL mode) | Needs wrapper | ✅ Multi-reader, single-writer | ✅ Standard tooling | **Best for dev** |

**A single SQLite database with WAL mode** is the recommended development backend. The schema `CREATE TABLE kv (user_id TEXT, app_id TEXT, key TEXT, value BLOB, PRIMARY KEY(user_id, app_id, key))` provides per-user partitioning with standard SQL tooling for debugging. A `SqliteKVStore` wrapper implementing `MutableMapping` is straightforward — roughly 30 lines wrapping `INSERT OR REPLACE`, `SELECT`, `DELETE`, and `SELECT key` queries.

Python's `shelve` module deserves a note: it *already implements* `collections.abc.MutableMapping` with zero wrapping needed, making it attractive for quick prototyping. However, it has disqualifying limitations — no concurrent write safety, pickle-based serialization (arbitrary code execution risk with untrusted data), and platform-dependent `dbm` backends that make files non-portable across operating systems.

⚠️ **Pitfall**: `shelve` returns *copies* of values. Mutating `store["key"]["nested"] = "new"` silently fails unless `writeback=True` is enabled, which loads the entire database into memory.

### Production: SQLite + Litestream replicating to Cloudflare R2

For a single-server deployment with <100 users, the simplest production architecture pairs **SQLite with Litestream** for continuous replication to **Cloudflare R2** for backup:

**Litestream** runs as a background process that streams SQLite WAL changes to S3-compatible storage in near-real-time — no code changes required. Point-in-time recovery works by replaying WAL segments to any timestamp. R2's free tier covers **10 GB storage, 1M writes, and 10M reads per month** with zero egress fees — more than sufficient for a small platform.

Critical SQLite production tuning:

```python
conn.execute("PRAGMA journal_mode=WAL")       # concurrent readers
conn.execute("PRAGMA busy_timeout=5000")       # 5s retry on lock
conn.execute("PRAGMA synchronous=NORMAL")      # safe with WAL
conn.execute("PRAGMA cache_size=-20000")        # ~20MB page cache
```

The total cost for this stack: **$5–16/month** for a VPS (Hetzner, DigitalOcean) plus near-zero storage costs. The MutableMapping abstraction ensures that migrating to PostgreSQL later is a backend swap, not a rewrite.

For **binary blobs** (user uploads, images, PDFs), a two-tier approach is essential: structured key-value data in SQLite, binary files in a separate `BlobStore`. During development, a filesystem-backed `BlobStore` with directory sharding handles files locally. In production, an `S3BlobStore` wrapping boto3 serves files via presigned URLs, offloading bandwidth from the application server. The key architectural rule: **never store large blobs (>1 MB) in the SQLite KV store** — it inflates the database, slows Litestream replication, and complicates backups.

Presigned URLs are the recommended serving strategy for production. The flow: frontend requests an upload URL → backend generates a time-limited presigned PUT URL → browser uploads directly to R2 → backend records metadata in SQLite. For downloads, the backend generates a presigned GET URL. This eliminates the application server as a bandwidth bottleneck.

## Browser-side storage and frontend-backend sync

### The MutableMapping equivalent in TypeScript

Python's `MutableMapping` is synchronous; browser storage is inherently async. The pattern translates to an `AsyncMutableMapping` interface:

```typescript
interface AsyncKVStore<V = unknown> {
  getItem(key: string): Promise<V | null>;
  setItem(key: string, value: V): Promise<void>;
  removeItem(key: string): Promise<void>;
  keys(): Promise<string[]>;
  hasItem(key: string): Promise<boolean>;
  clear(): Promise<void>;
}
```

**`unstorage`** (by UnJS, ~2,600 GitHub stars, 4.3M weekly npm downloads) is the JavaScript/TypeScript library closest to `dol`'s philosophy. It provides a unified async key-value API across **20+ drivers** — localStorage, IndexedDB, Redis, Cloudflare KV, filesystem, HTTP, and more. Its `prefixStorage()` function mirrors `dol`'s key-prefix wrapping, and its mount system mirrors the Mall pattern:

```typescript
import { createStorage, prefixStorage } from 'unstorage';
import indexedDbDriver from 'unstorage/drivers/indexedb';

const storage = createStorage({ driver: indexedDbDriver({ base: 'app:' }) });

// Prefix-scoped sub-store (like dol's wrap_kvs with prefix)
const userStore = prefixStorage(storage, 'user');
await userStore.setItem('settings', { theme: 'dark' }); // stored as 'user:settings'
```

For browser storage specifically: **IndexedDB** is the workhorse (up to ~50% of free disk in Chrome, structured-clonable types, async, worker-accessible). **localStorage** is fine for tiny configuration (<5 MiB, strings only, synchronous). **OPFS** (Origin Private File System) is the newest option, offering high-performance byte-level I/O suitable as a backend for WASM-compiled SQLite.

### Sync strategy: last-write-wins with a background queue

For a single-developer platform with <100 users, **CRDTs are overkill**. Both Figma and Linear — companies operating at massive collaborative-editing scale — rejected pure CRDTs in favor of simpler server-authoritative models. The recommended pattern is **last-write-wins (LWW) with a background sync queue**:

- On write: save to IndexedDB immediately → update UI → enqueue server push
- On app load: pull latest from server `since` last sync timestamp
- On reconnect (`online` event): drain the queue
- Conflict resolution: most recent `updatedAt` timestamp wins

```typescript
class SyncQueue {
  private queue: Array<{ key: string; value: any; updatedAt: number }> = [];

  async enqueue(key: string, value: any) {
    const op = { key, value, updatedAt: Date.now() };
    this.queue.push(op);
    await localStore.setItem('__sync_queue', this.queue);
    this.processQueue(); // fire-and-forget
  }

  private async processQueue() {
    if (!navigator.onLine) return;
    while (this.queue.length > 0) {
      try {
        await fetch('/api/sync', {
          method: 'POST',
          body: JSON.stringify(this.queue[0]),
        });
        this.queue.shift();
      } catch { break; } // retry on next trigger
    }
  }
}
```

For **optimistic UI**, TanStack Query's `onMutate`/`onError`/`onSettled` pattern is the industry standard for React applications — snapshot previous state before mutation, update cache optimistically, rollback on failure. For a framework-agnostic approach, **RxDB** provides local-first reactive storage with built-in bidirectional replication, where "complex parts are in RxDB, not in the backend" — the server just needs simple pull/push HTTP endpoints.

⚠️ **Pitfall**: The Background Sync API (for syncing even after tab close) is only fully supported in Chromium browsers. Use Workbox's `BackgroundSyncPlugin` for production-quality retry logic with graceful degradation.

## Migration, backup, and data export

### Backend migration is nearly trivial with MutableMapping

The `dol` library's `Pipe` composition makes cross-backend migration elegant. The core pattern: wrap both source and destination with appropriate key/value codecs, then call `dst.update(src)`:

```python
from dol import Pipe, KeyCodecs, ValueCodecs

# Source: local pickle files
src_wrap = Pipe(KeyCodecs.suffixed('.pkl'), ValueCodecs.pickle())
src = src_wrap(Files('/data/local/'))

# Destination: S3 with gzipped JSON
dst_wrap = Pipe(
    KeyCodecs.suffixed('.json.gz'),
    ValueCodecs.csv() + ValueCodecs.str_to_bytes() + ValueCodecs.gzip()
)
dst = dst_wrap(s3_backend)

# Migration is one line:
dst.update(src)  # iterates all keys, copies with automatic format conversion
```

However, several gotchas lurk beneath this simplicity:

- **Key format differences**: Local filesystem keys use OS-specific path separators (`\` on Windows, `/` on Linux); S3 uses `/` exclusively. Always normalize keys through a translation layer during migration.
- **S3 key constraints**: Maximum 1,024 bytes UTF-8, and characters like `\`, `{`, `}`, `^`, `%`, `~` can cause issues. Windows paths have a 260-character limit without long-path opt-in.
- **No atomic multi-object operations**: S3 doesn't support transactional writes across multiple keys. Use a two-phase migration: copy all data, verify integrity, then switch the application's backend configuration.
- **Binary/text encoding**: Some backends return `bytes`, others `str`. A `safe_copy` function should handle encoding normalization.

### GDPR-style per-user data export

The MutableMapping interface makes per-user data export straightforward — iterate all keys with the user's prefix, serialize to JSON or ZIP:

```python
def export_user_data(store, user_id):
    """GDPR Article 20: export everything for user X."""
    import zipfile, io, json
    buf = io.BytesIO()
    user_store = mall[user_id]  # pre-scoped via Mall
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        manifest = {'user_id': user_id, 'keys': []}
        for key in user_store:
            value = user_store[key]
            zf.writestr(key, value if isinstance(value, bytes) else json.dumps(value).encode())
            manifest['keys'].append(key)
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))
    return buf.getvalue()
```

For continuous backup, **Litestream** handles SQLite databases automatically. For S3-stored data, enable **bucket versioning** with lifecycle rules to auto-expire old versions. Never use `cp` to back up a live SQLite database — use `connection.backup()` or `VACUUM INTO` for transactionally safe copies.

## Security of key-prefix tenant isolation

Key-prefix-based isolation is the single most security-critical component of this architecture. **If a bug in prefix logic lets user A construct a key that resolves inside user B's namespace, all data isolation fails silently.** Real-world incidents confirm this risk: a cross-tenant vulnerability in AWS AppSync allowed access to resources in other organizations' accounts; GitLab CVE-2023-2825 enabled arbitrary file reads via directory traversal in nested group attachments; PostgreSQL CVE-2024-10976 allowed row-level security bypass through subquery user-ID changes.

The attack surface for key-prefix isolation includes **path traversal** (`../../other_user/data`), **URL-encoded variants** (`%2e%2e%2f`), **null byte injection** (`file%00.txt`), and **double encoding** (`%252e%252e`). Defense-in-depth requires validation at multiple layers:

```python
import re

class TenantIsolatedStore(MutableMapping):
    DANGEROUS = [
        re.compile(r'\.\.'),           # path traversal
        re.compile(r'\\'),             # backslashes
        re.compile(r'\x00'),           # null bytes
        re.compile(r'%2[eEfF]'),       # URL-encoded dots/slashes
        re.compile(r'[\x00-\x1f]'),   # control characters
    ]
    ALLOWED = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\./]{0,500}$')

    def _sanitize_key(self, key):
        for pattern in self.DANGEROUS:
            if pattern.search(key):
                raise ValueError(f"Forbidden key pattern: {key!r}")
        if not self.ALLOWED.match(key):
            raise ValueError(f"Invalid key characters: {key!r}")
        return '/'.join(p for p in key.split('/') if p)  # normalize

    def _full_key(self, key):
        full = self._prefix + self._sanitize_key(key)
        assert full.startswith(self._prefix)  # double-check after construction
        return full
```

Five non-negotiable security rules for this architecture:

- **Derive tenant IDs from the authenticated session, never from client input.** The ASGI auth middleware must be the sole source of `user_id`.
- **Validate and sanitize all keys** against a strict allowlist pattern. Block `..`, `\`, null bytes, control characters, and URL-encoded variants.
- **Double-verify the prefix** after key construction — confirm the full key still starts with the expected prefix.
- **Log and alert on violations** — any rejected key pattern should trigger an audit event for security monitoring.
- **Test traversal attacks in CI** — maintain a suite of known bypass payloads (null bytes, double encoding, Unicode normalization) and assert they all raise `ValueError`.

For S3 production backends, supplement application-level isolation with **IAM policies using ABAC** (Attribute-Based Access Control). AWS supports policies where `"Resource": "arn:aws:s3:::bucket/${aws:PrincipalTag/TenantID}/*"` restricts access to a tenant's prefix at the infrastructure level, providing a second layer of defense independent of application code.

## The Python and JavaScript storage abstraction landscape

### Python: dol stands alone for MutableMapping-native access

Among Python storage abstraction libraries, **`dol` is the only one that natively implements `collections.abc.MutableMapping`** as its core interface. Every store — filesystem, S3 (via `s3dol`), MongoDB (via `mongodol`), Azure (via `azuredol`) — presents the same `store[key] = value` API. Its `wrap_kvs` function, `KeyCodecs`, `ValueCodecs`, and `Pipe` composition make it uniquely suited for the described architecture.

**`fsspec`** (~1,300 GitHub stars, used by Dask, pandas, PyArrow) is the most mature filesystem abstraction, supporting 20+ backends via protocol-based dispatch (`fsspec.filesystem('s3')`). Its `FSMap` class provides a `MutableMapping` wrapper over any filesystem, making it a strong complement to `dol`:

```python
import fsspec

# FSMap: MutableMapping over any fsspec filesystem
m = fsspec.get_mapper('s3://my-bucket/user_42/')
m['settings.json'] = b'{"theme": "dark"}'
data = m['settings.json']  # returns bytes
```

**`cloudpathlib`** provides `pathlib.Path`-like access to S3/GCS/Azure. **`smart_open`** is a drop-in `open()` replacement for streaming I/O across cloud storage. Neither is dict-like, but both complement `dol` for specific use cases (path manipulation and streaming, respectively). **`django-storages`** dominates the Django ecosystem but is framework-coupled and file-oriented, not key-value.

### JavaScript/TypeScript: unstorage mirrors dol's philosophy

On the frontend, **`unstorage`** is the clear `dol` counterpart — unified async key-value API, 20+ drivers, Unix-style mounting for namespace composition, TypeScript-first. **`idb-keyval`** (~500 bytes) is ideal for minimal IndexedDB key-value needs. **`localForage`** (~25,700 stars) provides backward-compatible async storage with IndexedDB/WebSQL/localStorage fallback. **`RxDB`** (~22,000 stars) is the most complete solution for reactive local-first databases with bidirectional replication.

No widely-known library called "zodal" exists. The closest concept would be using Zod schemas for runtime type validation on top of `unstorage` or `idb-keyval` — a pattern that can be composed manually.

## Conclusion

The architecture described here — `dol`-based `MutableMapping` stores, key-prefix tenant isolation, ASGI middleware injection, and backend swappability — is not just theoretically clean but practically achievable with surprisingly little code. The **Mall pattern** (mapping of mappings) is the conceptual keystone: it models the `stores[user_id][app_id][key]` hierarchy naturally, composes with `dol`'s `wrap_kvs` for key transformation, and maps directly onto filesystem directories, S3 prefixes, and Redis key namespaces.

The strongest insight from this research is that **the `MutableMapping` interface makes backend migration nearly trivial in the common case** — `dst.update(src)` copies an entire store across backends — but **key-format differences between backends are the real migration hazard**, not the data transfer itself. Plan for key normalization from day one.

For immediate implementation: use SQLite + Litestream → R2 for the backend, `unstorage` with IndexedDB for the browser, last-write-wins with a sync queue for frontend-backend coordination, and `TenantIsolatedStore` with strict key validation for security. This stack costs under $20/month, handles <100 users comfortably on a single server, and scales to PostgreSQL or managed services through the `MutableMapping` abstraction when needed. The most dangerous pattern that "seems clean but isn't" is naïve key-prefix isolation without input sanitization — a single `..` in a key can breach all tenant boundaries.