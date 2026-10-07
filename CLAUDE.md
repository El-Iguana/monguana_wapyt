# Monguana — pytincture + wapyt rewrite

Browser-based MongoDB manager. A rewrite of
El-Iguana/monguana (a **private** repo; FastAPI + Motor +
~2,800 lines of vanilla JS with AG Grid and Monaco from CDNs) onto
**pytincture** (Python in the browser via Pyodide) and **wapyt** (the
DHTMLX-free widgetset at `../wa_pytincture_widgetset`). Started 2026-09-25.

It follows **IguanaXterm** (`../iguanaxterm_wapyt`) closely: same service
wiring, auth, login-page rewrite, container pattern. Read *its* CLAUDE.md for
the framework traps; the ones that bite here are summarised below, not
re-explained.

## Related repos

Siblings under `~/Development/Pytinc/`, each with its own `CLAUDE.md`:

- `pytincture/` — the framework, pinned to tag `v1.0.0rc13` in `pyproject.toml` *and*
  `requirements.txt` (keep them in step).
- `wa_pytincture_widgetset/` — **wapyt**. This app added `TreeAction(kinds=…)`
  (context-menu entries filtered by `node.data["kind"]`) so a server, a
  database and a collection get different menus.
- `iguanaxterm_wapyt/` — the sibling app this one is modelled on.

The original Monguana is not checked out locally; clone it to compare.

## Layout

```
service.py                  # ASGI entrypoint: config, hooks, routes, static mount
appcode/                    # pytincture modules_path
  monguana.py               #   browser UI (Pyodide). APP_ENTRYPOINT lives here.
  services/
    auth.py                 #   authenticator + BFF policy hook (from IguanaXterm)
    login_page.py           #   "Email" -> "Username" rewrite, verified at startup
    db.py                   #   SQLite: users, connections; Fernet; bcrypt
    mql.py                  #   shell-syntax parser + query safety   (server only)
    docfmt.py               #   display helpers                      (BOTH sides)
    querybuilder.py         #   visual query builder -> filter text  (BOTH sides)
    filter_rows.py          #   filter text -> builder rows          (server only)
    pipeline_text.py        #   pipeline text <-> stage cards         (BOTH sides)
    mongo_pool.py           #   client pool — plain module, NOT a BFF
    backends/               #   what a profile points at (phase 39) — plain, NOT BFFs
      __init__.py           #     registry: get / for_profile / register
      mongodb.py            #     the built-in MongoDB backend (client options, test)
      tinymongo.py          #     tinymongo stores, confined to MONGUANA_TINYMONGO_ROOT
    connection_service.py   #   BFF: profiles, test, connect/disconnect
    mongo_service.py        #   BFF: databases, collections, documents, indexes…
    user_service.py         #   BFF: accounts (from IguanaXterm)
    ui_state_service.py     #   BFF: per-user UI state (column layouts)
    jobs.py                 #   background jobs + progress — plain module, NOT a BFF
    job_service.py          #   BFF: start a dump; status/cancel any job
    transfer.py             #   plain routes under /mg: export, dump, restore
  static/                   #   artwork, same-origin
  vendor/codemirror/        #   the editor bundle (built, committed), served at /vendor
tools/codemirror/           # its recipe: pinned package.json, entry.js, build.sh
  wapyt-99.99.99-*.whl      #   dev wheel the BROWSER installs (git-ignored)
tests/                      # unit tests; test_live.py needs a MongoDB
tests/smoke/                # ui_smoke.py (Playwright) + seed_shop.py
tools/windows/              # native Windows install: launcher, build.py, Inno Setup (phase 38)
docs/BACKEND_PLUGINS.md     # writing a backend plugin (phase 39)
examples/monguana-sandbox-backend/  # a working backend plugin; tests/test_plugins.py installs it
```

## Running

```bash
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode   # after ANY wapyt asset edit
uv run python service.py                                  # http://127.0.0.2:8766/monguana
uv run --group dev pytest -q
scripts/podman-run.sh      # dev container "monguana": host network, local wapyt
```

Installing (users): **INSTALL.md** — `compose.yaml` on any OS with Docker or
Podman, `compose.host-network.yaml` on Linux. `manage.py` is the admin CLI
(`health`, `probe HOST PORT`, `users`, `reset-password`, `backends`).

### How the image is built (2026-09-26)

The `Containerfile` builds from a plain clone: a stage clones wapyt at the
pinned `WAPYT_REF` and builds **both** wheels — 0.1.0 for the server's Python
(non-editable, for widgetset discovery) and 99.99.99 with a regenerated asset
manifest for the browser (what `dev_wheel.sh` does). Before this, a fresh
clone could not be built: the browser wheel is git-ignored and the build
needed a `vendor-wheels/` folder the dev script made. **Bump `WAPYT_REF`**
when Monguana starts needing newer wapyt.

`scripts/podman-run.sh` replaces that source stage with the sibling checkout
(`--build-context wapyt-src=…`, a filtered copy), so the dev container carries
unmerged wapyt work.

Traps found writing INSTALL.md, all measured:

- **From a bridge network, `host.*.internal` does not reach the host's
  127.0.0.1** (rootless Podman here; Docker Engine behaves the same). A
  MongoDB bound to loopback — the usual install — is *connection refused*.
  Hence the bundled `--profile mongo`, `compose.host-network.yaml`, and
  `manage.py probe` to tell the cases apart.
