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

## Phase 37: small items

- Remember the view mode (table/JSON/tree) per collection.
- A "show system collections" toggle.
- The Int64 → Int32 change when saving an edited document — possibly by
  opening the editor in canonical Extended JSON when a document holds an
  Int64.
- wapyt's modal close button hides the dialog but leaves it in the page;
  make it close.

## Order

31–36 are done: every gap from the original is closed. Phase 37's small items
remain.
