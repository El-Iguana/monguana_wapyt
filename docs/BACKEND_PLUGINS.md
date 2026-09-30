# Backend plugins

A Monguana connection points at a **backend**: a MongoDB server (built in), a
tinymongo store (built in, see INSTALL.md), or anything a plugin adds. A
plugin is an ordinary Python package that names a backend in the
`monguana.backends` entry-point group. Install it where Monguana runs and
restart; **New connection → Connects to** then offers it.

`examples/monguana-sandbox-backend/` is a complete, working plugin (throwaway
in-memory databases on tinymongo's memory engine). `tests/test_plugins.py`
installs it and drives it through the real services, so it stays correct.

## The entry point

```toml
[project.entry-points."monguana.backends"]
sandbox = "monguana_sandbox_backend:SandboxBackend"
```

The value may be a backend object, its class (instantiated with no
arguments), or a function returning one.

## The backend protocol

| Member | What Monguana does with it |
|---|---|
| `name` | Stored in each profile (`connections.backend`). Unique; `mongodb` and `tinymongo` are taken. Renaming it orphans saved profiles. |
| `label` | Shown in the editor, the tree and messages ("*label* connections cannot …"). |
| `capabilities` | The optional features it supports, from `backends.CAPABILITIES`. Without one, Monguana falls back or refuses, and the UI hides it. An unknown name is a load error. |
| `fields()` | The editor's fields: dicts with `id`, `label`, and optionally `type` (`text`, `select`, `checkbox`, `number`, `password`), `options` (`[{value, label}]`), `value`, `placeholder`, `help`. The values arrive as the profile's `options`. |
| `public_options(options)` | What of `options` the editor may show again. **Never secrets**: this goes to the browser. |
| `validate(profile)` | `{field id: message}` for bad values; `{}` when fine. `profile["options"]` holds the fields. Runs on save and on Test. |
| `connect_options(profile)` | Everything that decides the connection, secrets included, JSON-able. The pool fingerprints it and builds a new client when it changes. |
| `open(options)` | A PyMongo-shaped client for those options. Called under the pool's lock: **must not block** on the network. |
| `test(options)` | Dial a throwaway client: `{"ok": True, "version": str, "ms": int}` or `{"ok": False, "error": str}`. |
| `create_collection(database, name)` | Optional: how to make an empty collection where `database.create_collection` does not exist. |

`options` are stored JSON, encrypted at rest like passwords. `save` keeps the
stored options when the editor sends none.

## What the client must do

Monguana calls the client the way it calls PyMongo: `list_databases`,
`list_collection_names`, `find` (with sort, skip, limit, projection),
`count_documents`, `aggregate`, `insert_one`/`insert_many`, `replace_one`,
`update_one`/`update_many`, `delete_one`/`delete_many`, `create_index`,
`list_indexes`, `drop_index`, `drop`, `drop_database`, `close`. Documents are
Python dicts of `bson` types (ObjectId, datetime, Decimal128, …). Declare only
the capabilities the client really has: `tests/test_live.py` and
`tests/test_tinymongo.py` show what each one exercises.

## Loading, and when it goes wrong

Plugins load once, with the built-ins, on first use. One that fails to import,
lacks a member, claims a taken name or an unknown capability is **left out**,
logged, and listed by:

```sh
python manage.py backends        # exits 1 when a plugin was left out
```

Profiles saved for a backend that is not installed stay in the list and say
so when opened.

**A plugin runs inside the Monguana server** with its access to every saved
profile's credentials, like any installed package. Install only what you
trust.

## Installing

- **Local run:** `uv pip install <package>` (or pip in the same environment).
- **Container:** set `MONGUANA_EXTRA_PACKAGES` in `.env`, then rebuild
  (`docker compose up -d --build`). It is a build argument, passed to pip.
- **Windows installer:** not supported; the bundle is built with a fixed set
  of packages.