- **`MONGUANA_DATA_DIR` must not be in `.env`.** `--env-file` overrides the
  image's `/data`, and a plain `docker run` then kept the database outside the
  volume. Compose masked it (its `environment:` wins).
- **The healthcheck sends the canonical host name.** Behind a proxy the app
  accepts only its public name, so a check against 127.0.0.1 would report a
  working deployment unhealthy. Podman ignores a Containerfile `HEALTHCHECK`
  in OCI images anyway; it lives in the compose files.
- **`docker build` wants `-f Containerfile`**; it only looks for `Dockerfile`.
- **SELinux needs `:z`** on a bind mount the container writes to (the tar
  backup): *Permission denied* otherwise. Only on a dedicated folder.
- **`localhost` answers `400 Invalid host header`** — the troubleshooting
  entry quotes it.

After changing wapyt's *Python* wrappers: `uv sync --reinstall-package wapyt`.

### Native Windows install (ROADMAP phase 38, 2026-09-29)

`tools/windows/build.py` assembles `build/windows/bundle` — the official
**embeddable CPython** (pinned version + sha256), Windows wheels, the app and
the browser wapyt wheel — and with `--installer` runs Inno Setup
(`monguana.iss`) into `dist/`. The bundle builds on Linux too; only ISCC
needs Windows, so the `windows` workflow builds, silently installs, runs
`--check`, start/stop and uninstall on `windows-latest`. `launcher.py`
(installed as `app\monguana_launcher.py`) runs the server as a child on
127.0.0.2, port 8766 or the next free one; data in `%LOCALAPPDATA%\Monguana`.
Tests: `tests/test_launcher.py`; by hand on Linux with
`MONGUANA_DATA_DIR=… uv run python build/windows/bundle/app/monguana_launcher.py --check`.

Traps, found building it:

- **pytincture could not read a single contained file on Windows**, so no
  BFF call could work. `safe_paths._open_relative_nofollow` opens the root
  *directory* with `os.open()` to walk the path through directory descriptors;
  Windows refuses with `PermissionError`, before the function's fallback (it
  only catches `NotImplementedError`/`TypeError`) can apply.
  Fixed upstream in pytincture#377 (in 1.0.0rc13), so the
  `pytincture_compat.py` patch that took the fallback up front is gone.
- **Tests read source with `read_text()` and no encoding**, which is cp1252
  on Windows: always pass `encoding="utf-8"`.
- **pip evaluates environment markers for the build machine, even with
  `--platform`**: it tried to install `uvloop` for Windows. `uv pip install
  --python-platform x86_64-pc-windows-msvc` evaluates them for the target.
  Dependencies come from `uv export` of `uv.lock`; resolving the loose
  requirements afresh conflicted with pytincture's pins.
- **The embeddable build's `python313._pth` replaces `sys.path`**: it lists
  the stdlib zip, `Lib\site-packages` and `..\app`, and nothing else.
- **Windows' `mimetypes` reads the registry**, where `.js` is often
  `text/plain`; `service.register_mime_types()` pins the types the browser is
  strict about (scripts, WebAssembly). CI checks a `.js` comes back right.
- **`os.open` is text mode on Windows** without `O_BINARY` (the key files).
  The launcher sets `PYTHONUTF8=1` for the server, so `open()`/`read_text()`
  without an encoding do not fall back to the ANSI code page.
- **pythonw.exe has no stdout.** The launcher runs under it but starts the
  server with `python.exe` + `CREATE_NO_WINDOW`, so the log gets written.
- **The first run's admin password is the default, `change_me`** (2026-09-29;
  it was a random one shown once, which people lost). The launcher removes
  any inherited `MONGUANA_ADMIN_PASS`/`_USER` so the default applies, and its
  first-run box says how to sign in. See *Default password* below.
- **The wapyt wheel ships its `tests/`, `build/` and `scripts/`** as top-level
  packages (setuptools auto-discovery); build.py deletes them from
  site-packages. Worth fixing in wapyt's packaging.
- The uninstaller runs `--stop` first (a running server holds files open),
  and so does an upgrade (`PrepareToInstall`). A silent uninstall keeps the
  data; an interactive one asks, defaulting to keep.

### Test MongoDB

A throwaway `monguana-mongotest` container (mongo:7) on **127.0.0.1:27018**,
root password `p@ss:w/rd` — the `@ : /` are deliberate, see *Credentials*
below.

```bash
podman run -d --name monguana-mongotest -p 127.0.0.1:27018:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=root -e 'MONGO_INITDB_ROOT_PASSWORD=p@ss:w/rd' \
  docker.io/library/mongo:7
MONGUANA_TEST_MONGO='127.0.0.1:27018:root:p@ss:w/rd' uv run --group dev pytest tests/test_live.py
uv run python tests/smoke/seed_shop.py        # the `shop` sample: 137 orders, a view
MONGUANA_PASS=… python3 tests/smoke/ui_smoke.py
```

The smoke test logs in, creates a profile through the editor (and its Test
button), then filters, sorts, pages, switches views, edits, runs an
updateMany through its preview, aggregates, uses Fields, checks the per-kind
context menus, exports CSV, round-trips a dump through Restore, and opens the
dashboard (kills a slow `$where` it starts itself, through pymongo) and the
query profiler (on, a recorded query reopened, a profile filter set from an
example and removed, off, cleared).
It expects an admin whose password is not flagged for change (the reminder
dialog blocks it): `manage.py reset-password admin` first on a fresh data
dir. Its About step expects the release check on. It fails
on any browser console error. Screenshots land in `tests/smoke/` (ignored).

