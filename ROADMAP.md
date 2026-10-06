# Roadmap

Monguana 2 reached most of the original's feature list at its first commit.
What is left are the original's phases the rewrite does not match yet, found
by going through the original `static/app.js` feature by feature (the
original's own phase table marks all 30 phases done, and it has no issues,
PRs or other branches). Numbering continues from the original's.

| Original phase | In the rewrite now | Gap |
|---|---|---|
| 6: Monaco + autocomplete | CodeMirror in every query box and the document editor, with field completions (phases 31–32) | — |
| 10: Visual query builder | Builder panel with typed values (phase 33) | — |
| 11: Pipeline builder | Stage cards + Raw, with Run to here (phase 34) | — |
| 24: Column width/order memory | Resize and reorder, saved per user on the server (phase 35) | — |
| 13/16: Dump/restore progress console | Jobs with a progress console and Cancel (phase 36) | — |
| Small items | Remembered view mode, system-collection toggle, Int64 fidelity (phase 37) | — |

## Phase 31: code-editor spike — done (2026-09-25)

**Result: CodeMirror 6 works inside pytincture's CSP.** Phases 32–34 build on
it.

- `tools/codemirror/` pins seven CodeMirror/lezer packages and esbuild;
  `build.sh` bundles `entry.js` into one classic script,
  `appcode/vendor/codemirror/monguana-editor.js` (441 KB, 152 KB gzipped),
  plus `VERSION` (package versions and sha256) and `LICENSE` (MIT). Served
  from `/vendor`.
- `entry.js` exposes a small value-in/value-out API, `window.MgEditor.create`,
  which Python drives through the FFI: `getValue`, `setValue`, `focus`,
  `setFields`, `setReadOnly`, `destroy`.
- The filter box is an editor: shell-syntax highlighting, bracket matching and
  auto-closing, completions for operators, BSON helpers and (once sampled)
  field paths, Enter runs, Shift+Enter breaks the line.
- **Verified in Chromium:** zero `securitypolicyviolation` events and zero
  console errors across the whole UI smoke test. The bundle contains no eval,
  workers or remote URLs; `tests/test_vendor.py` keeps it that way and checks
  it against its recorded checksum.
- **Fallback verified:** with the bundle blocked, the plain text box stays and
  queries still run.
- **Bug found by the smoke test:** CodeMirror declines to accept a completion
  within ~75 ms of the list opening, and a declined Enter fell through to the
  default newline — a fast Enter put a line break in the filter. In one-line
  boxes Enter now accepts or does nothing while the list is open.

## Index CRUD — done (2026-09-25)

Not one of the original's gaps — the original could only create and drop —
but asked for alongside phase 31. Edit (in place via `collMod` where MongoDB
allows it, otherwise a planned rebuild), hide/unhide, sizes, and the full
set of create options. See CLAUDE.md, *Index CRUD*.

## Phase 32: editor everywhere, plus completions (original phase 6) — done (2026-09-25)

- **Every query box is an editor:** filter, sort and projection (one line:
  Enter runs), update and pipeline (multi-line: Tab indents, Ctrl+Enter
  runs). The pipeline's stage templates insert into the editor with the caret
  inside the new stage.
- **The document editor** (edit, insert, clone, bulk update) is a full-height
  editor: Ctrl+S or Ctrl+Enter saves.
- **Field completions from the first keystroke:** a view samples its fields
  in the background when it opens; the Fields dialog reuses the sample.
- **Fallback kept:** with the bundle blocked, every box — the document editor
  included — is the plain text box and works.
- **Bug found and fixed:** Escape that only closed a completion list also
  reached wapyt's modal, which closes on any Escape — the document editor
  vanished with its unsaved edits. The editor now stops that Escape, at the
  highest precedence (at normal precedence the completion keymap closed the
  list first and the guard saw nothing to do).

## Phase 33: visual query builder (original phase 10) — done (2026-09-25)

- A **Builder** panel in each view: rows of field (sampled paths offered as
  suggestions), operator, value type and value; AND/OR; a live preview of the
  filter; **Apply** writes it into the filter editor (Ctrl+Z undoes) and runs.
