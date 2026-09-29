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
- AND merges conditions on one field (`{age: {$gte: 18, $lt: 65}}`) and only
  falls back to `$and` when two would collide; OR is `$or`.
- Not done: reading an existing filter back into rows. Apply replaces the
  filter.

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
works around it (see CLAUDE.md).

**Still open:**

- A hand check on a Windows desktop: the tray icon, the first-run message box
  and the Start-menu shortcuts.
- The upstream pytincture fix (branch `fix/windows-contained-file-open`,
  ready locally, parked: no push access to `pytincture/pytincture`). Once a
  release has it, bump the pin and delete `pytincture_compat.py`.

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

## Beyond the original

Not gaps — ideas the rewrite could take further:

- Read an existing filter back into the query builder's rows (Apply replaces
  the filter today).
- Progress for exports, which stream but show no bar.

## Order

31–37 are done: every gap from the original is closed.

38 (native Windows install) is in progress.