## Things that will bite (inherited — details in IguanaXterm's CLAUDE.md)

- **`APP_ENTRYPOINT = "Monguana"` is mandatory**: pytincture's MainWindow
  detection only knows dhxpyt, so without it startup is a 422.
- **A BFF module is re-executed on every call.** Anything that must persist
  lives in a plain module: the client pool is in `mongo_pool.py`, and BFF
  modules import it *inside functions*. `tests/test_bff_state.py` loads the
  BFF modules with pytincture's own loader and fails on module-level
  containers or calls in them — including a harmless-looking
  `re.compile(...)`, which is why `mongo_service._DB_NAME_BAD` is a string.
- **Build UI in `load_ui()`, never `__init__`.**
- **BFF calls are `await X().method_async(...)`.** Every export is a sync
  `def`; pytincture runs it on a worker thread, which is what makes blocking
  pymongo correct. (Motor, which the original used, has no place here.)
- **Hooks by dotted path** in `PytinctureConfig(environment=…)`, and
  `AUTH_SESSION_CLAIM_KEYS=user_id,is_admin,username` or every BFF call is a
  silent 403.
- **Absolute imports only** in `services/` (`from services.x import …`).
  In `monguana.py` too, import *names* from a module (`from services.about
  import VERSION`), never the module (`from services import about`): the
  browser package follows only the first form, and the second boots to a
  blank page with a truncated `import_module` traceback.
- **wapyt installed non-editable** or the app boots with no widgets.
- **pytincture will not serve authenticated plain HTTP** except on a literal
  loopback IP: use `http://127.0.0.2:8766`, never `localhost`.
- **Cookies are namespaced** (`COOKIE_NAMESPACE = "monguana"` in `service.py`,
  pytincture 1.0.0rc13 `cookie_namespace`): `monguana-dev-session` /
  `monguana-dev-csrf` over loopback HTTP, `__Host-monguana-*` over HTTPS.
  Before rc13 every pytincture app used `pytincture-*`, and next to IguanaXterm
  on 127.0.0.1 each sign-in clobbered the other's cookie. App routes read
  `request.session`, never a cookie by name. Changing the namespace signs
  everyone out once.
- **Monguana still lives on 127.0.0.2** (`MONGUANA_HOST`), the earlier fix for
  the same clash, kept so bookmarks and the Windows launcher keep working.
  Moving to 127.0.0.1 is now possible but needs INSTALL/README/launcher
  changes. macOS needs `ifconfig lo0 alias 127.0.0.2` or
  `MONGUANA_HOST=127.0.0.1`.
- **2 MiB body cap on every route.** `BodyLimitExceptRestore` lifts it for
  `POST /mg/restore/<id>` only; that route authenticates and checks CSRF
  before reading a byte and enforces `MONGUANA_MAX_RESTORE_BYTES` while
  streaming.
- **No CDN, ever.** pytincture's CSP blocks it. This is why the original's
  Monaco and AG Grid are not here (see *Not built yet*).
- **`JsNull` is not `None`.** Test DOM lookups for truthiness.
- **The page is `/monguana/`** (pytincture 1.0.0rc12+): `/monguana` redirects
  there, so pytincture's service worker (scope `/monguana/`) now controls the
  page. Before rc12 it never did and the loader waited 5 s for it on every
  load, which `pytincture_compat` worked around by turning the worker off;
  that patch is gone. Relative URLs now resolve under `/monguana/`: keep app
  URLs absolute (`/static/…`, `/mg/…`, `/vendor/…`). `APP_FAVICON` is a
  file path pytincture turns into an absolute URL, so it is unaffected.
- **`dataset["for"]` does not work through Pyodide** — use
  `getAttribute("data-for")`.

## Design decisions (and the original's bugs they fix)

### Queries are text until the server parses them — `mql.py`

The browser never builds BSON; it sends the text of the filter, sort,
projection, update or pipeline, and `mql.parse` turns it into Python/BSON on
the server. It reads mongo shell syntax (unquoted and dotted keys, single
quotes, trailing commas, comments, `ObjectId()`, `ISODate()`, `new Date()`,
`NumberLong/Int/Decimal()`, `UUID()`, `Timestamp()`, `/regex/flags`) **and**
Extended JSON, bottom-up through `json_util.object_hook`, so anything the
viewer displays pastes back with its types. Errors carry line and column.

The original `json.loads`-ed everything, so an ObjectId or a date could not be
queried at all — a filter on one compared against a string and silently
matched nothing.

### Documents travel as relaxed Extended JSON, ids as canonical

`mql.to_display` sends relaxed EJSON (`{"$oid": …}`, `{"$date": …}`,
`{"$numberDecimal": …}`), which `docfmt` renders the shell way. Each row also
carries its `_id` as **canonical** EJSON (`mql.encode_id`), handed back
verbatim for edit/delete, so string, numeric, UUID and compound ids all work.
The original ran `ObjectId(doc_id)` on whatever came back and could not touch
any other kind of `_id`.

Int64 is tagged even when small (`{"$numberLong": "5"}`, `mql._tag_int64`):
relaxed EJSON writes it as a plain number, which parsed back — and was saved —
as Int32 (fixed in ROADMAP phase 37). The table and tree still show `5`.