- Generation is `services/querybuilder.py`, pure Python shared by the browser
  and `tests/test_querybuilder.py`, which proves each output by parsing it with
  the server's `mql`.
- **Values are written as their type**, defaulting to the field's sampled
  type: String, Number, Decimal, Date, ObjectId, Boolean, UUID, Null, or Raw
  (any shell literal as typed). A value that is not what its type says is
  refused with the row number. This fixes the original's guessing, which made
  `"02134"` a number and an ObjectId or a date a string.
- Operators fit the type: `=, ≠, >, ≥, <, ≤, in, not in, matches` (strings),
  `exists`, `is of type`, and `array size` for fields sampled as arrays.
- Two or more conditions are a list under `$and` or `$or`, each in its own
  braces (`{$and: [{age: {$gte: 18}}, {age: {$lt: 65}}]}`); one stands alone.
  (It first merged AND into one object; changed 2026-10-01 at the user's
  request, so picking AND shows the list it implies.)
- Reading an existing filter back into rows: done 2026-10-01 (see
  "Beyond the original").

## Phase 34: pipeline stage list (original phase 11) — done (2026-09-25)

- In aggregate mode the pipeline is a list of **stage cards**: each with its
  stage selector, its own editor, **Run to here**, move up/down,
  enable/disable and remove. "+ stage" adds a card with the stage's template.
- **Stages | Raw** switches to the whole pipeline as text and back.
- **The raw text stays the source of truth** (it is what Run sends): the cards
  rewrite it on every change. Disabled stages are kept in it as
  `/* off: {…} */` comments, which the server's parser skips — a disabled
  stage cannot run, and nothing is lost switching views.
- `services/pipeline_text.py` (pure, both sides) splits text into stages and
  joins them back, skipping over strings, regex literals and comments
  (`/[a-z],]/` and `"a, ]}"` are not structure). Text that does not split —
  a stage with two keys, say — stays in Raw with the reason and position.
  `tests/test_pipeline_text.py` round-trips a deliberately awkward pipeline
  and proves the output with the server's parser.
- New over the original: Run to here.
- **Bug found by the fallback test:** in Pyodide, reading a `dataset`
  property the element lacks raises `AttributeError` (JS would give
  `undefined`), so a plain stage box — no `data-role` — crashed the change
  handler. Such reads now go through `getAttribute` (`_role`).

## Phase 35: column width/order memory (original phase 24) — done (2026-09-25)

- **wapyt DataTable** gained opt-in `resizable_columns` (drag a header's right
  edge) and `reorderable_columns` (drag a header onto another), a `columns`
  event with `[{id, width}]`, `get_column_state()` and `move_column()`. Off by
  default, so IguanaXterm is unchanged.
- **Monguana** saves each collection's widths and order **per user on the
  server** (`ui_state` table, `UiStateService`), not per browser as the
  original did, and reapplies them whenever the table is drawn. Saves are
  debounced. A page missing some fields keeps their saved place and width
  (`docfmt.merge_column_state` / `apply_column_state`, unit-tested).
  Aggregation output has its own shape and is not saved. A **Reset columns**
  button next to the view switch forgets the layout.
- **Bugs found building it:**
  - The resize grip overhung its header by 4 px, and the next sticky header
    (its own stacking context) painted over the overhang, so a press there
    started a column drag instead of a resize. The grip now sits inside.
  - A header's inline width set its *content* box while widths were measured
    as border boxes, so freezing widths added 20 px of padding to every
    column. Resizable tables are `box-sizing: border-box`.
  - `UiStateService.set(key, None)` was refused with a 400: pytincture
    validates arguments against annotations, and `None` is not a `dict`.
    Deleting is its own `clear(key)`.

## Phase 36: dump/restore progress (original phases 13/16) — done (2026-09-25)

- Dump and restore run as **background jobs** (`services/jobs.py`, a plain
  module; `JobService` to start a dump and to poll or cancel). The page polls
  twice a second — not a `@bff_stream`, which pytincture cuts off at 300 s.
