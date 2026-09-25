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

- `pytincture/` — the framework (pinned to tag `v1.0.0rc10`).
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
    mongo_pool.py           #   MongoClient pool — plain module, NOT a BFF
    connection_service.py   #   BFF: profiles, test, connect/disconnect
    mongo_service.py        #   BFF: databases, collections, documents, indexes…
    user_service.py         #   BFF: accounts (from IguanaXterm)
    transfer.py             #   plain routes under /mg: export, dump, restore
  static/                   #   artwork, same-origin
  vendor/codemirror/        #   the editor bundle (built, committed), served at /vendor
tools/codemirror/           # its recipe: pinned package.json, entry.js, build.sh
  wapyt-99.99.99-*.whl      #   dev wheel the BROWSER installs (git-ignored)
tests/                      # unit tests; test_live.py needs a MongoDB
tests/smoke/                # ui_smoke.py (Playwright) + seed_shop.py
```

## Running

```bash
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode   # after ANY wapyt asset edit
uv run python service.py                                  # http://127.0.0.1:8766/monguana
uv run --group dev pytest -q
scripts/podman-run.sh                                     # container "monguana"
```

After changing wapyt's *Python* wrappers: `uv sync --reinstall-package wapyt`.

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
context menus, exports CSV, and round-trips a dump through Restore. It fails
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
- **wapyt installed non-editable** or the app boots with no widgets.
- **pytincture will not serve authenticated plain HTTP** except on a literal
  loopback IP: use `http://127.0.0.1:8766`, never `localhost`.
- **2 MiB body cap on every route.** `BodyLimitExceptRestore` lifts it for
  `POST /mg/restore/<id>` only; that route authenticates and checks CSRF
  before reading a byte and enforces `MONGUANA_MAX_RESTORE_BYTES` while
  streaming.
- **No CDN, ever.** pytincture's CSP blocks it. This is why the original's
  Monaco and AG Grid are not here (see *Not built yet*).
- **`JsNull` is not `None`.** Test DOM lookups for truthiness.
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

One known loss: relaxed EJSON writes a small Int64 as a plain number, so
editing such a document saves it back as Int32.

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

`mongo_pool.client_options` passes `username`/`password` as keyword
arguments. The original formatted them into `mongodb://user:pass@host`, so a
password with `@`, `:` or `/` (the test container's has all three) produced a
different URI entirely. A profile may instead hold a full connection string
(`uri`, encrypted like the password), which then wins. Secrets are
three-state in `ConnectionService.save`: omitted = unchanged, `""` = cleared.
In the editor, a single space in *Connection string* clears a stored one.

### The pool

One `MongoClient` per `(user_id, connection_id)`. It re-reads the profile on
each call (one indexed SQLite lookup) and rebuilds the client when the
options fingerprint changes, so an edited profile applies at once. Idle
clients close after 15 minutes.

### Plain routes for bytes — `transfer.py`, under `/mg`

Export (`GET /mg/export/<id>`), dump (`GET /mg/dump/<id>`) and restore
(`POST /mg/restore/<id>`, body = the ZIP itself) are plain FastAPI routes that
read pytincture's session cookie, as IguanaXterm's transfers do. The BFF is
JSON and capped at 2 MiB, so a dump through it would be base64 in Pyodide's
heap. Export streams every match (not just the page) up to 1M (JSON) / 100k
(CSV); the UI checks the filter with a count first, because a bad one would
otherwise surface as a failed download with no message. Dump builds a
mongodump-layout ZIP into a spooled temp file on a worker thread, reading raw
BSON (`RawBSONDocument`) so documents are never decoded and re-encoded. Note
`insert_many` returns an empty `inserted_ids` for `RawBSONDocument`s — restore
counts the batch instead.

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
- **Modals are closed, not hidden.** wapyt's `hide()` leaves the overlay in the
  DOM; `close()` removes it. (Its × button still only hides — see wapyt's
  rough edges.) wapyt's modal also sets no font, hence the `.wapyt-modal` rule
  in `_CSS`.

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
- To change the editor: edit `tools/codemirror/entry.js`, run `build.sh`,
  commit the regenerated `monguana-editor.js` and `VERSION` together
  (`tests/test_vendor.py` compares them). `node_modules` is not committed.
- The smoke test types into `.cm-content` (Playwright `fill` works on it),
  records `securitypolicyviolation` events, and repeats a query with the
  bundle blocked to prove the fallback.

## Not built yet (the original had these)

See ROADMAP.md for the plan. In short:

1. **Visual query builder** (field/operator/value rows generating the filter).
2. **Pipeline stage list** — add/reorder/toggle stages individually. Today:
   one textarea plus stage templates.
3. **Column width/order persistence** per collection.
4. **Dump/restore progress console.** Restore returns a per-collection summary
   when done; no live progress.

## Conventions

- `MONGUANA_*` environment prefix, `/data` volume, SQLite + Fernet at rest.
- Port **8766** (IguanaXterm has 8765).
- **MIT**.
- Connection profiles belong to one user; there is **no unscoped read** of
  the `connections` table anywhere.
- This file is tracked.