**Types are visible (2026-09-29, Studio 3T style).** Each table cell carries
its BSON type icon (`_TYPE_ICONS`, the tree's map, via wapyt's
`ColumnConfig(icon_by=…)`; tooltip names the type, CSS tints it). The document
editor, Clone and the read-only viewer show `docfmt.to_shell`: mongo shell
syntax with every type spelled out — `ObjectId("…")`, `ISODate("…")`,
`NumberInt(1)`, `NumberLong("5")`, `NumberDecimal("…")`, `UUID("…")`,
`Timestamp(t, i)`, `/re/i`, and `1.0` for a double (a bare `5` would come back
Int32). What has no shell spelling (binary, code, far-out dates, NaN, a regex
with a `/`) stays Extended JSON; `mql.parse` reads both.
`tests/test_shell_format.py` round-trips every type. The JSON view and Copy as
JSON stay Extended JSON, since they are meant to be JSON.

UUIDs are "standard" (subtype 4) everywhere — in `mql`'s JSON options and on
every `MongoClient` (`uuidRepresentation="standard"`). pymongo's default
refuses to encode a native `uuid.UUID` at all. `to_display` rewrites them as
`{"$uuid": "…"}`, because json_util writes base64 `$binary`, which is
unreadable in the editor.

### Edits replace the whole document, loaded fresh

The editor fetches the full document with `get_document` (a projection may
have hidden fields — saving the projected copy would drop them) and saves
with `replace_one`. The original `$set` the edited body, so a field deleted in
the editor stayed in the database. Changing `_id` is refused; Clone exists.

### Safety walks arrays

`check_query` and `check_pipeline` visit every key at any depth, arrays
included. The original recursed into objects only, so `{$or: [{$where: …}]}`
passed its `$where` block. Pipelines: top-level stage allowlist, and
`$out`/`$merge`/server JS refused anywhere, including `$lookup`/`$facet`
sub-pipelines. Updates must be operator documents or an update pipeline of
`$set/$unset/$project/...` stages. Server-side time limits: 30 s per query,
5 s for the count that accompanies a page (past it, an unfiltered view falls
back to `estimated_document_count` and a filtered one reports the total as
unknown — the UI offers Count).

### Credentials

`backends.mongodb.client_options` passes `username`/`password` as keyword
arguments. The original formatted them into `mongodb://user:pass@host`, so a
password with `@`, `:` or `/` (the test container's has all three) produced a
different URI entirely. A profile may instead hold a full connection string
(`uri`, encrypted like the password), which then wins. Secrets are
three-state in `ConnectionService.save`: omitted = unchanged, `""` = cleared.
In the editor, a single space in *Connection string* clears a stored one.

### The pool

One client per `(user_id, connection_id)`, built by the profile's backend
(`connections.backend`, `services/backends/`; a `MongoClient` for MongoDB). It
re-reads the profile on each call (one indexed SQLite lookup) and rebuilds the
client when the fingerprint of backend + connect options changes, so an edited
profile applies at once. Idle clients close after 15 minutes. A profile whose
backend is not installed fails with `BackendUnavailable`, shown as an error.
A backend's own settings live in `connections.options` (JSON, Fernet-encrypted,
kept by `save` when omitted).

### Backend capabilities (phase 39)

`backends.CAPABILITIES` lists what a backend may lack; `MongoService._can` /
`_require` check the backend behind the client (`pool.open` returns both).
Without a capability the service either falls back (stats: count only, sizes
`None`; `$sample`: first documents; no `maxTimeMS` — use `_time_limit(...)`,
never a literal keyword; collMod changes become rebuilds) or refuses with
"<Label> connections cannot …". `ConnectionService.list` sends each profile's
`capabilities`; the UI's `_caps(conn_id)` reads them, tree nodes carry them as
`data["flags"]`, and `TreeAction(requires=[…])` (wapyt) hides entries.
**A new server feature needs a capability** if another backend might lack it.
`test_live.py` checks every fallback through a backend declaring none.

### tinymongo (phase 39, step 3)

In-process (tinymongo has no wire protocol), and **only when
`MONGUANA_TINYMONGO_ROOT` is set**: container `/tinymongo` (volume
`monguana-tinymongo`), Windows launcher `%LOCALAPPDATA%\Monguana\tinymongo`,
unset for `python service.py`. `resolve_folder` confines a profile's relative
folder to the root on save *and* on every open. Traps, all measured:

- **Touching a database creates its file on the JSON engine** — even
  `client.admin.command("ping")`. Test lists databases instead; never touch
  `admin` for a tinymongo client.
- No `create_collection`: collections appear on first insert. The backend's
  `create_collection` hook inserts and deletes a placeholder; both engines
  keep the empty collection.
- `aggregate()` refuses `maxTimeMS` (`find`/`count_documents` accept it), so
  no `time_limits`. Descending index keys, update pipelines and collations are
  refused with tinymongo's own messages.
- `get_collection(codec_options=RawBSONDocument)` silently returns plain
  dicts, hence the `raw_bson` capability (step 5): without it, dump encodes
  decoded documents and restore/copy decode with `transfer.decoded_codec()`
  (tz-aware UTC, standard UUIDs).
- Int64 comes back as `int`: a small `NumberLong` returns from a trip through
  tinymongo as Int32 (`test_copy_mongodb_to_tinymongo_and_back` pins it).
- The editor's backend fields are `opt_<id>` in the form; `_save` maps the
  service's errors (keyed by the backend's ids) back onto them.