- A **console** shows a bar per collection (documents for a dump, bytes read
  for a restore), the log, **Cancel** (cooperative, between batches; what
  finished is kept), the final summary, and for a dump the download, which
  starts by itself and can be repeated for an hour. Closing the console does
  not stop the job.
- The restore **upload shows its own progress** (XHR upload events — `fetch`
  reports nothing while sending a body); the job starts once it lands.
- Verified in Chromium on 400,000 documents: the bar at 60% mid-dump, then
  Cancel → cancelled, the partial file deleted, no errors.
- **Bug found by the smoke test:** the console's Cancel stayed visible after
  the job ended — `.mg-btn`'s `display:inline-flex` beats the `hidden`
  attribute (IguanaXterm's trap). `.mg-btn[hidden]` now hides.

## Phase 37: small items — done (2026-09-25)

- **The view mode is remembered** per collection and user (Table, JSON or
  Tree), next to the column layout in `UiStateService`.
- **System collections** (`system.views`, `system.profile`, …) are hidden by
  default; "Show / hide system collections" on a server's or database's menu
  toggles them, per user.
- **Int64 keeps its type** through the display and the editor: tagged as
  `{"$numberLong": "5"}` even when small, so saving an edited document no
  longer turns it into an int32. Cells still read `5`.
- **wapyt modals can dispose on close:** `ModalConfig(dispose_on_close=True)`
  makes ×, Escape and a backdrop click remove the dialog. Opt-in, because
  wAwesomeChat builds its modals once and reopens them — making × destroy
  them would have broken it. `close()` now also removes the modal's
  document-level Escape listener, which used to outlive every dialog.
  Monguana opts in everywhere.

## Phase 38: native Windows install, browser only — in progress (2026-09-29)

**Built** (`tools/windows/`, `.github/workflows/windows.yml`, INSTALL.md §11):
the launcher, the bundle build (runs on any OS), the Inno Setup script, the
Windows CI job, and the audit fixes (MIME types pinned against the Windows
registry, key files written in binary mode, UTF-8 for the server). Tested on
Linux: the bundle builds; the launcher's first run, port fallback, single
instance, `--status`/`--stop`/`--check`, browser sign-in with the generated
password, and `--reset-password`.

**Passing on Windows CI** (`windows-latest`, 2026-09-29): unit tests, the
installer build, silent install, the installed app serving its login page,
background start, `.js` served as `text/javascript` against a real Windows
registry, `--stop`, and silent uninstall keeping the data. Its first run found
pytincture unable to read any contained file on Windows; `pytincture_compat.py`
worked around it until pytincture#377 fixed it upstream (1.0.0rc13).

**Checked by hand on Windows 11** (2026-09-29): the installer built by CI
installs and works, including the tray icon, the first-run message box and
the Start-menu shortcuts.

**Still open:**

- ~~The upstream pytincture fix~~ merged as pytincture#377 (1.0.0rc13); the
  pin is bumped and `pytincture_compat.py` deleted.

A `setup.exe` that runs Monguana on Windows **without Docker and without
HTTPS**, for people who only want it on their own machine.

**Starting point.** Monguana and IguanaXterm both already run on Windows
under **Docker Desktop** (tested by hand, 2026-09-29), so the compose route in
INSTALL.md stays the answer for anyone who has Docker. This phase is for
machines without it.

**Why no HTTPS is needed.** pytincture already serves an authenticated app
over plain HTTP on a literal loopback address. The install binds to
`127.0.0.1` and opens `http://127.0.0.1:<port>/monguana`, never `localhost`,
which answers `400 Invalid host header`.

**Why it should port.** Nothing loads from a CDN: Pyodide 0.29.3, the MDI
font, the CodeMirror bundle and the wapyt wheel are all served by the app
itself, so it works offline. The server's dependencies (uvicorn/FastAPI,
pymongo, bcrypt, cryptography, SQLite) all have Windows wheels. The only
POSIX-only code found is pytincture's `resource` process limits, which already
skip themselves on Windows.

**Scope:**

