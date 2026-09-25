# Roadmap

Monguana 2 reached most of the original's feature list at its first commit.
What is left are the original's phases the rewrite does not match yet, found
by going through the original `static/app.js` feature by feature (the
original's own phase table marks all 30 phases done, and it has no issues,
PRs or other branches). Numbering continues from the original's.

| Original phase | In the rewrite now | Gap |
|---|---|---|
| 6: Monaco + autocomplete | CodeMirror in the filter box (phase 31) + the Fields dialog | Editor in the other boxes; field completions before sampling |
| 10: Visual query builder | Nothing | Missing |
| 11: Pipeline builder | One text box + stage templates | No per-stage list with move, enable/disable, remove |
| 24: Column width/order memory | Nothing | wapyt's DataTable cannot resize or reorder columns |
| 13/16: Dump/restore progress console | Summary when it finishes | No live progress |
| Small items | | View mode not remembered; `system.*` collections always listed |

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

## Phase 32: editor everywhere, plus completions (original phase 6)

- The editor in the sort, projection, update and pipeline boxes and in the
  document editor (`_attach_editor(tid, role, multiline=…)` already takes any
  box; the document editor needs `onSave` wiring).
- Completions: operators and helpers are in; field paths arrive only after
  Fields has sampled. Sample in the background when a view opens.
- Enter-to-run and Ctrl+S keep working.
- The text boxes remain the fallback if the bundle fails to load.

## Phase 33: visual query builder (original phase 10)

- Rows of field / operator / value, joined by AND or OR, producing filter text
  in shell syntax.
- Generation lives in `services/querybuilder.py`, plain Python shared by the
  browser and the unit tests.
- Fixes two bugs in the original's builder: it chose operators by Python type
  names such as `str`, which never matched most fields, and coerced every
  value to a number or string, so an ObjectId or a date became a string. Adds
  `$in`, `$exists` and `$regex`.

## Phase 34: pipeline stage list (original phase 11)

- One editor per stage, with move up/down, enable/disable and remove.
- Switch between the stage list and the raw pipeline text.
- New: "Run up to this stage" to see intermediate results.
- The server's read-only pipeline checks stay as they are.

## Phase 35: column width/order memory (original phase 24)

- First a wapyt change: resizable columns, drag-to-reorder, and a
  column-change event. It benefits IguanaXterm's file table too.
- Then the app saves each collection's column state per user, on the server,
  not per browser as the original did.

## Phase 36: dump/restore progress (original phases 13/16)

- pytincture cuts off streamed BFF responses after 300 s, so this is a
  background job the page polls about once a second, as IguanaXterm's
  server-side downloads do.
- A console with per-collection progress, Cancel, and the final summary.
- Restore keeps uploading the ZIP as it does now, then follows the job.

## Phase 37: small items

- Remember the view mode (table/JSON/tree) per collection.
- A "show system collections" toggle.
- The Int64 → Int32 change when saving an edited document — possibly by
  opening the editor in canonical Extended JSON when a document holds an
  Int64.
- wapyt's modal close button hides the dialog but leaves it in the page;
  make it close.

## Order

31 is done; 32 next. 33 and 34 can use the editor for their value and stage
boxes.