`tests/test_tinymongo.py` needs no MongoDB and runs on every push. It covers
DuckDB and Parquet too when they are installed (`uv sync --extra duckdb
--extra parquet`); in the image, the `EXTRA_PACKAGES` build argument
(`MONGUANA_EXTRA_PACKAGES` in `.env`) installs them, and plugins.

### Backend plugins (phase 39, step 4)

Entry-point group `monguana.backends`; guide in docs/BACKEND_PLUGINS.md,
working example in `examples/monguana-sandbox-backend`. `_load_builtins`
loads built-ins then plugins **once, under `_load_lock`** (an RLock plus a
`_loading` flag, so a plugin importing the registry mid-load does not
deadlock). Rejected plugins land in `backends.load_errors`;
`python manage.py backends` prints them and exits 1. Tests that need a fresh
registry must reset `_registry`, `sources`, `load_errors` and
`_builtins_loaded` together (see `tests/test_plugins.py`).

### Plain routes for bytes — `transfer.py`, under `/mg`

Export (`GET /mg/export/<id>`), restore (`POST /mg/restore/<id>`, body = the
ZIP itself) and a finished job's download (`GET /mg/jobs/<job>/download`) are
plain FastAPI routes that read pytincture's session cookie, as IguanaXterm's
transfers do. The BFF is JSON and capped at 2 MiB, so bytes through it would be
base64 in Pyodide's heap. Export streams every match (not just the page) up to
1M (JSON) / 100k (CSV); the UI checks the filter with a count first, because a
bad one would otherwise surface as a failed download with no message. Dumps
read raw BSON (`RawBSONDocument`) so documents are never decoded and
re-encoded. Note `insert_many` returns an empty `inserted_ids` for
`RawBSONDocument`s — restore counts the batch instead.

### Dump and restore are jobs (ROADMAP phase 36; copy, phase 39)

Since phase 39 step 5 they work on every backend: `write_dump`,
`restore_archive` and `copy_collections` take the backend's capabilities.
`raw_bson` keeps documents as `RawBSONDocument` end to end (copy only when
*both* sides have it); `bulk_write` batches Merge's upserts, else one
`replace_one` each. `_Writer` is the one place documents are written (skip,
drop, merge) and creates indexes **one at a time**, so an index the target
refuses is reported and the rest still land. `JobService.start_copy` copies a
database or a collection between any two of the caller's connections, and
refuses a copy onto itself. The UI's **Copy … to…** dialog starts it and
follows it in the same job console, refreshing the target at the end.

`services/jobs.py` is the registry — a **plain module**, since a BFF module is
re-executed per call and would hand each poll an empty one. A job runs its work
on its own thread with a `Progress` (items with done/total, a log, `partial`
results, `check()` for cooperative cancel); the page polls
`JobService.status(job, log_from)` every 0.5 s and draws the console
(`_job_console`). Not a `@bff_stream`: pytincture caps those at 300 s.

- **Dump:** `JobService.start_dump` → `transfer.write_dump` writes the ZIP to
  `DATA_DIR/jobs/`, per-collection totals from `estimated_document_count` and
  corrected to the real count at the end. The console downloads it from
  `/mg/jobs/<id>/download` when done; "Download again" works for an hour.
- **Restore:** the upload (XHR, for upload progress) lands in
  `DATA_DIR/jobs/` and `POST /mg/restore` answers with the job id at once.
  Progress is bytes of each `.bson` member read (through `_Counting`); what was
  restored before a cancel is kept as the result.
- **The bars are wapyt's** (wa_pytincture_widgetset#28). Each console item is
  `progress_html(..., text=amount, state=…)` rebuilt per poll (job state
  running/done/failed/cancelled → active/done/error/paused), the restore
  upload is a live compact `ProgressBar`, and the dashboard tile meters are
  `progress_html` with the label visually hidden (the tile headline already
  shows the value) and `--wapyt-progress-fill` set to `--mg-accent`.
- Finished jobs and their files are dropped after an hour (`purge`), leftovers
  from a previous process at startup (`clear_leftovers`), a cancelled or
  failed dump's partial file at once. At most 3 running jobs per user.
- **`hidden` does not hide an `.mg-btn`** (its `display:inline-flex` wins):
  the console's Cancel stayed on screen after the job ended until
  `.mg-btn[hidden]` got its own rule. IguanaXterm hit the same trap.

### The UI

- **Sidebar tree** loads lazily on expand (`on_toggle`); node ids are
  deterministic (`c:<id>`, `d:<id>:<db>`, `k:<id>:<db>:<coll>`, names
  percent-encoded) so the Tree keeps expansion across `set_items`. Unloaded
  branches carry a placeholder child, since the Tree decides "branch" by
  having items. Collections are **leaves** (double-click opens; the Tree only
  emits `activate` for leaves), so indexes live in a dialog rather than as
  child nodes as in the original.
- **Tabs** are `mg-view` blocks with ids `v<n>-…`; one delegated `click`,
  `change` and `keydown` listener on `document` routes by `data-mg`/`data-role`
  and the enclosing `.mg-view`'s `data-tab`.
- **Table sorting is the server's.** A header click writes `{field: ±1}` to the
  Sort box and re-queries. Every column's `sort_by` points at a hidden rank
  key (±row index), so the DataTable's own local sort reproduces the server's
  order instead of re-ordering a page of mixed BSON types by text.
  `_sync_table_sort` moves the caret to match a sort typed by hand, with a
  guard flag because `DataTable.sort()` emits `sort` itself.