- **Browser only.** The launcher opens the default browser. A desktop window
  (pywebview/WebView2) is out of scope for now.
- **No MongoDB bundled**, in the installer or in the repo for testing. Users
  point Monguana at their own server; a native install reaches
  `127.0.0.1:27017` directly, without the bridge-network workarounds the
  containers need.
- **The login stays.** On a shared machine it stops other programs or users
  from reaching your saved connections through the port.

**Work:**

1. **Launcher** (`monguana-launch`, Python): pick port 8766 or the next free
   one, start `service.py` bound to 127.0.0.1, wait for the health check, open
   the browser. Tray icon with Open / Quit; a second launch just opens the
   browser on the running instance.
2. **First run:** data in `%LOCALAPPDATA%\Monguana` (`MONGUANA_DATA_DIR`).
   Ask for the admin password, or generate one and show it once, instead of
   `.env`. The Fernet key is generated per install. `manage.py
   reset-password` gets a Start-menu shortcut.
3. **Packaging:** a pinned embeddable CPython with the dependencies
   pre-installed (or a PyInstaller build), wrapped by Inno Setup:
   Start-menu entries, an uninstaller, and a choice to keep or remove the data
   folder on uninstall. Optional: start at login.
4. **Windows audit:** paths in dump/restore and job files (`pathlib`, no
   `/tmp`), SQLite locking, and file handles closed before a job's file is
   deleted, since Windows refuses to delete an open file.
5. **CI:** a `windows-latest` GitHub Actions job on each release tag that runs
   the unit tests, builds the installer, installs it silently, and checks that
   the app starts and serves its login page. The full Playwright smoke test is
   run by hand against a MongoDB the tester supplies (`SMOKE_MONGO_HOST`),
   since no MongoDB ships with the repo.
6. **INSTALL.md:** a "Windows without Docker" section, including the unsigned
   installer's SmartScreen warning. Code signing is a later decision; it needs
   a certificate.

**Later, not this phase:** a desktop window instead of a browser tab; the
same launcher for IguanaXterm, which shares the service wiring.

## Phase 39: backend plugins, tinymongo first — done (2026-09-30)

**Step 1 done** (2026-09-30): `services/backends/` (registry + the built-in
`mongodb` backend), `connections.backend`/`options` with their migration, the
pool and `ConnectionService` building, validating and testing through the
profile's backend. A profile naming a backend that is not installed says so
instead of failing. `tests/test_backends.py` covers it with a fake backend;
the live tests and the UI smoke test pass unchanged against MongoDB 7.

**Step 2 done** (2026-09-30): `backends.CAPABILITIES` names 13 optional
features; a backend lists the ones it has (MongoDB: all). `MongoService` falls
back without `stats`, `sample`, `collection_types`, `time_limits`,
`authorized_listing`, `hello` and `collmod` (index changes rebuild instead), and
refuses the rest with "<Backend> connections cannot …". The connection list
carries each profile's capabilities to the UI, which hides the tree entries
(through wapyt's new `TreeAction(requires=…)`), Explain, the capped, TTL and
Hidden fields, and Hide/unhide. Live tests run every fallback and refusal
against MongoDB through a backend declaring none; a browser check compared
that profile's menus and dialogs with a full MongoDB one. It needs wapyt's
`requires` (WAwesome-AI/wa_pytincture_widgetset#17): `WAPYT_REF` is pinned to
its merge, `d3fecf4`.

**Step 3 done** (2026-09-30): `services/backends/tinymongo.py`, registered only
when `MONGUANA_TINYMONGO_ROOT` is set. Profiles store an engine (`sqlite`,
`json`) and a folder relative to that root, re-resolved and checked on every
use (`..`, absolute paths, symlinks out). Capabilities: `authorized_listing`
and `create_collection` (a `create_collection(database, name)` hook inserts
and deletes a placeholder, since tinymongo creates collections on first
insert). Test lists databases instead of pinging `admin`, which on the JSON
engine creates `admin.json`. The editor gets **Connects to** and builds the
fields of a backend other than MongoDB from `ConnectionService.backends()`.
Container root `/tinymongo` (volume `monguana-tinymongo`), Windows
`%LOCALAPPDATA%\Monguana\tinymongo`; `tinymongo>=1.3.1` is a dependency.
`tests/test_tinymongo.py` runs every service against both engines without a
MongoDB. Found on the way: tinymongo refuses descending index keys, update
pipelines and collations (shown as its own errors).

**Step 5 done** (2026-09-30): dump, restore and **copy** across backends.
`dump_restore` became two capabilities, `raw_bson` (RawBSONDocument end to
end) and `bulk_write` (Merge's batched upserts); without them documents are
decoded/encoded and Merge replaces one at a time, so Dump and Restore are on
for every backend. `transfer._Writer` writes for restore and copy, and creates
indexes one by one so a refused one does not stop the rest.
`JobService.start_copy` and the **Copy database / collection to…** dialog copy
between any two of a user's connections as a job. Tests: dump/restore round
trips and a copy job on all four tinymongo engines; live, the decoded paths
through the no-capability backend and MongoDB → tinymongo → MongoDB with the
BSON types checked (a small Int64 comes back Int32: tinymongo returns `int`).

**Step 4 done** (2026-09-30): third-party backends through the
`monguana.backends` entry-point group, loaded once with the built-ins under
one lock. A plugin that fails to load, lacks part of the protocol
(`backends.REQUIRED`), claims a built-in or taken name, or declares an unknown
capability is left out and listed by `python manage.py backends` (exit 1).
`examples/monguana-sandbox-backend` is a working plugin (in-memory tinymongo)
and `tests/test_plugins.py` installs it as a real distribution and drives it
through the services; docs/BACKEND_PLUGINS.md is the guide. tinymongo's
DuckDB and Parquet engines appear when installed: Monguana extras `duckdb`
and `parquet`, and in the container the `EXTRA_PACKAGES` build argument
(`MONGUANA_EXTRA_PACKAGES` in `.env`), which also installs plugins. The
tinymongo suite passes unchanged on all four engines.

Let a connection point at something other than a MongoDB server, starting with
**tinymongo** (`../tinymongo`, PyPI `tinymongo`): a PyMongo-shaped library
that stores databases in local files (SQLite, JSON, DuckDB, Parquet).

**Starting point.** tinymongo has **no wire-protocol server** yet (its roadmap
§6–8, unbuilt), so Monguana cannot reach it over TCP. The plugin opens the
store **in-process** in the Monguana server. Its `MongoClient` is close enough
to PyMongo's that most of `MongoService` works unchanged. Monguana assumes
MongoDB throughout today: `mongo_pool` always builds a `pymongo.MongoClient`,
`connections` has no backend column, `_validate` accepts only `mongodb://`
URIs, and several views use server features tinymongo lacks.

**What tinymongo lacks that Monguana uses** (checked against tinymongo master,
2026-09-30): `$collStats` (the header of every collection view), `$sample`,
`explain`, `rename`, `create_collection` options (capped), `list_collections`
(types and options), `collMod`, `bulk_write`, `maxTimeMS`, `hello`, and
aggregation beyond `$match/$sort/$skip/$limit/$count/$project/$set/$unset/$group`
(no `$lookup`, `$unwind`). `db.command` answers only `ping` and `buildInfo`.
It reports what it supports through `client.capabilities()`.

**Design:**

1. **Backend protocol and registry** in a plain module, `services/backends/`
   — not a BFF: BFF modules re-execute per call, and `test_bff_state.py`
   forbids module-level state in them. A backend provides `name`, `label`,
   its connection-editor fields (the editor is built from them), `validate`,
   `open(profile)` → a PyMongo-shaped client, `test(profile)` → a status
   line, and `capabilities`. The current code moves into the built-in
   `mongodb` backend.
2. **Storage:** `backend TEXT DEFAULT 'mongodb'` and `options TEXT` (JSON,
   Fernet-encrypted when it holds secrets) on `connections`, added in
   `_migrate`. Fix the hand-written column lists in `ConnectionService.save`
   and `duplicate`. The pool fingerprint includes backend and options.
3. **Capabilities.** MongoDB has them all, so nothing changes there. Without
   one, `MongoService` falls back or refuses:

   | Capability | Fallback |
   |---|---|
   | Collection stats | `count_documents` + `sizeOnDisk` from `list_databases` |
   | `$sample` (builder, fields) | `find().limit(n)` |
   | `list_collections` | plain names, type `collection` |
   | Raw BSON reads | decode, then `bson.encode` |
   | `maxTimeMS` | dropped |
   | Explain, rename, capped, collMod, TTL/hidden indexes | hidden in the UI; the service refuses with a clear message |

   The tree nodes carry the backend's capabilities, and the context menu,
   index dialog and Explain button follow them. `TreeAction` filters by node
   `kind` only, so wapyt probably needs a small capability filter.
4. **tinymongo backend:**
   - Editor fields: engine select and folder. **First release: `sqlite` and
     `json`** (no extras needed). `duckdb`/`parquet` come later and only
     appear when their extras are installed. `memory` (lost on restart) and
     Postgres/MySQL (DSNs) are out.
   - **Security:** the folder is a path on the server, so any Monguana user
     could otherwise read or write any file the server can reach. It must
     resolve inside one admin-set root, **`MONGUANA_TINYMONGO_ROOT`**; unset
     means the backend is off. Containers mount a volume there; the Windows
     install defaults it to `%LOCALAPPDATA%\Monguana\tinymongo`.
   - **Test creates nothing:** tinymongo's constructor makes folders, so Test
     checks the path exists before opening it.
   - Unsupported stages raise `TinyMongoNotSupportedError`, shown as an
     error; the stage cards can mark them.
   - Database names come from the folder's files for the chosen engine: the
     engine cannot be auto-detected.
5. **Third-party plugins** through a `monguana.backends` entry-point group —
   after tinymongo works, so the protocol is shaped by a real second backend.

**Tests:** unit tests for the registry, validation and the root containment
(`..`, symlinks); a tinymongo variant of `test_live.py` on SQLite in a temp
folder, which needs no MongoDB and so runs in CI on every push; a smoke step
that creates a tinymongo profile through the editor.

**Work, in order:**

1. Protocol, registry and schema migration; MongoDB moved into a backend.
   No visible change: the existing tests stay green.
2. Capabilities, the service fallbacks and the UI gating (plus the wapyt
   `TreeAction` change if needed).
3. The tinymongo backend on `sqlite` and `json`: root, editor fields, tests,
   container volume and Windows bundle.
4. Entry-point discovery for third-party backends; `duckdb`/`parquet`; docs
   (INSTALL.md, CLAUDE.md).
5. Dump, restore and copy across backends. Both already read and write the
   mongodump layout in Python, so MongoDB → tinymongo and back comes almost
   free.

## Phase 40: native macOS install, browser only — planned (2026-10-01)

A `.dmg` with a `Monguana.app` that runs Monguana on a Mac **without Docker and
without HTTPS**, the macOS counterpart of phase 38. Docker Desktop or Podman
(INSTALL.md) stays the answer meanwhile.

**What carries over from phase 38:**

- **The launcher** (`tools/windows/launcher.py`) is already cross-platform and
  tested on Linux. It keeps one instance, uses port 8766 or the next free one
  on 127.0.0.1, starts the server, opens the browser, and handles `--stop`,
  `--status` and `--check`.
- **The bundle approach:** a bundled Python with the app as plain files, no
  PyInstaller, for the same reason as on Windows (pytincture serves appcode's
  source to the browser).
- **No pytincture workaround is needed:** macOS supports `dir_fd` for
  `os.open`, and the service-worker and Windows fixes are upstream since
  1.0.0rc12 / rc13.
- **No HTTPS needed**, as on Windows: a literal loopback address.

**Findings (2026-10-01):**

- **Apple Silicon is covered.** Every compiled package in `uv.lock` (14)
  publishes macOS arm64 wheels.
- **Intel is not.** `cryptography` 50.0.1 and `argon2-cffi-bindings` 26.1.0
  publish no x86_64 macOS wheels. Supporting Intel means building them from
  source (cryptography needs Rust) or pinning releases that still had Intel
  wheels.
- **python.org has no embeddable macOS Python.** The relocatable choice is
  **python-build-standalone** (the builds `uv` uses), for arm64 and x86_64.

**Scope:**

- **Apple Silicon only, unsigned, first.** Apple Silicon Macs have shipped
  since late 2020, and Apple has sold no Intel Macs since 2023.
- **Browser only and no MongoDB bundled**, as on Windows.

**Work:**

1. **Runtime and dependencies.** `build.py` (or a sibling under
   `tools/macos/`) fetches python-build-standalone, pinned by sha256, and
   installs `uv.lock` as macOS arm64 wheels with
   `uv pip --python-platform aarch64-apple-darwin --only-binary :all:`, as the
   Windows build does for its platform.
2. **App bundle.** `Monguana.app/Contents/` with `Info.plist`
   (`LSUIElement` for a menu-bar app with no Dock icon), the icon as `.icns`,
   and a small executable in `Contents/MacOS` that runs the launcher with the
   bundled Python.
3. **Launcher changes.** Data in `~/Library/Application Support/Monguana`
   (today it is `~/.local/share/monguana` off Windows). The menu-bar icon is
   pystray, which needs PyObjC on macOS and must run on the main thread.
   Messages via `osascript` instead of the Windows message box.
4. **DMG.** `hdiutil`, with the usual drag-to-Applications window.
5. **Updates.** Dragging a new `Monguana.app` over the old one keeps the data,
   which lives outside the app. Nothing stops a running copy first, unlike
   the Windows installer, so either say "Quit from the menu bar first" or ship
   a `.pkg` whose preinstall script runs `--stop`.
6. **CI.** A `macos` workflow on a GitHub macOS (arm64) runner: unit tests,
   build the bundle and DMG, mount it, run `--check`, start, stop, and attach
   the DMG to `v*` releases, as `windows.yml` does.
7. **Docs.** An INSTALL.md section, the wiki's Installing page, and
   `docs/UPGRADE_TEST.md` steps for the Mac.

**Signing (later, a decision for the owner).** Without an Apple Developer ID,
Gatekeeper blocks a downloaded app. Since macOS 15 the right-click → Open
bypass is gone: people must try to open it, then use **System Settings →
Privacy & Security → Open Anyway**. With the Apple Developer Program
(US$99/year), every binary in the bundle is signed with hardened runtime, the
DMG is notarized with `notarytool` and stapled. The bundled Python may need
entitlements for cffi; that needs a real Mac to establish.

**Intel (only if asked).** Universal or separate x86_64 builds, after solving
the two missing wheels above.

**Testing.** No Mac on the development workstation: CI covers the build and
`--check`, and the first hand test (install, menu-bar icon, sign-in, update
over an older build) needs a real Mac.

**IguanaXterm** gets the same phase (its ROADMAP.md); the work is shared.

## Beyond the original

Not gaps — ideas the rewrite could take further:

- ~~Read an existing filter back into the query builder's rows~~ — done
  2026-10-01. `services/filter_rows.py` (server side, it parses with `mql`)
  turns a flat AND or a single `$or` of one-field conditions into rows typed
  as the builder would write them; opening the builder reads a filter that
  changed since the rows were last read or applied, and **Read filter** does
  it on demand. What rows cannot express (`$nor`, `$expr`, `$elemMatch`,
  `$not`, `$or` inside an AND, numeric `$type`) is refused with a note and the
  rows are kept. Tests prove read → build → read is a fixed point.
- Progress for exports, which stream but show no bar.
- ~~A server dashboard and the query profiler~~ — done 2026-10-01. See
  CLAUDE.md, *Dashboard and query profiler*. Killing a running operation
  from the dashboard followed the same day, and then a profile filter editor.

## Order

31–37 are done: every gap from the original is closed.

38 (native Windows install) is in progress.

39 (backend plugins, tinymongo first) is done.

40 (native macOS install) is planned: Apple Silicon, unsigned, first.