- **The header is a wapyt `Toolbar`** (wa_pytincture_widgetset#24), built by
  `_toolbar_config()` from `_TOOLBAR_BUTTONS` and held as `self.toolbar`;
  `on_click` routes ids to `_on_toolbar` (the `update` notice opens About).
  The signed-in user is the `user` text item and the release notice the
  hidden `update` button, both set through `set_text` / `set_hidden`, not DOM
  lookups. Compact mode drops labels to icons when the window is narrow. A
  small `_CSS` rule keeps it in the app's panel colour.
- **Toasts and dialogs come from `wapyt.message`** (wa_pytincture_widgetset#22):
  `self._toast(text, kind=...)` delegates to `message.toast` with `info` /
  `success` / `warning` / `error`, and every confirm or prompt is
  `await message.confirm(...)` / `await message.prompt(...)` with a title, an
  action-named OK button and `danger=True` when destructive. Never
  `js.confirm` / `js.prompt`: they block the page and can't be styled. The
  method that asks must be `async`; `_dash_op_action` stays a sync handler by
  asking inside the `_kill()` coroutine it spawns.
- **Modals are closed, not hidden.** wapyt's `hide()` leaves the overlay in the
  DOM; `close()` removes it. Every modal here is built with
  `ModalConfig(dispose_on_close=True)` (wapyt, 2026-09-25), so ×, Escape and a
  backdrop click remove it too. Their font comes from wapyt's
  `--wapyt-font-family` (wa_pytincture_widgetset#21); there is no app rule.
- **Per-user settings and view state** go through `UiStateService`:
  `settings` (`show_system`: `system.*` collections, hidden by default,
  toggled from a server's or database's menu), `columns:<conn>:<db>:<coll>`
  (phase 35) and `view:<conn>:<db>:<coll>` (the Table/JSON/Tree choice).

### Dashboard and query profiler (added 2026-10-01)

Two tab kinds besides collection views, held in `self._dashes` (not
`_views`) and routed by their `.mg-dash` root, as views are by `.mg-view`:
`_dash_action` (clicks), `_dash_change` (changes). Closing the tab runs
`_discard_dash`, which clears its `setInterval`.

- **Dashboard** (`_open_dashboard`; server menu, toolbar): one per
  connection. Polls `MongoService.server_status` every `DASH_POLL_SECONDS`
  (5) **only while auto is on, its tab is in front and the page is visible**.
  Counters are cumulative; the page keeps `DASH_HISTORY` samples and turns
  deltas over `uptimeMillis` into rates (a lower uptime = restart, history
  cleared). Sparklines are hand-made inline SVG (`_sparkline`): one series,
  a `<title>` per sample for the tooltip. `database_stats` (dbStats + profile
  level per database, first `DASHBOARD_MAX_DATABASES`) runs on open, on
  Refresh and after a profiler change — not every poll.
- **Profiler** (`_open_profiler`; database menu, a dashboard row): one per
  database. `profiler_summary` groups `system.profile` by ns + op +
  `planCacheShapeHash`/`queryHash`; `profiler_entries` is newest first
  (`$natural: -1`). An entry's `open` is how to re-run it in a view (find:
  one-line filter/sort/projection via `to_shell`; aggregate: the pipeline);
  `_open_profiled` sets the boxes right after `_open_view`, before its
  spawned first query runs.
- Capabilities **`server_status`** (serverStatus, dbStats, `$currentOp`;
  without it the dashboard shows databases with collection counts only) and
  **`profiler`** (hidden without it).

Traps, all measured on 7.0:

- **`serverStatus` with `metrics: 0` also drops `mem`**, even with `mem: 1`.
  Only `repl` and `locks` are excluded.
- **Reading `system.profile` is itself profiled** at level 2, so without a
  collection filter `_profile_query` excludes `<db>.system.profile`.
- **`slowms` and `sampleRate` are the mongod's, not the database's** (they
  also set what the log calls slow). `set_profiler` leaves them alone when
  passed negative — the dashboard's on/off menu does — and the profiler tab
  says so in its tooltips.
- **`system.profile` can only be dropped while profiling is off**:
  `profiler_clear` switches it off, drops, and restores the level and settings.
- `$currentOp` lists itself; `_current_ops` drops the entry whose pipeline
  starts with `$currentOp`. It also drops **every driver's monitor** (an
  awaitable `hello` with `topologyVersion`, which looks like a 10 s command
  per client — a kill test once killed that instead of the query) and the
  server's own threads (`op: "none"`, no client: JournalFlusher, Checkpointer).
- **Kill** (`kill_op`, the running-operations table's menu, confirmed first)
  sends `killOp`; opids are ints on a mongod, `"shard:n"` strings on a mongos.
  MongoDB answers ok even for an op that already ended, so the page re-reads
  the list a second later. The op stops at its next interrupt check; its
  client gets code 11601. `serverStatus` needs `clusterMonitor` and
  `$currentOp` with `allUsers` needs `inprog`: each half fails on its own
  and the page says which.
- `find()` takes `max_time_ms`, not `maxTimeMS`: `_time_limit(ms,
  "max_time_ms")` there.
- The local database cannot be profiled; refused before the server is asked.
- **The profile filter** (`set_profile_filter`, the profiler's **Filter…**
  dialog) is **per database**, set with `profile: -1` so the level stays,
  removed with `filter: "unset"` (blank text here). While set, it replaces
  `slowms`/`sampleRate` for level 1 *and* the slow-query log; level 2 still
  records everything. The tab relabels Slow only as **Filtered** and disables
  the threshold, sample and Apply. MongoDB **stores it normalized**
  (`{op: "query"}` reads back as `{$and: [{op: {$eq: "query"}}]}`), so the
  dialog shows that form. It is parsed by `mql` (shell syntax, braces
  optional) and `check_query`d; MongoDB refuses `$where` there anyway.
  Completions come from `_PROFILE_FIELDS` (a profiler entry's fields).

### Index CRUD (added 2026-09-25)

The Indexes dialog lists (with sizes), creates, edits, hides/unhides and
drops. `MongoService.update_index` takes a whole definition, diffs it against
the live index and picks a strategy; `dry_run=True` returns the plan, which
the UI shows before **Apply**:

| strategy | when | how |
|---|---|---|
| `in-place` | only TTL set/changed, hidden toggled, unique turned **on** | `collMod`; unique is `prepareUnique` then `unique`, and on duplicates (code 359) the preparation is undone and the error names up to 5 colliding `_id`s |
| `build-then-drop` | anything else, under a **new name** | build new, drop old: never without an index. Falls back to drop-then-build if MongoDB refuses two indexes on the same keys (codes 85/86) |
| `drop-then-build` | anything else, **same name** (indexes cannot be renamed) | drop, build; if the build fails the old index is recreated from its exact spec |

Rebuild triggers: keys, name, sparse, partial filter, other options, TTL
removed, unique turned **off** (in place only from MongoDB 7.1; the test
server is 7.0). `_id_` is refused throughout.

Traps found building it, both pinned by `tests/test_live.py`:

- **A text index comes back as `{_fts: "text", _ftsx: 1}`** plus
  server-filled `weights`, `default_language`, `language_override`,
  `textIndexVersion`. `_index_view` turns it back into `{title: "text"}` and
  drops the defaults, or every edit of a text index is a phantom rebuild.
- **`list_indexes` returns naive datetimes even from a tz-aware client**, so a
  partial filter with a date never equalled the one the form sent back.
  Definitions are compared through canonical Extended JSON (`_same_bson`).
- The form's text is relaxed Extended JSON (`_shell`), not the table's
  compact display, which is lossy (bare ISO dates, bare decimals, unquoted
  `$**`).
- "Other options" accepts only `INDEX_EXTRA_OPTIONS` (collation, weights,
  default_language, language_override, wildcardProjection, bits, min, max).
  A stored collation is shown fully expanded by the server — verbose, exact,
  and round-trips.

### The visual query builder (ROADMAP phase 33)

`querybuilder.build_filter(rows, logic)` turns `{field, op, type, value}` rows
into shell-syntax text; the view's **Builder** panel (`_qb_*` in
`monguana.py`) keeps the rows in `view["builder"]`.

- Every row carries an explicit **value type**, defaulting to the field's
  sampled type (`default_type`), and `type_set` records that the person chose
  one so picking a field does not overwrite it.
- **Redraw a row, never the panel, on `change`.** A text box's `change` fires
  on blur — on the way to clicking Apply — and redrawing the panel then
  replaced the button mid-click. `_qb_read` swaps the one row and puts focus
  back on the same control; value edits only update the preview.
- Apply goes through `_set_text`, so it lands in the filter editor's undo
  history.
- **Reading a filter back** is server side (`services/filter_rows.py`, via
  `MongoService.builder_rows`): the browser has no `bson`, so it cannot run
  `mql`. `view["builder"]["source"]` is the filter text the rows were read
  from or applied as; opening the panel reads the filter only when it
  differs, so rows in progress for the same filter survive a close. A filter
  rows cannot express sets `note` and leaves the rows alone. The reply is
  dropped if the filter changed while it was asked.

### The pipeline stage list (ROADMAP phase 34)

Aggregate mode shows stage cards (`_render_stages`) or the raw text (**Stages
| Raw**, `view["pl_mode"]`). **The raw `{tid}-pipeline` text is the source of
truth**: every card change rewrites it (`_stages_to_text`, via
`pipeline_text.join`), so Run, Count's validation and everything else read
one place. Cards come back from it with `pipeline_text.split`.

- Disabled stages are `/* off: {…} */` comments in the text; the server's
  parser skips comments, so they never run. A stage containing `*/` cannot be
  disabled (it would end the comment) and says so.
- Cards are redrawn whole on every structural change, so their editors are
  destroyed and remounted (`{tid}-stage-<id>` in `self._editors`); editing a
  body only updates state (`on_change` hook of `_mount_editor`).
- Run to here sends `compose(stages, upto=i)` straight to `_aggregate`
  without touching the text.

### Column layout (ROADMAP phase 35)

The collection table opts in to wapyt's `resizable_columns` and
`reorderable_columns`. Its `columns` event is folded into `view["columns"]`
(`docfmt.merge_column_state`) and saved 0.4 s later under
`columns:<conn>:<db>:<coll>` by `UiStateService`; `_render_table` applies it
(`docfmt.apply_column_state`) in find mode only. Renaming a collection starts
it with a fresh layout.

- **Every column starts with a pixel width** (`docfmt.default_column_width`:
  fits the header, 140–320 px; `_id` 230). wapyt only sizes the table to the
  sum of its columns, and scrolls sideways, once *all* of them have one;
  otherwise `table-layout: fixed` squeezes every column into the panel. A
  `min-width:100%` rule in `_CSS` stretches a narrow table to fill.

- **pytincture validates BFF arguments against their annotations.** `None`
  for a `dict` parameter is a 400 before the method runs — hence
  `UiStateService.clear(key)` rather than `set(key, None)`.

### Default password and the reminder (2026-09-29)

The first admin's password is `MONGUANA_ADMIN_PASS`, or `change_me`
(`db.DEFAULT_ADMIN_PASSWORD`) when that is unset. **`users.must_change_pw`**
marks a password the person did not choose, and while it is set the page
shows `_password_nag` **on every load** (Change password / Later).

- Set: the seeded admin (either source), an account an admin creates, and an
  admin's reset of *someone else's* password.
- Cleared: changing your own password (which must differ from the current
  one), resetting your own from the Users panel, and `manage.py
  reset-password` (whoever runs it chooses).
- `db._migrate` adds the column to older databases and flags any account
  still on `change_me` or the old default `changeme`.
- `UserService.me()` reads the flag from the database, not the session, so
  it clears the moment the password changes. `tests/test_default_password.py`.

### JS properties that may be missing

Reading `element.dataset.x`, or any property, that the JS object lacks
**raises `AttributeError` in Pyodide** where JS would give `undefined`. A plain
stage textarea (no `data-role`) crashed `_on_change` this way, and the
builder's focus restore could have on its remove button. Use
`getAttribute("data-x")` (null → falsy `JsNull`) — `_role(el)` for roles — or
`getattr(obj, "x", None)`, as with `js.navigator.brave` in IguanaXterm.

### The code editor (ROADMAP phase 31)

CodeMirror 6, bundled by `tools/codemirror/build.sh` into one classic script
that sets `window.MgEditor`. Not Monaco: Monaco needs web workers and is
several MB. The bundle is loaded on demand by `_ensure_editor` (one attempt
per page, shared by every tab; a failure is remembered and the text boxes
stay) and mounted by `_mount_editor(area, …)`: for the query boxes through
`_attach_editor(tid, role)` (all five, per `_QUERY_EDITORS`, when a view
opens), and in the document editor directly. A view also samples its fields
in the background (`_sample_fields`) so completions offer field paths at once.

- **The `<textarea>` stays, hidden, as the source of truth.** The editor's
  `onChange` copies every edit into it, so every reader of a query box is
  unchanged. **Writes must go through `_set_text`** (and focus through
  `_focus_field`, hiding through `_set_hidden`); assigning `.value` directly
  updates the hidden textarea and leaves the editor showing the old text.
- **Keys:** the editor's own keymap handles Enter / Ctrl+Enter / Ctrl+S and
  prevents the default; the page's delegated `keydown` skips
  `event.defaultPrevented`, or a run would fire twice.
- **One-line boxes never take a newline on Enter** while a completion list is
  open — see the 75 ms accept guard in ROADMAP phase 31.
- **Escape that closes a completion list is stopped** (`Prec.highest`
  `domEventHandlers` in `entry.js`): wapyt's modal closes on any Escape that
  reaches `document`, even a prevented one, and took the document editor's
  unsaved text with it.
- The document editor's own `keydown` listener skips `defaultPrevented`, or
  Ctrl+S would save twice.
- **Outer braces are optional.** `mql.parse_braced` retries text that does
  not parse as `{…}` (filter, sort, projection, update, a bare pipeline
  stage), and after a successful run `_add_braces` rewrites the box to the
  braced text it was read as. Accepting a field completion writes
  `field: ` — and `{field: }` in an empty filter/sort/projection box
  (`objectBox`, `applyField` in `entry.js`).
- To change the editor: edit `tools/codemirror/entry.js`, run `build.sh`,
  commit the regenerated `monguana-editor.js` and `VERSION` together
  (`tests/test_vendor.py` compares them). `node_modules` is not committed.
- The smoke test types into `.cm-content` (Playwright `fill` works on it),
  records `securitypolicyviolation` events, and repeats a query with the
  bundle blocked to prove the fallback.

## Not built yet

Every gap from the original is closed (ROADMAP phases 31–37). ROADMAP.md
lists what the rewrite still does not do: a live progress bar for export. Phase 38 plans a native
Windows installer (no Docker, no HTTPS, browser only).
Phase 39 (done) added backends: tinymongo stores and plugins, with dump,
restore and copy between any of them.

## Conventions

- `MONGUANA_*` environment prefix, `/data` volume, SQLite + Fernet at rest.
- **A release bumps the version in two places**: `pyproject.toml` and
  `appcode/services/about.py` (`VERSION`, shown in About), plus the README
  badge and `uv lock`. `tests/test_about.py` fails if the two disagree. Tag
  `vX.Y.Z` on `main`; `windows.yml` builds the installer and the release.
  Updating is documented in INSTALL.md §12; `docs/UPGRADE_TEST.md` is the
  hand test for an in-place update (CI only tests a fresh install). Run it
  when the installer, launcher or database schema changes, and record it.
- **About and the release check**: `services/about.py` (version + links, both
  sides), `release_check.py` (GitHub's latest-release API, cached 6 h / 30 min
  on failure, plain module), `about_service.py` (the BFF). Off with
  `MONGUANA_UPDATE_CHECK=off`.
- Port **8766** (IguanaXterm has 8765).
- **MIT**.
- Connection profiles belong to one user; there is **no unscoped read** of
  the `connections` table anywhere.
- This file is tracked.
