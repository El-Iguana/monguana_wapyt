"""
Monguana — browser UI.

Runs in Pyodide. Everything here is client-side Python; anything touching a
MongoDB server, a credential or the database of profiles goes through the BFF
services in ``services/``, whose implementations are stripped from what reaches
the browser.

Conventions carried from IguanaXterm, each learned the hard way there:

* **BFF calls use the generated ``*_async`` name.** ``MongoService().find()``
  is a blocking XHR; ``await MongoService().find_async(...)`` is the awaitable.
* **UI is built in ``load_ui``, never ``__init__``.** wapyt's metaclass calls
  ``load_ui()`` after construction; defining both renders everything twice.
* **JS ``null`` arrives as ``JsNull``, which is not ``None``.** Test DOM
  lookups for truthiness, never ``is None``.
* **Queries stay text in the browser.** The server parses them (``mql``), so
  ``ObjectId("…")`` and ``ISODate("…")`` work and nothing here needs ``bson``.
"""
from __future__ import annotations

import asyncio
import html
import json
import traceback
from urllib.parse import quote, urlencode

import js
from pyodide.ffi import create_proxy, to_js

from wapyt import (
    CellConfig,
    ColumnConfig,
    DataTable,
    DataTableConfig,
    FieldConfig,
    Form,
    FormConfig,
    LayoutConfig,
    MainWindow,
    message,
    ModalConfig,
    ModalWindow,
    SelectOption,
    TabConfig,
    TableAction,
    TabWidget,
    TabWidgetConfig,
    Tree,
    TreeAction,
    TreeConfig,
    TreeItem,
)

from services.about import LICENSE as ABOUT_LICENSE, RELEASES_URL, REPO, REPO_URL, VERSION, WIKI_URL
from services.about_service import AboutService
from services.connection_service import ConnectionService
from services.job_service import JobService
from services.docfmt import (
    apply_column_state,
    cell_text,
    compact,
    default_column_width,
    count_nodes,
    format_bytes,
    merge_column_state,
    page_summary,
    scalar_text,
    tagged_type,
    to_pretty,
    to_shell,
    type_label,
    union_columns,
)
from services.mongo_service import MongoService
from services.pipeline_text import PipelineTextError, compose as compose_pipeline
from services.pipeline_text import join as join_pipeline, split as split_pipeline
from services.querybuilder import (
    OPERATORS as QB_OPERATORS,
    VALUE_TYPES as QB_TYPES,
    BuilderError,
    build_filter,
    default_type,
    operators_for,
)
from services.ui_state_service import UiStateService
from services.user_service import UserService

# pytincture resolves the browser entrypoint by AST, and its MainWindow
# detection is hardcoded to dhxpyt's MainWindow — it never matches a wapyt
# base. Without this declaration the app starts with HTTP 422.
APP_ENTRYPOINT = "Monguana"

APP_FAVICON = "static/el_iguana_avatar.webp"

UNGROUPED = ""

# The CodeMirror bundle (ROADMAP phase 31), vendored under appcode/vendor and
# built by tools/codemirror/build.sh. Loaded the first time a view opens; if it
# cannot load, the plain text boxes stay and everything still works.
EDITOR_SRC = "/vendor/codemirror/monguana-editor.js"
EDITOR_LOAD_SECONDS = 10

PAGE_SIZES = (20, 50, 100, 200, 500)

# The dashboard polls every DASH_POLL_SECONDS while its tab is in front and
# keeps DASH_HISTORY samples for the sparklines (5 minutes).
DASH_POLL_SECONDS = 5
DASH_HISTORY = 60

# Throughput tiles: counter (serverStatus opcounters/network), label, bytes?
_RATE_TILES = (
    ("query", "Queries", False),
    ("getmore", "Getmores", False),
    ("insert", "Inserts", False),
    ("update", "Updates", False),
    ("delete", "Deletes", False),
    ("command", "Commands", False),
    ("numRequests", "Requests", False),
    ("bytesIn", "Network in", True),
    ("bytesOut", "Network out", True),
)

_PROFILE_LEVELS = {0: "Off", 1: "Slow operations", 2: "Everything"}
# What a profile filter can test: a profiler entry's fields, for completions.
_PROFILE_FIELDS = (
    ("op", "String"), ("ns", "String"), ("millis", "Int"), ("planSummary", "String"),
    ("docsExamined", "Int"), ("keysExamined", "Int"), ("nreturned", "Int"),
    ("nModified", "Int"), ("ndeleted", "Int"), ("ninserted", "Int"), ("nMatched", "Int"),
    ("responseLength", "Int"), ("numYield", "Int"), ("hasSortStage", "Boolean"),
    ("usedDisk", "Boolean"), ("appName", "String"), ("client", "String"), ("user", "String"),
    ("errCode", "Int"), ("queryHash", "String"), ("planCacheShapeHash", "String"),
    ("command", "Object"), ("command.find", "String"), ("command.aggregate", "String"),
    ("command.filter", "Object"), ("ts", "Date"),
)
# The profile filter dialog's starting points.
_PROFILE_FILTER_EXAMPLES = (
    ("Slower than 50 ms", "{millis: {$gte: 50}}"),
    ("Collection scans", '{planSummary: "COLLSCAN"}'),
    ("Reads only", '{op: {$in: ["query", "getmore"]}}'),
    ("Read 1,000+ documents", "{docsExamined: {$gte: 1000}}"),
    ("Sorted in memory", "{hasSortStage: true}"),
    ("One app", '{appName: "my-service"}'),
)
# system.profile's "op" values (mongo_service.PROFILE_OPS).
_PROFILE_OPS = ("query", "getmore", "insert", "update", "remove", "command")

# The document tree view: how deep, and how many nodes per page, before it
# stops and says so. The original froze the tab on a large nested document.
TREE_MAX_DEPTH = 10
TREE_MAX_NODES = 4000

# Row key for the table's stable id, and the key every column sorts by (see
# _render_table). Neither can collide with a real field in practice.
_ROW_KEY = "\u0001row"
_RANK_KEY = "\u0001rank"
# Row key prefix for a cell's type icon (the column's icon_by); "_title" on
# the same key is its tooltip.
_TYPE_KEY = "\u0001type:"

_MODES = (
    ("find", "find"),
    ("aggregate", "aggregate"),
    ("updateOne", "updateOne"),
    ("updateMany", "updateMany"),
    ("deleteOne", "deleteOne"),
    ("deleteMany", "deleteMany"),
)

_STAGE_TEMPLATES = (
    ("$match", '{$match: {\n  \n}}'),
    ("$group", '{$group: {\n  _id: "$field",\n  count: {$sum: 1}\n}}'),
    ("$project", '{$project: {\n  field: 1\n}}'),
    ("$sort", '{$sort: {field: -1}}'),
    ("$limit", '{$limit: 10}'),
    ("$skip", '{$skip: 0}'),
    ("$unwind", '{$unwind: "$field"}'),
    ("$lookup", '{$lookup: {\n  from: "other",\n  localField: "field",\n  foreignField: "_id",\n  as: "joined"\n}}'),
    ("$addFields", '{$addFields: {\n  newField: "$field"\n}}'),
    ("$count", '{$count: "total"}'),
    ("$sortByCount", '{$sortByCount: "$field"}'),
    ("$facet", '{$facet: {\n  first: [{$limit: 5}]\n}}'),
)

# Every stage the server's read-only allowlist accepts, for a card's selector:
# the templates first, then the rest.
_STAGE_OPS = tuple(name for name, _ in _STAGE_TEMPLATES) + (
    "$set", "$unset", "$replaceRoot", "$replaceWith", "$sample", "$bucket",
    "$bucketAuto", "$graphLookup", "$unionWith", "$redact", "$setWindowFields",
    "$densify", "$fill", "$geoNear",
)

_TOOLBAR_BUTTONS = (
    ("new_conn", "New connection", "mdi-plus-circle"),
    ("edit_conn", "Edit", "mdi-pencil"),
    ("delete_conn", "Delete", "mdi-delete"),
    ("|", "", ""),
    ("refresh", "Refresh", "mdi-refresh"),
    ("dashboard", "Dashboard", "mdi-view-dashboard-outline"),
    ("|", "", ""),
    ("users", "Users", "mdi-account-group"),
    ("password", "Password", "mdi-key"),
    ("shortcuts", "Shortcuts", "mdi-keyboard-outline"),
    ("about", "About", "mdi-information-outline"),
)

_TYPE_ICONS = {
    "ObjectId": "mdi-identifier",
    "String": "mdi-format-quote-close",
    "Int": "mdi-numeric",
    "Int32": "mdi-numeric",
    "Int64": "mdi-numeric",
    "Double": "mdi-decimal",
    "Decimal128": "mdi-decimal",
    "Boolean": "mdi-toggle-switch-outline",
    "Date": "mdi-calendar",
    "Null": "mdi-null",
    "Array": "mdi-code-brackets",
    "Object": "mdi-code-braces",
    "Binary": "mdi-file-outline",
    "UUID": "mdi-barcode",
    "Regex": "mdi-regex",
    "Timestamp": "mdi-clock-outline",
}


def _csrf_token() -> str:
    """pytincture's CSRF cookie, readable from script on purpose."""
    for part in str(js.document.cookie).split(";"):
        name, _, value = part.strip().partition("=")
        if "csrf" in name.lower():
            return str(js.decodeURIComponent(value))
    return ""


def _report(label: str) -> None:
    """``asyncio.ensure_future`` swallows exceptions; never let it do so silently."""
    js.console.error(f"[monguana] {label} failed:\n{traceback.format_exc()}")


def _spawn(coro, label: str):
    async def _guarded():
        try:
            await coro
        except Exception:  # noqa: BLE001 - last line of defence
            _report(label)

    return asyncio.ensure_future(_guarded())


def _role(element) -> str:
    """
    An element's ``data-role``, or "". Not ``element.dataset.role``: in Pyodide
    a missing JS property raises AttributeError where JS would give
    ``undefined`` — a plain stage box (no role) crashed the change handler.
    """
    value = element.getAttribute("data-role") if hasattr(element, "getAttribute") else None
    return str(value) if value else ""


def _el(element_id: str):
    return js.document.getElementById(element_id)


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _node_id(kind: str, *parts) -> str:
    """
    Deterministic tree ids, so the Tree keeps expansion across ``set_items``.
    Names are percent-encoded: a collection name may contain anything but $.
    """
    return ":".join([kind, *(quote(str(part), safe="") for part in parts)])


def _sort_value(value):
    """A primitive the table can order by, for types it cannot compare itself."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    tagged = tagged_type(value)
    if tagged in ("Int64", "Int32", "Double", "Decimal128"):
        try:
            return float(next(iter(value.values())))
        except (TypeError, ValueError):
            return 0
    if value is None:
        return None
    return cell_text(value, 200)


def _download(url: str) -> None:
    """Start a same-origin download without leaving the page."""
    anchor = js.document.createElement("a")
    anchor.href = url
    anchor.download = ""
    anchor.style.display = "none"
    js.document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()


class Monguana(MainWindow):
    # No __init__: the LoadUICaller metaclass calls load_ui() after
    # construction, and defining both builds the UI twice.

    def load_ui(self) -> None:
        self.set_theme("dark")
        js.document.title = "Monguana"

        self._conns: list[dict] = []
        self._dbs: dict[int, list] = {}              # conn id -> database rows
        self._colls: dict[tuple, list] = {}          # (conn id, db) -> collection rows
        self._errors: dict[str, str] = {}            # tree node id -> load error
        self._loading: set = set()
        self._selected: dict = {}                    # the tree node's data
        self._views: dict[str, dict] = {}            # tab id -> view state
        self._dashes: dict[str, dict] = {}           # tab id -> dashboard / profiler state
        self._tab_counter = 0
        self._me: dict = {}
        # Per-user settings that follow the person (UiStateService "settings").
        self._settings: dict = {"show_system": False}
        self._proxies: list = []
        # element id of the <textarea> an editor replaces -> {"editor", "host", "proxies"}
        self._editors: dict[str, dict] = {}
        self._editor_ready = None                    # future: did the bundle load?
        self._release: dict = {}                     # AboutService.latest(), once per load

        self._build_chrome()
        _spawn(self._load_identity(), "identity load")
        _spawn(self._reload_connections(), "connection list")
        _spawn(self._load_settings(), "settings")
        _spawn(self._check_release(), "release check")

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def _build_chrome(self) -> None:
        self.attach_html("mainwindow_header", self._toolbar_html())

        body = self.add_layout(
            "mainwindow",
            LayoutConfig(
                type="line",
                cols=[
                    CellConfig(id="sidebar", width="300px"),
                    CellConfig(id="workspace", width="100%"),
                ],
            ),
        )
        self.body = body

        sidebar = body.get_cell("sidebar")
        sidebar_el = sidebar.getContainer() if hasattr(sidebar, "getContainer") else sidebar
        sidebar_el.innerHTML = (
            '<div class="mg-sidebar">'
            '  <div class="mg-brand">'
            '    <img class="mg-brand-logo" src="/static/el_iguana_avatar.webp"'
            '         alt="" width="40" height="40" decoding="async">'
            '    <div class="mg-brand-text">'
            '      <span class="mg-brand-name">Monguana</span>'
            '      <span class="mg-brand-sub">MongoDB &middot; pytincture &middot; wapyt</span>'
            "    </div>"
            "  </div>"
            '  <div class="mg-sidebar-tree" id="mg-tree-host"></div>'
            "</div>"
        )

        server = ["server"]
        database = ["database"]
        collection = ["collection"]
        either = ["collection", "view"]
        # `requires` entries show only where the node's data["flags"] (its
        # connection's backend capabilities) has them: phase 39.
        self.tree = Tree(
            TreeConfig(
                filterable=True,
                filter_placeholder="Filter",
                empty_text="No connections yet.\nUse New connection to add one.",
                context_actions=[
                    TreeAction("connect", "Connect / refresh", "mdi-connection", kinds=server),
                    TreeAction("dashboard", "Dashboard", "mdi-view-dashboard-outline", kinds=server),
                    TreeAction("new_db", "Create database…", "mdi-database-plus", kinds=server,
                               requires=["create_collection"]),
                    TreeAction("restore", "Restore dump…", "mdi-backup-restore", kinds=server),
                    TreeAction("disconnect", "Disconnect", "mdi-lan-disconnect", kinds=server),
                    TreeAction(separator=True),
                    TreeAction("edit_conn", "Edit connection…", "mdi-pencil", kinds=server),
                    TreeAction("dup_conn", "Duplicate", "mdi-content-copy", kinds=server),
                    TreeAction("delete_conn", "Delete connection", "mdi-delete",
                               kinds=server, danger=True),
                    TreeAction("toggle_system", "Show / hide system collections",
                               "mdi-eye-settings-outline", kinds=server + database),
                    TreeAction("refresh_db", "Refresh", "mdi-refresh", kinds=database),
                    TreeAction("profiler", "Query profiler", "mdi-speedometer", kinds=database,
                               requires=["profiler"]),
                    TreeAction("new_coll", "Create collection…", "mdi-table-plus", kinds=database,
                               requires=["create_collection"]),
                    TreeAction("dump_db", "Dump database", "mdi-download", kinds=database),
                    TreeAction("copy_db", "Copy database to…", "mdi-content-duplicate",
                               kinds=database),
                    TreeAction("restore_db", "Restore into this database…",
                               "mdi-backup-restore", kinds=database),
                    TreeAction("drop_db", "Drop database…", "mdi-delete-forever",
                               kinds=database, danger=True),
                    TreeAction("open", "Open in new tab", "mdi-tab-plus", kinds=either),
                    TreeAction("indexes", "Indexes…", "mdi-key-chain", kinds=collection),
                    TreeAction("stats", "Statistics", "mdi-chart-box-outline", kinds=collection),
                    TreeAction("rename", "Rename…", "mdi-rename", kinds=collection,
                               requires=["rename"]),
                    TreeAction("dump_coll", "Dump collection", "mdi-download", kinds=collection),
                    TreeAction("copy_coll", "Copy collection to…", "mdi-content-duplicate",
                               kinds=collection),
                    TreeAction("drop_coll", "Drop…", "mdi-delete-forever",
                               kinds=either, danger=True),
                ],
            ),
            root="#mg-tree-host",
        )
        self.tree.on_select(self._on_tree_select)
        self.tree.on_activate(self._on_tree_activate)
        self.tree.on_toggle(self._on_tree_toggle)
        self.tree.on_action(self._on_tree_action)

        workspace = body.get_cell("workspace")
        workspace_el = workspace.getContainer() if hasattr(workspace, "getContainer") else workspace
        workspace_el.innerHTML = '<div class="mg-workspace" id="mg-tabs-host"></div>'
        self.tabs = TabWidget(TabWidgetConfig(tabs=[]), container=_el("mg-tabs-host"))
        self.tabs.on_close(self._on_tab_close)

        style = js.document.createElement("style")
        style.textContent = _CSS
        js.document.head.appendChild(style)

        self._listen("click", self._on_click)
        self._listen("change", self._on_change)
        self._listen("keydown", self._on_keydown)
        self._listen("input", self._on_input)

    def _listen(self, event: str, handler) -> None:
        proxy = create_proxy(handler)
        self._proxies.append(proxy)
        js.document.addEventListener(event, proxy)

    def _toolbar_html(self) -> str:
        parts = ['<div class="mg-toolbar">']
        for key, label, icon in _TOOLBAR_BUTTONS:
            if key == "|":
                parts.append('<span class="mg-toolbar-sep"></span>')
                continue
            parts.append(
                f'<button type="button" class="mg-toolbar-btn" data-top="{key}" title="{label}">'
                f'<span class="mdi {icon}"></span><span>{label}</span></button>'
            )
        parts.append('<span class="mg-toolbar-spacer"></span>')
        # Shown by _check_release when a newer release is out; opens About.
        parts.append(
            '<button type="button" class="mg-update" data-top="about" id="mg-update" hidden>'
            '<span class="mdi mdi-arrow-up-circle"></span><span id="mg-update-text"></span></button>'
        )
        parts.append('<span class="mg-toolbar-user" id="mg-user"></span>')
        parts.append(
            '<button type="button" class="mg-toolbar-btn" data-top="logout" title="Sign out">'
            '<span class="mdi mdi-logout"></span><span>Logout</span></button>'
        )
        parts.append("</div>")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Delegated DOM events
    # ------------------------------------------------------------------

    def _on_click(self, event) -> None:
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        top = target.closest("[data-top]")
        if top:
            self._on_toolbar(str(top.dataset.top))
            return
        button = target.closest("[data-mg]")
        if not button or button.disabled:
            return
        dash_el = button.closest(".mg-dash")
        if dash_el:
            tid = str(dash_el.getAttribute("data-tab"))
            if tid in self._dashes:
                self._dash_action(tid, str(button.getAttribute("data-mg")), button)
            return
        view_el = button.closest(".mg-view")
        if not view_el:
            return
        tid = str(view_el.dataset.tab)
        if tid in self._views:
            self._on_view_action(tid, str(button.dataset.mg), button)

    def _on_input(self, event) -> None:
        """Typing in a builder row or a plain stage box: keep state current."""
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        if target.hasAttribute("data-stage-body"):
            view_el = target.closest(".mg-view")
            if view_el and str(view_el.dataset.tab) in self._views:
                self._stage_set(str(view_el.dataset.tab), int(target.getAttribute("data-stage-body")),
                                body=str(target.value), rerender=False)
            return
        if not target.closest("[data-qb]"):
            return
        view_el = target.closest(".mg-view")
        if view_el and str(view_el.dataset.tab) in self._views:
            self._qb_read(str(view_el.dataset.tab), target, rerender=False)

    def _on_change(self, event) -> None:
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        dash_el = target.closest(".mg-dash")
        if dash_el:
            self._dash_change(str(dash_el.getAttribute("data-tab")), target)
            return
        view_el = target.closest(".mg-view")
        if not view_el:
            return
        tid = str(view_el.dataset.tab)
        view = self._views.get(tid)
        if view is None:
            return
        if target.hasAttribute("data-stage-op"):
            self._stage_set(tid, int(target.getAttribute("data-stage-op")), op=str(target.value))
            return
        if target.closest("[data-qb]"):
            # A field chosen, or an operator or type changed: the row's
            # choices may change with it, so it is drawn again.
            self._qb_read(tid, target, rerender=True)
            return
        role = _role(target)
        if role == "mode":
            self._apply_mode(tid, str(target.value))
        elif role == "size":
            view["page_size"] = int(target.value)
            view["page"] = 1
            _spawn(self._run(tid), "page size")
        elif role == "stage":
            self._insert_stage(tid, str(target.value))
            target.value = ""

    def _on_keydown(self, event) -> None:
        target = event.target
        if not target or not hasattr(target, "closest"):
            return
        key = str(event.key)

        # Two spaces, not a focus change, inside the multi-line code boxes.
        if key == "Tab" and target.classList.contains("mg-tabbable") and not event.shiftKey:
            event.preventDefault()
            start, end = target.selectionStart, target.selectionEnd
            value = str(target.value)
            target.value = value[:start] + "  " + value[end:]
            target.selectionStart = target.selectionEnd = start + 2
            return

        view_el = target.closest(".mg-view")
        if view_el:
            # An editor that acted on the key (run, completion, newline) has
            # already prevented its default; running again here would double it.
            if event.defaultPrevented:
                return
            tid = str(view_el.dataset.tab)
            if key == "Enter" and (event.ctrlKey or event.metaKey):
                event.preventDefault()
                if tid in self._views:
                    self._views[tid]["page"] = 1
                    _spawn(self._run(tid), "run")
                return
            if key == "Enter" and _role(target) == "page":
                event.preventDefault()
                self._goto_page(tid, str(target.value))
                return
            # Enter in a builder row applies the builder.
            if key == "Enter" and target.closest("[data-qb]") and tid in self._views:
                event.preventDefault()
                self._qb_action(tid, "qb_apply", target)
                return
            # A one-line filter box: Enter runs, Shift+Enter adds a line.
            if key == "Enter" and not event.shiftKey and _role(target) in (
                "filter", "sort", "projection"
            ):
                event.preventDefault()
                if tid in self._views:
                    self._views[tid]["page"] = 1
                    _spawn(self._run(tid), "run")
                return

        # Single-key shortcuts, only when not typing into something.
        tag = str(target.tagName or "").lower()
        if tag in ("input", "textarea", "select") or target.isContentEditable:
            return
        if event.ctrlKey or event.metaKey or event.altKey:
            return
        if any(
            str(overlay.style.display) == "flex"
            for overlay in js.document.querySelectorAll(".wapyt-modal-overlay")
        ):
            return
        active = self._active_view()
        if key == "?":
            self._shortcuts_dialog()
        elif active and key in ("n", "N"):
            _spawn(self._insert_document(active), "insert")
        elif active and key in ("i", "I"):
            view = self._views[active]
            _spawn(self._indexes_dialog(view["conn"], view["db"], view["coll"]), "indexes")
        elif active and key == "/":
            event.preventDefault()
            self._focus_field(f"{active}-filter")

    def _active_view(self) -> str | None:
        active = self.tabs.get_active()
        return active if active in self._views else None

    # ------------------------------------------------------------------
    # Identity and connections
    # ------------------------------------------------------------------

    async def _load_settings(self) -> None:
        result = await UiStateService().get_async("settings")
        if result.get("ok") and result.get("value"):
            self._settings.update(result["value"])
            self._rebuild_tree()

    async def _toggle_system_collections(self) -> None:
        self._settings["show_system"] = not self._settings.get("show_system")
        self._rebuild_tree()
        shown = self._settings["show_system"]
        self._toast(f"System collections are {'shown' if shown else 'hidden'}.")
        await UiStateService().set_async("settings", self._settings)

    async def _load_identity(self) -> None:
        self._me = await UserService().me_async()
        label = _el("mg-user")
        if label:
            suffix = " · admin" if self._me.get("is_admin") else ""
            label.textContent = f"{self._me.get('username', '')}{suffix}"
        if self._me.get("must_change_password"):
            self._password_nag()

    def _password_nag(self) -> None:
        """
        Shown on every load while the password is one this person did not
        choose: the install default (change_me) or one an administrator set.
        """
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Change your password",
                                        width=480, height=250))
        modal.body.innerHTML = (
            '<div class="mg-nag">'
            '<p><span class="mdi mdi-shield-alert-outline"></span> '
            "You are still using a password you did not choose: the default "
            "<code>change_me</code>, or one an administrator set for you. Anyone "
            "who knows it can sign in as you and use your saved connections.</p>"
            "<p>Monguana will ask again each time it loads until you change it.</p>"
            '<div class="mg-editor-actions">'
            '<button type="button" class="mg-btn" data-nag="later">Later</button>'
            '<button type="button" class="mg-btn mg-primary" data-nag="change">'
            '<span class="mdi mdi-key"></span><span>Change password</span></button>'
            "</div></div>"
        )

        def _on_click(event) -> None:
            which = event.target.closest("[data-nag]")
            if not which:
                return
            modal.close()
            if str(which.getAttribute("data-nag")) == "change":
                self._password_dialog()

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()
        focus = modal.body.querySelector('[data-nag="change"]')
        if focus:
            focus.focus()

    async def _reload_connections(self) -> None:
        self._conns = await ConnectionService().list_async()
        known = {conn["id"] for conn in self._conns}
        for conn_id in list(self._dbs):
            if conn_id not in known:
                self._forget(conn_id)
        self._rebuild_tree()

    def _conn(self, conn_id) -> dict | None:
        try:
            wanted = int(conn_id)
        except (TypeError, ValueError):
            return None
        return next((conn for conn in self._conns if conn["id"] == wanted), None)

    def _caps(self, conn_id) -> list:
        """What this connection's backend supports (``backends.CAPABILITIES``)."""
        return list((self._conn(conn_id) or {}).get("capabilities") or [])

    def _forget(self, conn_id: int) -> None:
        self._dbs.pop(conn_id, None)
        for key in [key for key in self._colls if key[0] == conn_id]:
            self._colls.pop(key, None)

    # ------------------------------------------------------------------
    # The tree
    # ------------------------------------------------------------------

    def _rebuild_tree(self) -> None:
        folders: dict[str, list] = {}
        for conn in self._conns:
            folders.setdefault(conn.get("folder") or UNGROUPED, []).append(conn)

        items: list = []
        for folder in sorted(folders, key=str.lower):
            nodes = [self._server_item(conn) for conn in folders[folder]]
            if folder == UNGROUPED:
                items.extend(nodes)
            else:
                items.insert(0, TreeItem(
                    id=_node_id("f", folder), label=folder, badge=len(nodes),
                    items=nodes, data={"kind": "folder"},
                ))
        self.tree.set_items(items)

    def _server_item(self, conn: dict) -> TreeItem:
        conn_id = conn["id"]
        node = _node_id("c", conn_id)
        options = conn.get("options") or {}
        is_mongo = (conn.get("backend") or "mongodb") == "mongodb"
        if not is_mongo:
            where = " · ".join(part for part in (
                conn.get("backend_label", ""), options.get("engine", ""),
                options.get("folder") or "(root)") if part)
        elif conn.get("has_uri"):
            where = "connection string"
        else:
            where = f"{conn.get('host', '')}:{conn.get('port', '')}"
        tooltip = f"{conn['name']}\n{where}" + (f"\n{conn['notes']}" if conn.get("notes") else "")
        if conn_id in self._dbs:
            children = [self._db_item(conn_id, row) for row in self._dbs[conn_id]]
            if not children:
                children = [self._note_item(node, "No databases visible")]
        else:
            children = [self._note_item(node, self._errors.get(node) or (
                "Connecting…" if node in self._loading else "Expand to connect"))]
        return TreeItem(
            id=node, label=conn["name"],
            icon="mdi-server" if is_mongo else "mdi-folder-table",
            open_icon="mdi-server-network" if is_mongo else "mdi-folder-table-outline",
            tooltip=tooltip, items=children,
            data={"kind": "server", "conn": conn_id, "flags": self._caps(conn_id)},
        )

    def _db_item(self, conn_id: int, row: dict) -> TreeItem:
        name = row["name"]
        node = _node_id("d", conn_id, name)
        colls = self._colls.get((conn_id, name))
        if colls is not None and not self._settings.get("show_system"):
            # system.views, system.profile, system.js…: MongoDB's own
            # bookkeeping. Hidden unless asked for (right-click the server).
            colls = [coll for coll in colls if not coll.get("system")]
        if colls is None:
            children = [self._note_item(node, self._errors.get(node) or (
                "Loading…" if node in self._loading else "Expand to list collections"))]
        else:
            children = [self._coll_item(conn_id, name, coll) for coll in colls] or [
                self._note_item(node, "No collections")
            ]
        size = format_bytes(row.get("size")) if row.get("size") else ""
        return TreeItem(
            id=node, label=name, icon="mdi-database", open_icon="mdi-database-outline",
            badge=len(colls) if colls is not None else None,
            tooltip=f"{name}" + (f"\n{size} on disk" if size else ""),
            items=children,
            data={"kind": "database", "conn": conn_id, "db": name, "flags": self._caps(conn_id)},
        )

    def _coll_item(self, conn_id: int, db: str, row: dict) -> TreeItem:
        kind = "view" if row.get("type") == "view" else "collection"
        icon = {
            "view": "mdi-eye-outline",
            "timeseries": "mdi-chart-timeline-variant",
        }.get(row.get("type"), "mdi-table")
        return TreeItem(
            id=_node_id("k", conn_id, db, row["name"]),
            label=row["name"], icon=icon,
            tooltip=f"{db}.{row['name']}" + (f" ({row.get('type')})" if kind == "view" else ""),
            data={"kind": kind, "conn": conn_id, "db": db, "coll": row["name"],
                  "flags": self._caps(conn_id)},
        )

    @staticmethod
    def _note_item(parent: str, text: str) -> TreeItem:
        return TreeItem(
            id=f"{parent}:note", label=text, icon="mdi-information-outline",
            data={"kind": "note"},
        )

    @staticmethod
    def _payload_data(payload: dict) -> dict:
        node = payload.get("node") or {}
        return dict(node.get("data") or {})

    def _on_tree_select(self, payload: dict) -> None:
        self._selected = self._payload_data(payload)

    def _on_tree_toggle(self, payload: dict) -> None:
        if not payload.get("expanded"):
            return
        node_id = str(payload.get("id") or "")
        data = dict((self.tree.get_node(node_id) or {}).get("data") or {})
        kind = data.get("kind")
        if kind == "server" and data["conn"] not in self._dbs:
            _spawn(self._load_databases(data["conn"]), "databases")
        elif kind == "database" and (data["conn"], data["db"]) not in self._colls:
            _spawn(self._load_collections(data["conn"], data["db"]), "collections")

    def _on_tree_activate(self, payload: dict) -> None:
        data = self._payload_data(payload)
        if data.get("kind") in ("collection", "view"):
            self._open_view(data["conn"], data["db"], data["coll"], data["kind"])

    async def _load_databases(self, conn_id: int) -> None:
        node = _node_id("c", conn_id)
        self._loading.add(node)
        self._errors.pop(node, None)
        self._rebuild_tree()
        try:
            result = await MongoService().databases_async(conn_id)
        finally:
            self._loading.discard(node)
        if result.get("ok"):
            self._dbs[conn_id] = result["databases"]
            for conn in self._conns:
                if conn["id"] == conn_id:
                    conn["open"] = True
        else:
            self._dbs.pop(conn_id, None)
            self._errors[node] = f"⚠ {result.get('error', 'Could not connect')}"
            self._toast(result.get("error", "Could not connect"), kind="error")
        self._rebuild_tree()

    async def _load_collections(self, conn_id: int, db: str) -> None:
        node = _node_id("d", conn_id, db)
        self._loading.add(node)
        self._errors.pop(node, None)
        self._rebuild_tree()
        try:
            result = await MongoService().collections_async(conn_id, db)
        finally:
            self._loading.discard(node)
        if result.get("ok"):
            self._colls[(conn_id, db)] = result["collections"]
        else:
            self._colls.pop((conn_id, db), None)
            self._errors[node] = f"⚠ {result.get('error', 'Could not list')}"
        self._rebuild_tree()

    async def _refresh_server(self, conn_id: int) -> None:
        self._forget(conn_id)
        self.tree.expand(_node_id("c", conn_id))
        await self._load_databases(conn_id)
        # Reload what was open, so a refresh does not collapse the tree.
        expanded = set(self.tree.get_expanded())
        for row in self._dbs.get(conn_id, []):
            if _node_id("d", conn_id, row["name"]) in expanded:
                await self._load_collections(conn_id, row["name"])

    def _on_tree_action(self, payload: dict) -> None:
        data = self._payload_data(payload)
        self._selected = data
        action = str(payload.get("action") or "")
        conn_id = data.get("conn")
        db = data.get("db")
        coll = data.get("coll")
        handlers = {
            "connect": lambda: self._refresh_server(conn_id),
            "disconnect": lambda: self._disconnect(conn_id),
            "new_db": lambda: self._create_database_dialog(conn_id),
            "restore": lambda: self._restore_dialog(conn_id, ""),
            "edit_conn": lambda: self._connection_editor(conn_id),
            "dup_conn": lambda: self._duplicate_connection(conn_id),
            "delete_conn": lambda: self._delete_connection(conn_id),
            "refresh_db": lambda: self._load_collections(conn_id, db),
            "toggle_system": lambda: self._toggle_system_collections(),
            "new_coll": lambda: self._create_collection_dialog(conn_id, db),
            "restore_db": lambda: self._restore_dialog(conn_id, db),
            "drop_db": lambda: self._drop_database(conn_id, db),
            "indexes": lambda: self._indexes_dialog(conn_id, db, coll),
            "stats": lambda: self._stats_dialog(conn_id, db, coll),
            "rename": lambda: self._rename_collection(conn_id, db, coll),
            "drop_coll": lambda: self._drop_collection(conn_id, db, coll),
            "copy_db": lambda: self._copy_dialog(conn_id, db, ""),
            "copy_coll": lambda: self._copy_dialog(conn_id, db, coll),
        }
        if action == "open":
            self._open_view(conn_id, db, coll, data.get("kind", "collection"))
        elif action == "dashboard":
            self._open_dashboard(conn_id)
        elif action == "profiler":
            self._open_profiler(conn_id, db)
        elif action == "dump_db":
            _spawn(self._start_dump(conn_id, db, ""), "dump database")
        elif action == "dump_coll":
            _spawn(self._start_dump(conn_id, db, coll), "dump collection")
        elif action in handlers:
            _spawn(handlers[action](), action)

    def _on_toolbar(self, action: str) -> None:
        conn_id = self._selected.get("conn")
        if action == "logout":
            _spawn(self._logout(), "logout")
        elif action == "new_conn":
            _spawn(self._connection_editor(None), "connection editor")
        elif action == "users":
            _spawn(self._admin_panel(), "admin panel")
        elif action == "password":
            self._password_dialog()
        elif action == "shortcuts":
            self._shortcuts_dialog()
        elif action == "about":
            self._about_dialog()
        elif action == "refresh":
            _spawn(self._refresh_all(), "refresh")
        elif not conn_id:
            self._toast("Select a connection first.", kind="warning")
        elif action == "edit_conn":
            _spawn(self._connection_editor(conn_id), "connection editor")
        elif action == "dashboard":
            self._open_dashboard(conn_id)
        elif action == "delete_conn":
            _spawn(self._delete_connection(conn_id), "connection delete")

    async def _refresh_all(self) -> None:
        await self._reload_connections()
        for conn_id in list(self._dbs):
            await self._refresh_server(conn_id)

    async def _disconnect(self, conn_id: int) -> None:
        await ConnectionService().disconnect_async(conn_id)
        self._forget(conn_id)
        self.tree.collapse(_node_id("c", conn_id))
        self._rebuild_tree()

    async def _logout(self) -> None:
        """pytincture's logout is a POST that needs the CSRF header, hence fetch."""
        application = str(js.window.location.pathname).strip("/").split("/")[0]
        options = js.Object.new()
        options.method = "POST"
        options.credentials = "same-origin"
        headers = js.Object.new()
        setattr(headers, "X-CSRF-Token", _csrf_token())
        options.headers = headers
        try:
            response = await js.fetch(f"/{application}/auth/logout", options)
        except Exception:  # noqa: BLE001
            self._toast("Could not sign out.", kind="error")
            return
        if not response.ok:
            self._toast("Could not sign out.", kind="error")
            return
        js.window.location.assign(f"/{application}/login")

    # ------------------------------------------------------------------
    # Connection profiles
    # ------------------------------------------------------------------

    async def _connection_editor(self, conn_id: int | None, backend: str = "",
                                 carry: dict | None = None) -> None:
        """
        New or edit. MongoDB has its own fields; another backend's come from
        ``ConnectionService.backends`` (phase 39). Choosing another backend in
        a new connection reopens the editor for it, keeping ``carry``.
        """
        existing: dict = {}
        if conn_id:
            existing = await ConnectionService().get_async(conn_id)
            if not existing:
                self._toast("Connection not found.", kind="error")
                return
        existing = {**existing, **(carry or {})}
        available = await ConnectionService().backends_async()
        chosen = existing.get("backend") or backend or "mongodb"
        spec = next((item for item in available if item["name"] == chosen), None)
        if spec is None:
            self._toast(f"The {chosen!r} backend is not installed.", kind="error")
            return
        is_mongo = spec["fields"] is None
        stored_options = existing.get("options") or {}

        title = "Edit connection" if conn_id else "New connection"
        if not is_mongo:
            title += f" — {spec['label']}"
        modal = ModalWindow(ModalConfig(
            dispose_on_close=True, title=title, width=620, height=780 if is_mongo else 640,
        ))
        host = js.document.createElement("div")
        host.className = "mg-dialog"
        host.innerHTML = (
            '<div class="mg-dialog-form"></div>'
            '<div class="mg-test-row">'
            '  <button type="button" class="mg-btn" data-test><span class="mdi mdi-lan-connect"></span>'
            "  <span>Test connection</span></button>"
            '  <span class="mg-test-result" aria-live="polite"></span>'
            "</div>"
        )
        modal.body.appendChild(host)

        has_password = existing.get("has_password")
        has_uri = existing.get("has_uri")
        head = [FieldConfig(id="name", label="Name", required=True,
                            value=existing.get("name", ""), span=2)]
        if not conn_id and len(available) > 1:
            head.append(FieldConfig(
                id="backend", label="Connects to", type="select", value=chosen, span=2,
                options=[SelectOption(item["name"], item["label"]) for item in available],
            ))
        if is_mongo:
            body = [
                FieldConfig(id="host", label="Host",
                            value=existing.get("host", "localhost" if not conn_id else "")),
                FieldConfig(id="port", label="Port", type="number", min=1, max=65535,
                            value=existing.get("port", 27017)),
                FieldConfig(id="username", label="Username", autocomplete="off",
                            value=existing.get("username", "")),
                FieldConfig(id="password", label="Password", type="password",
                            autocomplete="new-password",
                            placeholder="•••••• (stored)" if has_password else "",
                            help="Blank keeps the stored password." if has_password else None),
                FieldConfig(id="auth_source", label="Auth database",
                            value=existing.get("auth_source", "admin")),
                FieldConfig(id="default_db", label="Default database",
                            value=existing.get("default_db", ""),
                            help="Shown when the user may not list databases."),
                FieldConfig(id="tls", label="TLS", type="checkbox",
                            value=bool(existing.get("tls"))),
                FieldConfig(id="direct", label="Direct connection", type="checkbox",
                            value=bool(existing.get("direct", True))),
                FieldConfig(id="uri", label="Connection string", span=2,
                            autocomplete="off",
                            placeholder="•••••• (stored)" if has_uri
                            else "mongodb+srv://user:pass@cluster.example.net/",
                            help=("Stored. Blank keeps it; type a single space to clear it. "
                                  if has_uri else "")
                            + "When set, it replaces every field above."),
            ]
        else:
            body = [
                FieldConfig(
                    id=f"opt_{field['id']}", label=field["label"],
                    type=field.get("type", "text"), span=2,
                    value=stored_options.get(field["id"], field.get("value", "")),
                    placeholder=field.get("placeholder"), help=field.get("help"),
                    options=[SelectOption(option["value"], option["label"])
                             for option in field["options"]] if field.get("options") else None,
                )
                for field in spec["fields"]
            ]
        tail = [
            FieldConfig(id="folder", label="Folder", value=existing.get("folder", ""),
                        placeholder="(none)"),
            FieldConfig(id="notes", label="Notes", value=existing.get("notes", "")),
        ]
        form = Form(
            FormConfig(
                columns=2,
                submit_text="Save",
                cancel_text="Cancel",
                fields=head + body + tail,
            ),
            container=host.querySelector(".mg-dialog-form"),
        )
        form.on_cancel(lambda _payload: modal.close())

        def _on_change(payload: dict) -> None:
            if payload.get("id") != "backend" or payload.get("value") in (None, chosen):
                return
            values = form.get_values()
            keep = {key: values.get(key) or "" for key in ("name", "folder", "notes")}
            modal.close()
            _spawn(self._connection_editor(None, str(payload["value"]), keep), "connection editor")

        form.on_change(_on_change)

        def _payload(values: dict) -> dict:
            if not is_mongo:
                return {
                    "backend": chosen,
                    "options": {field["id"]: values.get(f"opt_{field['id']}") or ""
                                for field in spec["fields"]},
                }
            payload = {
                "backend": "mongodb",
                "host": values.get("host") or "",
                "port": int(values.get("port") or 27017),
                "username": values.get("username") or "",
                "auth_source": values.get("auth_source") or "admin",
                "tls": bool(values.get("tls")),
                "direct": bool(values.get("direct")),
            }
            # Blank secret fields mean "unchanged" when one is stored, and the
            # service's sentinel default expresses that by omission.
            if values.get("password"):
                payload["password"] = values["password"]
            elif not has_password:
                payload["password"] = ""
            uri = values.get("uri") or ""
            if uri.strip():
                payload["uri"] = uri.strip()
            elif uri or not has_uri:
                payload["uri"] = ""
            return payload

        result_el = host.querySelector(".mg-test-result")

        async def _test() -> None:
            result_el.textContent = "Testing…"
            result_el.dataset.state = "busy"
            payload = _payload(form.get_values())
            result = await ConnectionService().test_async(conn_id=conn_id, **payload)
            if result.get("ok"):
                version = result.get("version") or "Connected"
                if is_mongo and result.get("version"):
                    version = f"MongoDB {version}"
                result_el.textContent = f"✓ {version} · {result.get('ms', 0)} ms"
                result_el.dataset.state = "ok"
            else:
                result_el.textContent = f"✗ {result.get('error', 'Failed')}"
                result_el.dataset.state = "fail"

        test_proxy = create_proxy(lambda _event: _spawn(_test(), "connection test"))
        self._proxies.append(test_proxy)
        host.querySelector("[data-test]").addEventListener("click", test_proxy)

        async def _save(values: dict) -> None:
            form.set_busy(True)
            try:
                result = await ConnectionService().save_async(
                    conn_id=conn_id,
                    name=values.get("name", ""),
                    folder=values.get("folder") or "",
                    default_db=values.get("default_db") or "",
                    notes=values.get("notes") or "",
                    **_payload(values),
                )
                if not result.get("ok"):
                    if result.get("errors"):
                        # A backend names its own fields; the form prefixes them.
                        own = set() if is_mongo else {field["id"] for field in spec["fields"]}
                        form.set_errors({(f"opt_{key}" if key in own else key): text
                                         for key, text in result["errors"].items()})
                    else:
                        form.set_error(None, result.get("error", "Could not save"))
                    return
                modal.close()
                saved_id = result["id"]
                self._forget(saved_id)
                await self._reload_connections()
                if conn_id:
                    self.tree.collapse(_node_id("c", saved_id))
            finally:
                form.set_busy(False)

        form.on_submit(lambda values: _spawn(_save(values), "connection save"))
        modal.show()
        form.focus_first()

    async def _duplicate_connection(self, conn_id: int) -> None:
        result = await ConnectionService().duplicate_async(conn_id)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not duplicate"), kind="error")
            return
        await self._reload_connections()

    async def _delete_connection(self, conn_id: int) -> None:
        conn = self._conn(conn_id)
        if conn is None:
            return
        if not await message.confirm(
            f"Delete the connection “{conn['name']}”?\n\nThe server is not touched.",
            title="Delete connection", ok_text="Delete", danger=True,
        ):
            return
        result = await ConnectionService().delete_async(conn_id)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not delete"), kind="error")
            return
        for tid in [tid for tid, view in self._views.items() if view["conn"] == conn_id]:
            self._close_view(tid)
        for tid in [tid for tid, dash in self._dashes.items() if dash["conn"] == conn_id]:
            self._close_dash(tid)
        self._selected = {}
        self._forget(conn_id)
        await self._reload_connections()

    # ------------------------------------------------------------------
    # Databases and collections
    # ------------------------------------------------------------------

    async def _create_database_dialog(self, conn_id: int) -> None:
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Create database", width=460, height=330))
        form = Form(
            FormConfig(
                submit_text="Create", cancel_text="Cancel",
                fields=[
                    FieldConfig(id="db", label="Database name", required=True),
                    FieldConfig(id="collection", label="First collection", required=True,
                                help="MongoDB creates a database with its first collection."),
                ],
            ),
            container=modal.body,
        )
        form.on_cancel(lambda _payload: modal.close())

        async def _create(values: dict) -> None:
            form.set_busy(True)
            try:
                result = await MongoService().create_database_async(
                    conn_id, values["db"], values["collection"]
                )
                if not result.get("ok"):
                    form.set_error(None, result.get("error", "Could not create"))
                    return
                modal.close()
                await self._refresh_server(conn_id)
                self.tree.expand(_node_id("d", conn_id, result["db"]))
                await self._load_collections(conn_id, result["db"])
            finally:
                form.set_busy(False)

        form.on_submit(lambda values: _spawn(_create(values), "create database"))
        modal.show()
        form.focus_first()

    async def _drop_database(self, conn_id: int, db: str) -> None:
        typed = await message.prompt(
            f"Drop database “{db}” and every collection in it?\n\n"
            "This cannot be undone. Type the database name to confirm:",
            title="Drop database", ok_text="Drop", placeholder=db,
        )
        if typed is None or not typed:
            return
        if str(typed) != db:
            self._toast("The name did not match; nothing was dropped.", kind="warning")
            return
        result = await MongoService().drop_database_async(conn_id, db)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not drop"), kind="error")
            return
        for tid in [tid for tid, v in self._views.items() if v["conn"] == conn_id and v["db"] == db]:
            self._close_view(tid)
        self._toast(f"Dropped {db}.", kind="success")
        await self._refresh_server(conn_id)

    async def _create_collection_dialog(self, conn_id: int, db: str) -> None:
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Create collection in {db}", width=480, height=420))
        fields = [FieldConfig(id="name", label="Collection name", required=True, span=2)]
        if "capped" in self._caps(conn_id):
            fields += [
                FieldConfig(id="capped", label="Capped", type="checkbox", span=2),
                FieldConfig(id="size", label="Size (bytes)", type="number", min=0,
                            help="Required when capped."),
                FieldConfig(id="max_docs", label="Max documents", type="number", min=0),
            ]
        form = Form(
            FormConfig(
                submit_text="Create", cancel_text="Cancel", columns=2,
                fields=fields,
            ),
            container=modal.body,
        )
        form.on_cancel(lambda _payload: modal.close())

        async def _create(values: dict) -> None:
            form.set_busy(True)
            try:
                result = await MongoService().create_collection_async(
                    conn_id, db, values["name"], bool(values.get("capped")),
                    int(values.get("size") or 0), int(values.get("max_docs") or 0),
                )
                if not result.get("ok"):
                    form.set_error(None, result.get("error", "Could not create"))
                    return
                modal.close()
                self.tree.expand(_node_id("d", conn_id, db))
                await self._load_collections(conn_id, db)
            finally:
                form.set_busy(False)

        form.on_submit(lambda values: _spawn(_create(values), "create collection"))
        modal.show()
        form.focus_first()

    async def _rename_collection(self, conn_id: int, db: str, coll: str) -> None:
        new_name = await message.prompt(
            f"Rename {db}.{coll} to:", title="Rename collection", value=coll, ok_text="Rename",
        )
        if not new_name or str(new_name) == coll:
            return
        result = await MongoService().rename_collection_async(conn_id, db, coll, str(new_name))
        if not result.get("ok"):
            self._toast(result.get("error", "Could not rename"), kind="error")
            return
        for tid, view in self._views.items():
            if (view["conn"], view["db"], view["coll"]) == (conn_id, db, coll):
                view["coll"] = result["new"]
                head = _el(f"{tid}-coll")
                if head:
                    head.textContent = result["new"]
        await self._load_collections(conn_id, db)

    async def _drop_collection(self, conn_id: int, db: str, coll: str) -> None:
        if not await message.confirm(
            f"Drop {db}.{coll} and all its documents and indexes?\n\nThis cannot be undone.",
            title="Drop collection", ok_text="Drop", danger=True,
        ):
            return
        result = await MongoService().drop_collection_async(conn_id, db, coll)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not drop"), kind="error")
            return
        for tid in [tid for tid, v in self._views.items()
                    if (v["conn"], v["db"], v["coll"]) == (conn_id, db, coll)]:
            self._close_view(tid)
        await self._load_collections(conn_id, db)

    async def _stats_dialog(self, conn_id: int, db: str, coll: str) -> None:
        result = await MongoService().stats_async(conn_id, db, coll)
        if not result.get("ok"):
            self._toast(result.get("error", "No statistics"), kind="error")
            return
        def size(key: str) -> str:
            # None: the backend has no $collStats, so no sizes (phase 39).
            return "—" if result.get(key) is None else format_bytes(result[key])

        rows = [
            ("Documents", f"{result['count']:,}"),
            ("Data size", size("size")),
            ("Average document", size("avg_obj_size")),
            ("Storage size", size("storage_size")),
            ("Index size", size("total_index_size")),
            ("Capped", "yes" if result.get("capped") else "no"),
        ] + [(f"  {name}", format_bytes(bytes_)) for name, bytes_ in result["index_sizes"].items()]
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"{db}.{coll}", width=440, height=420))
        modal.body.innerHTML = '<table class="mg-kv">' + "".join(
            f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>" for label, value in rows
        ) + "</table>"
        modal.show()

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------

    async def _indexes_dialog(self, conn_id: int, db: str, coll: str) -> None:
        caps = self._caps(conn_id)
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Indexes — {db}.{coll}", width=880, height=680))
        modal.body.innerHTML = (
            '<div class="mg-split">'
            '  <div class="mg-split-top" data-slot="table"></div>'
            '  <div class="mg-hint">Double-click an index, or right-click it, to edit, hide or drop it.</div>'
            '  <div class="mg-split-bottom-auto" data-slot="form"></div>'
            "</div>"
        )
        indexes: dict[str, dict] = {}
        table = DataTable(
            DataTableConfig(
                columns=[
                    ColumnConfig(id="name", header="Name", width=170),
                    ColumnConfig(id="keys_shown", header="Keys"),
                    ColumnConfig(id="flags", header="Options", width=260),
                    ColumnConfig(id="size", header="Size", width=80, align="right",
                                 sort_by="size_bytes"),
                ],
                id_field="name",
                selection="single",
                empty_text="No indexes",
                context_actions=[
                    TableAction("edit", "Edit…", "mdi-pencil"),
                    *([TableAction("hide", "Hide / unhide", "mdi-eye-off-outline")]
                      if "collmod" in caps else []),
                    TableAction(separator=True),
                    TableAction("drop", "Drop index", "mdi-delete", danger=True),
                ],
            ),
            container=modal.body.querySelector("[data-slot=table]"),
        )

        async def _refresh() -> None:
            result = await MongoService().indexes_async(conn_id, db, coll)
            if not result.get("ok"):
                table.set_empty_text(result.get("error", "Could not list indexes"))
                table.set_rows([])
                return
            stats = await MongoService().stats_async(conn_id, db, coll)
            sizes = stats.get("index_sizes", {}) if stats.get("ok") else {}
            indexes.clear()
            rows = []
            for index in result["indexes"]:
                indexes[index["name"]] = index
                flags = [name for name in ("unique", "sparse", "hidden") if index.get(name)]
                if index.get("ttl") is not None:
                    flags.append(f"TTL {index['ttl']}s")
                if index.get("partial"):
                    flags.append("partial " + compact(index["partial"], 60))
                if index.get("options"):
                    flags.append(compact(index["options"], 60))
                size = sizes.get(index["name"])
                rows.append({
                    "name": index["name"],
                    "keys_shown": compact(index["keys"], 200),
                    "flags": ", ".join(flags) or "—",
                    "size": format_bytes(size) if size is not None else "",
                    "size_bytes": size or 0,
                })
            table.set_rows(rows)

        def _edit(name: str) -> None:
            index = indexes.get(name)
            if name == "_id_":
                self._toast("The _id index cannot be changed.", kind="warning")
            elif index is not None:
                _spawn(self._index_editor(conn_id, db, coll, index, _refresh), "edit index")

        def _on_action(payload: dict) -> None:
            name = str(payload.get("id") or "")
            action = payload.get("action")
            if action == "edit":
                _edit(name)
                return

            async def _run() -> None:
                service = MongoService()
                if action == "drop":
                    if not await message.confirm(
                        f"Drop index {name}?", title="Drop index", ok_text="Drop", danger=True,
                    ):
                        return
                    result = await service.drop_index_async(conn_id, db, coll, name)
                elif action == "hide":
                    hidden = not (indexes.get(name) or {}).get("hidden")
                    result = await service.set_index_hidden_async(conn_id, db, coll, name, hidden)
                    if result.get("ok"):
                        self._toast(f"{name} is {'hidden from' if hidden else 'visible to'} the query planner.", kind="success")
                else:
                    return
                if not result.get("ok"):
                    self._toast(result.get("error", "Failed"), kind="error")
                await _refresh()

            _spawn(_run(), f"index {action}")

        table.on_action(_on_action)
        table.on_activate(lambda payload: _edit(str(payload.get("id") or "")))

        form = Form(
            FormConfig(
                columns=4, submit_text="Create index",
                fields=self._index_fields(None, caps),
            ),
            container=modal.body.querySelector("[data-slot=form]"),
        )

        async def _create(values: dict) -> None:
            form.set_busy(True)
            try:
                result = await MongoService().create_index_async(
                    conn_id, db, coll, values.get("keys", ""), values.get("name") or "",
                    bool(values.get("unique")), bool(values.get("sparse")),
                    int(values.get("ttl") or 0), values.get("partial") or "",
                    bool(values.get("hidden")), values.get("options") or "",
                )
                if not result.get("ok"):
                    form.set_error(None, result.get("error", "Could not create"))
                    return
                form.set_values({"keys": "", "name": "", "ttl": "", "partial": "", "options": "",
                                 "unique": False, "sparse": False, "hidden": False})
                self._toast(f"Created index {result['name']}.", kind="success")
                await _refresh()
            finally:
                form.set_busy(False)

        form.on_submit(lambda values: _spawn(_create(values), "create index"))
        modal.show()
        await _refresh()

    @staticmethod
    def _index_fields(index: dict | None = None, caps: list | None = None) -> list:
        """
        The index form, empty for Create or filled from an index for Edit.
        TTL and Hidden only where the backend has them (phase 39).
        """
        index = index or {}
        caps = caps if caps is not None else ["ttl_indexes", "collmod"]
        fields = [
            FieldConfig(id="keys", label="Keys", required=True, span=2,
                        value=index.get("keys_text", ""),
                        placeholder="{email: 1}  ·  {title: 'text'}  ·  {\"$**\": 1}"),
            FieldConfig(id="name", label="Name", value=index.get("name", ""),
                        placeholder="(generated)"),
            FieldConfig(id="ttl", label="TTL seconds", type="number", min=0,
                        value=index.get("ttl") if index.get("ttl") is not None else "",
                        help="Blank or 0: no expiry."),
            FieldConfig(id="partial", label="Partial filter", span=2,
                        value=index.get("partial_text", ""),
                        placeholder="{status: 'active'}"),
            FieldConfig(id="options", label="Other options", span=2,
                        value=index.get("options_text", ""),
                        placeholder="{collation: {locale: 'en', strength: 2}}",
                        help="collation, weights, default_language, language_override, "
                             "wildcardProjection, bits, min, max"),
            FieldConfig(id="unique", label="Unique", type="checkbox",
                        value=bool(index.get("unique"))),
            FieldConfig(id="sparse", label="Sparse", type="checkbox",
                        value=bool(index.get("sparse"))),
            FieldConfig(id="hidden", label="Hidden from planner", type="checkbox",
                        value=bool(index.get("hidden"))),
        ]
        dropped = {"ttl": "ttl_indexes", "hidden": "collmod"}
        return [field for field in fields if dropped.get(field.id, "") in caps or field.id not in dropped]

    async def _index_editor(self, conn_id: int, db: str, coll: str, index: dict, refresh) -> None:
        """
        Edit one index. The server plans first (``dry_run``) and the plan is
        shown before anything changes: what differs, and whether it is done in
        place or needs a rebuild — and if so, whether the collection goes
        without the index while it builds.
        """
        name = index["name"]
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Edit index {name}", width=760, height=600))
        modal.body.innerHTML = (
            '<div class="mg-split">'
            '  <div data-slot="form"></div>'
            '  <div class="mg-plan" data-slot="plan" hidden></div>'
            "</div>"
        )
        plan_el = modal.body.querySelector("[data-slot=plan]")
        form = Form(
            FormConfig(columns=4, submit_text="Review changes", cancel_text="Cancel",
                       fields=self._index_fields(index, self._caps(conn_id))),
            container=modal.body.querySelector("[data-slot=form]"),
        )
        form.on_cancel(lambda _payload: modal.close())
        pending: dict = {}

        def _args(values: dict) -> dict:
            return {
                "keys": values.get("keys", ""),
                "new_name": (values.get("name") or "").strip() or name,
                "unique": bool(values.get("unique")),
                "sparse": bool(values.get("sparse")),
                "ttl_seconds": int(values.get("ttl") or 0),
                "partial": values.get("partial") or "",
                "hidden": bool(values.get("hidden")),
                "options": values.get("options") or "",
            }

        how = {
            "in-place": ("mdi-check-circle-outline", "In place, without rebuilding the index."),
            "build-then-drop": ("mdi-swap-horizontal",
                                "Rebuild: the new index is built first, then the old one is "
                                "dropped, so queries keep an index throughout. Building "
                                "takes a while on a large collection."),
            "drop-then-build": ("mdi-alert-outline",
                                "Rebuild under the same name: MongoDB cannot rename an index, "
                                "so the old one is dropped first and the collection has no "
                                "such index until the new one is built. If the build fails, "
                                "the old index is restored. Give it a new name to avoid the gap."),
        }

        async def _review(values: dict) -> None:
            form.set_busy(True)
            try:
                args = _args(values)
                result = await MongoService().update_index_async(
                    conn_id, db, coll, name, dry_run=True, **args)
            finally:
                form.set_busy(False)
            if not result.get("ok"):
                form.set_error(None, result.get("error", "Invalid definition"))
                return
            if result["strategy"] == "none":
                plan_el.hidden = True
                self._toast("Nothing to change.")
                return
            pending.clear()
            pending.update(args)
            icon, text = how[result["strategy"]]
            plan_el.dataset.strategy = result["strategy"]
            plan_el.innerHTML = (
                f'<div class="mg-plan-head"><span class="mdi {icon}"></span>'
                f'<span>Changes: {_esc(", ".join(result["changes"]))}</span></div>'
                f'<div class="mg-plan-how">{_esc(text)}</div>'
                '<div class="mg-editor-actions">'
                '<button type="button" class="mg-btn mg-primary" data-plan="apply">'
                '<span class="mdi mdi-check"></span><span>Apply</span></button></div>'
            )
            plan_el.hidden = False

        async def _apply() -> None:
            button = plan_el.querySelector("[data-plan=apply]")
            if button:
                button.disabled = True
            result = await MongoService().update_index_async(conn_id, db, coll, name, **pending)
            if not result.get("ok"):
                plan_el.hidden = True
                form.set_error(None, result.get("error", "Could not change the index"))
                await refresh()
                return
            modal.close()
            done = "rebuilt" if result["strategy"] != "in-place" else "updated"
            self._toast(f"Index {result['name']} {done}: {', '.join(result['changes'])}.", kind="success")
            await refresh()

        def _on_click(event) -> None:
            if event.target.closest("[data-plan=apply]"):
                _spawn(_apply(), "apply index change")

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        # Editing the form again invalidates a plan already on screen.
        form.on_change(lambda _payload: setattr(plan_el, "hidden", True))
        form.on_submit(lambda values: _spawn(_review(values), "review index change"))
        modal.show()
        form.focus_first()

    # ------------------------------------------------------------------
    # Collection views
    # ------------------------------------------------------------------

    def _open_view(self, conn_id: int, db: str, coll: str, kind: str = "collection") -> str:
        """
        A new tab on a collection. Several tabs on one collection are fine;
        each keeps its own query, page and view mode, as in the original.
        """
        self._tab_counter += 1
        tid = f"v{self._tab_counter}"
        conn = self._conn(conn_id) or {}
        self.tabs.add_tab(TabConfig(id=tid, title=coll, closable=True))
        cell = self.tabs.get_cell(tid)
        container = cell.getContainer() if hasattr(cell, "getContainer") else cell
        container.innerHTML = self._view_html(tid, conn.get("name", "?"), db, coll, kind)
        if "explain" not in self._caps(conn_id):
            container.querySelector('[data-mg="explain"]').hidden = True

        view = {
            "tid": tid, "conn": conn_id, "db": db, "coll": coll, "kind": kind,
            "page": 1, "page_size": 50, "total": None, "exact": True,
            "docs": [], "mode": "find", "shown": "table", "readonly": False,
            "table": None, "tree": None, "schema": None, "tsort": None,
            "syncing_sort": False, "busy": False,
            # source: the filter text the rows were read from or applied as;
            # note: why the filter could not be read back, while it stands.
            "builder": {"logic": "and", "rows": [self._qb_blank()], "source": None, "note": ""},
            # The pipeline as cards (phase 34); the raw text stays the source
            # of truth and is rewritten from these on every change.
            "pl_mode": "stages", "stages": [], "stage_counter": 0,
        }
        self._views[tid] = view
        view["table"] = self._make_table(tid)
        self.tabs.set_active(tid)
        _spawn(self._run(tid), "first query")
        _spawn(self._load_head_stats(tid), "head stats")
        _spawn(self._attach_view_editors(tid), "query editors")
        _spawn(self._sample_fields(tid), "field sample")
        _spawn(self._load_columns(tid), "column layout")
        _spawn(self._load_view_mode(tid), "view mode")
        return tid

    def _view_html(self, tid: str, conn_name: str, db: str, coll: str, kind: str) -> str:
        modes = "".join(
            f'<option value="{value}">{label}</option>' for value, label in _MODES
            if kind != "view" or value in ("find", "aggregate")
        )
        stages = '<option value="">+ stage</option>' + "".join(
            f'<option value="{_esc(name)}">{_esc(name)}</option>' for name, _ in _STAGE_TEMPLATES
        )
        sizes = "".join(
            f'<option value="{size}"{" selected" if size == 50 else ""}>{size} / page</option>'
            for size in PAGE_SIZES
        )
        write = "" if kind != "view" else " disabled"
        return f"""
<div class="mg-view" data-tab="{tid}">
  <div class="mg-head">
    <span class="mdi mdi-server"></span><span>{_esc(conn_name)}</span>
    <span class="mg-head-sep">›</span>
    <span class="mdi mdi-database"></span><span>{_esc(db)}</span>
    <span class="mg-head-sep">›</span>
    <span class="mdi {'mdi-eye-outline' if kind == 'view' else 'mdi-table'}"></span>
    <strong id="{tid}-coll">{_esc(coll)}</strong>
    <span class="mg-head-stats" id="{tid}-stats"></span>
  </div>
  <div class="mg-query">
    <div class="mg-qrow">
      <select class="mg-select mg-mode" data-role="mode" id="{tid}-mode" title="Query mode">{modes}</select>
      <textarea class="mg-input mg-code mg-grow" data-role="filter" id="{tid}-filter" rows="1"
        spellcheck="false" autocomplete="off"
        placeholder="Filter — {{status: &quot;active&quot;, age: {{$gte: 21}}}}  ·  Enter runs"></textarea>
      <textarea class="mg-input mg-code mg-grow mg-tabbable" data-role="pipeline" id="{tid}-pipeline"
        rows="5" spellcheck="false" hidden
        placeholder="[{{$match: {{…}}}}, {{$group: {{_id: &quot;$field&quot;, n: {{$sum: 1}}}}}}]  ·  Ctrl+Enter runs"></textarea>
      <div class="mg-qbuttons">
        <button type="button" class="mg-btn mg-primary" data-mg="run" title="Run (Ctrl+Enter)">
          <span class="mdi mdi-play"></span><span>Run</span></button>
        <button type="button" class="mg-btn" data-mg="count" title="Count matching documents">
          <span class="mdi mdi-counter"></span><span>Count</span></button>
        <button type="button" class="mg-btn" data-mg="explain" title="Show the query plan">
          <span class="mdi mdi-map-search-outline"></span><span>Explain</span></button>
        <button type="button" class="mg-btn" data-mg="builder" title="Build the filter from conditions"
          aria-pressed="false">
          <span class="mdi mdi-filter-cog-outline"></span><span>Builder</span></button>
        <button type="button" class="mg-btn" data-mg="fields" title="Fields sampled from the collection">
          <span class="mdi mdi-format-list-bulleted-type"></span><span>Fields</span></button>
        <button type="button" class="mg-btn" data-mg="reset" title="Clear the query">
          <span class="mdi mdi-backspace-outline"></span></button>
      </div>
    </div>
    <div class="mg-qrow" data-for="find">
      <input class="mg-input mg-code" data-role="sort" id="{tid}-sort" spellcheck="false"
        autocomplete="off" placeholder="Sort — {{created: -1}}">
      <input class="mg-input mg-code" data-role="projection" id="{tid}-projection" spellcheck="false"
        autocomplete="off" placeholder="Projection — {{name: 1, email: 1}}">
    </div>
    <div class="mg-qrow" data-for="update" hidden>
      <textarea class="mg-input mg-code mg-grow mg-tabbable" data-role="update" id="{tid}-update"
        rows="2" spellcheck="false" placeholder="Update — {{$set: {{status: &quot;archived&quot;}}}}"></textarea>
      <label class="mg-check"><input type="checkbox" id="{tid}-upsert"> upsert</label>
    </div>
    <div class="mg-qrow" data-for="aggregate" hidden>
      <span class="mg-seg" role="group" aria-label="Pipeline editor">
        <button type="button" class="mg-seg-btn mg-seg-text" data-mg="pl_mode" data-plmode="stages"
          aria-pressed="true" title="One card per stage">Stages</button>
        <button type="button" class="mg-seg-btn mg-seg-text" data-mg="pl_mode" data-plmode="raw"
          aria-pressed="false" title="The whole pipeline as text">Raw</button>
      </span>
      <select class="mg-select" data-role="stage" title="Add a stage">{stages}</select>
      <span class="mg-hint">Read-only: $out and $merge are refused. Results are capped at 1,000.</span>
    </div>
    <div class="mg-stages" id="{tid}-stages" hidden></div>
    <div class="mg-builder" id="{tid}-builder" hidden></div>
  </div>
  <div class="mg-bar">
    <button type="button" class="mg-btn" data-mg="insert" title="Insert a document (N)"{write}>
      <span class="mdi mdi-plus"></span><span>Insert</span></button>
    <button type="button" class="mg-btn" data-mg="edit" title="Edit the selected document"{write}>
      <span class="mdi mdi-pencil"></span><span>Edit</span></button>
    <button type="button" class="mg-btn" data-mg="clone" title="Copy the selected document"{write}>
      <span class="mdi mdi-content-duplicate"></span><span>Clone</span></button>
    <button type="button" class="mg-btn" data-mg="delete_sel" title="Delete the selected documents"{write}>
      <span class="mdi mdi-delete"></span><span>Delete</span></button>
    <button type="button" class="mg-btn" data-mg="bulk_update" title="Apply an update to the selected documents"{write}>
      <span class="mdi mdi-pencil-box-multiple"></span><span>Update selected</span></button>
    <span class="mg-bar-sep"></span>
    <button type="button" class="mg-btn" data-mg="export_json" title="Export every match as JSON">
      <span class="mdi mdi-code-json"></span><span>JSON</span></button>
    <button type="button" class="mg-btn" data-mg="export_csv" title="Export every match as CSV">
      <span class="mdi mdi-file-delimited-outline"></span><span>CSV</span></button>
    <span class="mg-bar-spacer"></span>
    <span class="mg-seg" role="group" aria-label="View">
      <button type="button" class="mg-seg-btn" data-mg="view" data-view="table" aria-pressed="true" title="Table">
        <span class="mdi mdi-table"></span></button>
      <button type="button" class="mg-seg-btn" data-mg="view" data-view="json" aria-pressed="false" title="JSON">
        <span class="mdi mdi-code-json"></span></button>
      <button type="button" class="mg-seg-btn" data-mg="reset_columns"
        title="Reset column widths and order for this collection">
        <span class="mdi mdi-table-column-width"></span></button>
      <button type="button" class="mg-seg-btn" data-mg="view" data-view="tree" aria-pressed="false" title="Tree">
        <span class="mdi mdi-file-tree"></span></button>
    </span>
    <span class="mg-pager">
      <button type="button" class="mg-icon-btn" data-mg="first" title="First page"><span class="mdi mdi-page-first"></span></button>
      <button type="button" class="mg-icon-btn" data-mg="prev" title="Previous page"><span class="mdi mdi-chevron-left"></span></button>
      <input class="mg-input mg-page" data-role="page" id="{tid}-page" value="1" inputmode="numeric" title="Page — Enter to jump">
      <span class="mg-pages" id="{tid}-pages">/ 1</span>
      <button type="button" class="mg-icon-btn" data-mg="next" title="Next page"><span class="mdi mdi-chevron-right"></span></button>
      <button type="button" class="mg-icon-btn" data-mg="last" title="Last page"><span class="mdi mdi-page-last"></span></button>
      <select class="mg-select" data-role="size" title="Page size">{sizes}</select>
      <span class="mg-summary" id="{tid}-summary"></span>
    </span>
  </div>
  <div class="mg-status" id="{tid}-status" hidden></div>
  <div class="mg-results">
    <div class="mg-panel" id="{tid}-table"></div>
    <pre class="mg-panel mg-json" id="{tid}-json" hidden></pre>
    <div class="mg-panel" id="{tid}-tree" hidden></div>
  </div>
</div>"""

    def _make_table(self, tid: str) -> DataTable:
        table = DataTable(
            DataTableConfig(
                columns=[ColumnConfig(id="_id", header="_id")],
                id_field=_ROW_KEY,
                selection="multi",
                # Widths and order persist per collection (phase 35).
                resizable_columns=True,
                reorderable_columns=True,
                filterable=False,
                empty_text="No documents",
                loading_text="Running…",
                context_actions=[
                    TableAction("edit", "Edit…", "mdi-pencil"),
                    TableAction("clone", "Clone…", "mdi-content-duplicate"),
                    TableAction("copy", "Copy as JSON", "mdi-content-copy"),
                    TableAction("filter_id", "Filter to this _id", "mdi-filter-outline"),
                    TableAction(separator=True),
                    TableAction("delete", "Delete", "mdi-delete", danger=True),
                ],
            ),
            container=_el(f"{tid}-table"),
        )
        table.on_activate(lambda payload: self._row_edit(tid, payload))
        table.on_action(lambda payload: self._row_action(tid, payload))
        table.on_sort(lambda payload: self._on_table_sort(tid, payload))
        table.on_columns(lambda payload: self._on_columns(tid, payload))
        return table

    def _on_tab_close(self, payload) -> None:
        tid = payload.get("id") if isinstance(payload, dict) else payload
        self._discard_view(str(tid))
        self._discard_dash(str(tid))

    def _close_view(self, tid: str) -> None:
        self._discard_view(tid)
        self.tabs.remove_tab(tid)

    def _discard_view(self, tid: str) -> None:
        view = self._views.pop(tid, None)
        if view is None:
            return
        for element_id in [key for key in self._editors if key.startswith(f"{tid}-")]:
            entry = self._editors.pop(element_id)
            try:
                entry["editor"].destroy()
            except Exception:  # noqa: BLE001 - already gone with its tab
                pass
        for key in ("table", "tree"):
            widget = view.get(key)
            if widget is not None:
                try:
                    widget.destroy()
                except Exception:  # noqa: BLE001 - already torn down with its tab
                    pass

    def _apply_mode(self, tid: str, mode: str) -> None:
        view = self._views[tid]
        view["mode"] = mode
        root = js.document.querySelector(f'.mg-view[data-tab="{tid}"]')
        is_update = mode in ("updateOne", "updateMany")
        for row in root.querySelectorAll("[data-for]"):
            which = str(row.getAttribute("data-for"))
            row.hidden = not (
                (which == "find" and mode == "find")
                or (which == "update" and is_update)
                or (which == "aggregate" and mode == "aggregate")
            )
        self._set_hidden(f"{tid}-filter", mode == "aggregate")
        stages_mode = view["pl_mode"] == "stages"
        self._set_hidden(f"{tid}-pipeline", mode != "aggregate" or stages_mode)
        _el(f"{tid}-stages").hidden = mode != "aggregate" or not stages_mode
        if mode == "aggregate" and stages_mode:
            self._stages_from_text(tid, quiet=True)
        run = root.querySelector('[data-mg="run"] span:not(.mdi)')
        run.textContent = {
            "find": "Run", "aggregate": "Run",
            "updateOne": "Preview", "updateMany": "Preview",
            "deleteOne": "Preview", "deleteMany": "Preview",
        }.get(mode, "Run")
        for action in ("count", "explain"):
            button = root.querySelector(f'[data-mg="{action}"]')
            button.disabled = mode == "aggregate"

    def _insert_stage(self, tid: str, name: str) -> None:
        if self._views[tid]["pl_mode"] == "stages":
            self._stage_add(tid, name)
            return
        template = dict(_STAGE_TEMPLATES).get(name)
        field = _el(f"{tid}-pipeline")
        if not template or not field:
            return
        current = str(field.value).strip()
        stage = template.replace(chr(10), chr(10) + "  ")
        if not current:
            text = f"[\n  {stage}\n]"
        elif current.endswith("]"):
            body = current[:-1].rstrip()
            separator = "" if body.endswith("[") else ","
            text = f"{body}{separator}\n  {stage}\n]"
        else:
            text = f"{current},\n{template}"
        # The caret lands inside the new stage's innermost braces, ready to type.
        caret = len(text.rstrip("]\n").rstrip("}"))
        self._set_text(f"{tid}-pipeline", text, caret)
        self._focus_field(f"{tid}-pipeline")

    def _query(self, tid: str) -> dict:
        def value(suffix: str) -> str:
            element = _el(f"{tid}-{suffix}")
            return str(element.value) if element else ""

        return {
            "filter": value("filter"),
            "sort": value("sort"),
            "projection": value("projection"),
            "update": value("update"),
            "pipeline": value("pipeline"),
        }

    def _add_braces(self, tid: str, query: dict, roles) -> None:
        """
        The server forgives missing outer braces (``mql.parse_braced``); once
        a query has run, show the person the braced text it was read as. Only
        boxes still holding the text that ran, so typing since is never lost.
        """
        for role in roles:
            text = query.get(role, "")
            stripped = text.strip()
            if not stripped or stripped[0] in "{[" or stripped.startswith(("/*", "//")):
                continue
            if self._query(tid).get(role) == text:
                self._set_text(f"{tid}-{role}", "{" + stripped + "}")

    def _status(self, tid: str, text: str = "", kind: str = "error") -> None:
        status = _el(f"{tid}-status")
        if not status:
            return
        status.hidden = not text
        status.dataset.kind = kind
        status.textContent = text

    def _on_view_action(self, tid: str, action: str, button) -> None:
        view = self._views[tid]
        if action == "run":
            view["page"] = 1
            _spawn(self._run(tid), "run")
        elif action == "count":
            _spawn(self._count(tid), "count")
        elif action == "explain":
            _spawn(self._explain(tid), "explain")
        elif action == "fields":
            _spawn(self._fields_dialog(tid), "fields")
        elif action == "builder":
            self._qb_toggle(tid)
        elif action == "pl_mode":
            self._pipeline_mode(tid, str(button.dataset.plmode))
        elif action.startswith("st_"):
            self._stage_action(tid, action, int(button.getAttribute("data-stage")))
        elif action.startswith("qb_"):
            self._qb_action(tid, action, button)
        elif action == "reset":
            for suffix in ("filter", "sort", "projection", "update", "pipeline"):
                self._set_text(f"{tid}-{suffix}", "")
            view["stages"] = []
            if not _el(f"{tid}-stages").hidden:
                self._render_stages(tid)
            view["page"] = 1
            self._status(tid)
            _spawn(self._run(tid), "reset")
        elif action == "first":
            self._goto_page(tid, 1)
        elif action == "prev":
            self._goto_page(tid, view["page"] - 1)
        elif action == "next":
            self._goto_page(tid, view["page"] + 1)
        elif action == "last":
            self._goto_page(tid, self._page_count(view))
        elif action == "reset_columns":
            _spawn(self._reset_columns(tid), "reset columns")
        elif action == "view":
            self._show_as(tid, str(button.dataset.view))
        elif action == "insert":
            _spawn(self._insert_document(tid), "insert")
        elif action in ("edit", "clone"):
            row = self._selected_row(tid)
            if row is not None:
                handler = self._edit_document if action == "edit" else self._clone_document
                _spawn(handler(tid, row), action)
        elif action == "delete_sel":
            _spawn(self._delete_selected(tid), "delete selected")
        elif action == "bulk_update":
            _spawn(self._bulk_update(tid), "bulk update")
        elif action in ("export_json", "export_csv"):
            _spawn(self._export(tid, action.split("_")[1]), "export")

    @staticmethod
    def _page_count(view: dict) -> int:
        total = view.get("total")
        if total is None:
            return view["page"] + 1
        return max(1, -(-int(total) // view["page_size"]))

    def _goto_page(self, tid: str, page) -> None:
        view = self._views.get(tid)
        if view is None:
            return
        try:
            number = int(str(page).strip())
        except ValueError:
            number = view["page"]
        if view.get("total") is not None:
            number = min(number, self._page_count(view))
        number = max(1, number)
        if number != view["page"] or view["mode"] != "find":
            view["page"] = number
            if view["mode"] != "find":
                view["mode"] = "find"
                _el(f"{tid}-mode").value = "find"
                self._apply_mode(tid, "find")
            _spawn(self._run(tid), "page")
        else:
            _el(f"{tid}-page").value = str(view["page"])

    # -- the code editor (ROADMAP phase 31) ------------------------------

    async def _load_asset(self, tag: str, attrs: dict) -> bool:
        """Append a <script>/<link> and wait for its load or error event."""
        future = asyncio.get_event_loop().create_future()
        element = js.document.createElement(tag)
        for name, value in attrs.items():
            setattr(element, name, value)

        def _settle(ok: bool):
            def _handler(*_args) -> None:
                if not future.done():
                    future.set_result(ok)
            proxy = create_proxy(_handler)
            self._proxies.append(proxy)
            return proxy

        element.addEventListener("load", _settle(True))
        element.addEventListener("error", _settle(False))
        js.document.head.appendChild(element)
        try:
            return await asyncio.wait_for(future, EDITOR_LOAD_SECONDS)
        except asyncio.TimeoutError:
            return False

    async def _ensure_editor(self) -> bool:
        """
        Load the editor bundle once; every caller awaits the same attempt.
        A failure is remembered, so the page does not retry per tab.
        """
        if self._editor_ready is None:
            self._editor_ready = asyncio.ensure_future(self._load_editor())
        return await asyncio.shield(self._editor_ready)

    async def _load_editor(self) -> bool:
        if getattr(js.window, "MgEditor", None):
            return True
        loaded = await self._load_asset("script", {"src": EDITOR_SRC})
        if not loaded or not getattr(js.window, "MgEditor", None):
            js.console.warn("[monguana] editor bundle unavailable; keeping plain text boxes")
            return False
        return True

    # Which query boxes get an editor, and how each behaves. One-line boxes
    # run on Enter; multi-line ones indent with Tab and run on Ctrl+Enter.
    _QUERY_EDITORS = (
        ("filter", False, "9em"),
        ("sort", False, "5em"),
        ("projection", False, "5em"),
        ("update", True, "10em"),
        ("pipeline", True, "22em"),
    )

    def _mount_editor(self, area, *, role: str, multiline: bool, on_run=None,
                      on_save=None, on_change=None, max_height: str | None = None,
                      height: str | None = None, fill: bool = False,
                      fields=None) -> dict:
        """
        Put a CodeMirror editor in place of a <textarea>/<input>, which stays
        in the DOM, hidden, as the source of truth: every edit is copied into
        it, so code that reads a box needs no editor awareness. Writes go
        through ``_set_text``. The caller must have awaited ``_ensure_editor``.
        """
        host = js.document.createElement("div")
        host.className = "mg-cm " + ("mg-cm-fill" if fill else "mg-grow")
        host.dataset.role = f"{role}-editor"
        area.parentNode.insertBefore(host, area)
        hidden_before = bool(area.hidden)

        def _on_change(text) -> None:
            area.value = str(text)
            if on_change:
                on_change(str(text))

        def _call(handler):
            return lambda *_args: handler() if handler else None

        proxies = [create_proxy(_on_change), create_proxy(_call(on_run)),
                   create_proxy(_call(on_save))]
        options = {
            "value": str(area.value),
            "placeholder": str(area.placeholder or ""),
            "multiline": multiline,
            "label": str(area.getAttribute("aria-label") or role),
            "onChange": proxies[0],
            "onRun": proxies[1],
            "onSave": proxies[2],
            # Filter, sort and projection are one object: completing a field
            # in an empty box writes its braces too (see entry.js applyField).
            "objectBox": role in ("filter", "sort", "projection"),
        }
        if max_height:
            options["maxHeight"] = max_height
        if height:
            options["height"] = height
        if fields is not None:
            options["fields"] = fields
        editor = js.window.MgEditor.create(host, to_js(options, dict_converter=js.Object.fromEntries))
        area.hidden = True
        host.hidden = hidden_before
        return {"editor": editor, "host": host, "proxies": proxies}

    async def _attach_editor(self, tid: str, role: str, *, multiline: bool = False,
                             max_height: str = "9em") -> None:
        """An editor over one of a view's query boxes (see ``_mount_editor``)."""
        element_id = f"{tid}-{role}"
        if element_id in self._editors or not await self._ensure_editor():
            return
        area = _el(element_id)
        if not area or tid not in self._views:
            return

        def _run() -> None:
            view = self._views.get(tid)
            if view is not None:
                view["page"] = 1
                _spawn(self._run(tid), "run")

        self._editors[element_id] = self._mount_editor(
            area, role=role, multiline=multiline, on_run=_run, max_height=max_height,
            fields=self._field_list(tid),
        )

    async def _attach_view_editors(self, tid: str) -> None:
        for role, multiline, max_height in self._QUERY_EDITORS:
            await self._attach_editor(tid, role, multiline=multiline, max_height=max_height)

    async def _sample_fields(self, tid: str) -> None:
        """
        Sample the collection's fields in the background when a view opens,
        so completions offer field paths from the first keystroke rather than
        only after Fields has been opened. Quiet: a failure just means no
        field completions.
        """
        view = self._views.get(tid)
        if view is None or view.get("schema"):
            return
        result = await MongoService().schema_async(view["conn"], view["db"], view["coll"])
        if result.get("ok") and tid in self._views and not self._views[tid].get("schema"):
            self._views[tid]["schema"] = result
            self._editor_fields(tid)
            panel = _el(f"{tid}-builder")
            if panel and not panel.hidden:
                self._qb_render(tid)

    def _field_list(self, tid: str):
        """The view's sampled field paths as the editor's JS completion list."""
        schema = (self._views.get(tid) or {}).get("schema") or {}
        return to_js(
            [{"path": row["path"], "types": " | ".join(row["types"])}
             for row in schema.get("fields", [])],
            dict_converter=js.Object.fromEntries,
        )

    def _editor_fields(self, tid: str) -> None:
        """Offer the sampled field paths as completions in this view's editors."""
        fields = self._field_list(tid)
        for element_id, entry in self._editors.items():
            if element_id.startswith(f"{tid}-"):
                entry["editor"].setFields(fields)

    def _set_text(self, element_id: str, text: str, caret: int | None = None) -> None:
        field = _el(element_id)
        if field:
            field.value = text
        entry = self._editors.get(element_id)
        if entry:
            entry["editor"].setValue(text, caret if caret is not None else len(text))

    def _focus_field(self, element_id: str) -> None:
        entry = self._editors.get(element_id)
        if entry:
            entry["editor"].focus()
            return
        field = _el(element_id)
        if field:
            field.focus()

    def _set_hidden(self, element_id: str, hidden: bool) -> None:
        entry = self._editors.get(element_id)
        if entry:
            entry["host"].hidden = hidden
            return
        field = _el(element_id)
        if field:
            field.hidden = hidden

    # -- the visual query builder (ROADMAP phase 33) -----------------------

    @staticmethod
    def _qb_blank() -> dict:
        # type_set: the person chose the type, so picking a field must not
        # replace it with the field's sampled one.
        return {"field": "", "op": "$eq", "type": "string", "value": "", "type_set": False}

    def _qb_types(self, tid: str) -> dict:
        schema = (self._views.get(tid) or {}).get("schema") or {}
        return {row["path"]: row["types"] for row in schema.get("fields", [])}

    def _qb_toggle(self, tid: str) -> None:
        panel = _el(f"{tid}-builder")
        root = js.document.querySelector(f'.mg-view[data-tab="{tid}"]')
        opening = bool(panel.hidden)
        panel.hidden = not opening
        root.querySelector('[data-mg="builder"]').setAttribute("aria-pressed", "true" if opening else "false")
        if opening:
            self._qb_render(tid)
            first = panel.querySelector("[data-qb=field]")
            if first:
                first.focus()
            # A filter written or changed since the rows were last read or
            # applied is read back; rows still in progress for this filter stay.
            text = self._query(tid)["filter"]
            if text.strip() and text.strip() != (self._views[tid]["builder"]["source"] or "").strip():
                _spawn(self._qb_load(tid), "builder read filter")

    async def _qb_load(self, tid: str, explicit: bool = False) -> None:
        """Read the filter box back into the builder's rows (``filter_rows``)."""
        view = self._views.get(tid)
        if view is None:
            return
        state = view["builder"]
        text = self._query(tid)["filter"]
        if not text.strip():
            if explicit:
                state.update(rows=[self._qb_blank()], logic="and", source=text, note="")
                self._qb_render(tid)
            return
        result = await MongoService().builder_rows_async(text)
        if tid not in self._views or self._query(tid)["filter"] != text:
            return  # closed, or the filter changed while this was asked
        if result.get("ok"):
            rows = [{key: (bool(row.get(key)) if key == "type_set" else str(row.get(key) or ""))
                     for key in ("field", "op", "type", "value", "type_set")}
                    for row in result.get("rows") or []]
            state.update(rows=rows or [self._qb_blank()], logic=str(result.get("logic") or "and"),
                         source=text, note="")
        else:
            state["note"] = (f"The filter can't be shown as rows: {result.get('error', 'unknown')}. "
                             "Apply replaces it.")
        self._qb_render(tid)

    def _qb_row_html(self, tid: str, index: int, row: dict) -> str:
        sampled = self._qb_types(tid)
        op_labels = dict(QB_OPERATORS)
        types = sampled.get(row["field"].strip(), [])
        ops = operators_for(row["type"], types)
        if row["op"] not in ops:
            ops = ops + [row["op"]]
        op_options = "".join(
            f'<option value="{_esc(op)}"{" selected" if op == row["op"] else ""}>'
            f"{_esc(op_labels.get(op, op))}</option>" for op in ops
        )
        type_options = "".join(
            f'<option value="{value}"{" selected" if value == row["type"] else ""}>{label}</option>'
            for value, label in QB_TYPES
        )
        if row["op"] == "$exists":
            chosen = "false" if str(row["value"]).lower() == "false" else "true"
            value_html = (
                '<select class="mg-select mg-qb-value" data-qb="value">'
                f'<option value="true"{" selected" if chosen == "true" else ""}>yes</option>'
                f'<option value="false"{" selected" if chosen == "false" else ""}>no</option>'
                "</select>"
            )
        else:
            hint = {
                "$in": "a, b, c", "$nin": "a, b, c", "$regex": "pattern or /pattern/i",
                "$type": "string, int, date, objectId…", "$size": "3",
            }.get(row["op"]) or {
                "date": "2026-03-01 or 2026-03-01T12:00:00Z", "objectid": "24 hex digits",
                "bool": "true or false", "uuid": "xxxxxxxx-xxxx-…", "null": "(no value)",
                "raw": "any value, e.g. ISODate(\"…\")",
            }.get(row["type"], "value")
            disabled = " disabled" if row["type"] == "null" and row["op"] in ("$eq", "$ne") else ""
            value_html = (
                f'<input class="mg-input mg-code mg-qb-value" data-qb="value" '
                f'value="{_esc(row["value"])}" placeholder="{_esc(hint)}" '
                f'spellcheck="false" autocomplete="off"{disabled}>'
            )
        type_hint = (" · sampled: " + ", ".join(types)) if types else ""
        return (
            f'<div class="mg-qb-row" data-row="{index}">'
            f'<input class="mg-input mg-code mg-qb-field" data-qb="field" list="{tid}-qb-fields" '
            f'value="{_esc(row["field"])}" placeholder="field" spellcheck="false" autocomplete="off">'
            f'<select class="mg-select mg-qb-op" data-qb="op" title="Operator">{op_options}</select>'
            f'<select class="mg-select mg-qb-type" data-qb="type" '
            f'title="Write the value as this type{_esc(type_hint)}">{type_options}</select>'
            f"{value_html}"
            f'<button type="button" class="mg-icon-btn" data-mg="qb_remove" data-row="{index}" '
            f'title="Remove this condition"><span class="mdi mdi-close"></span></button>'
            "</div>"
        )

    def _qb_render(self, tid: str) -> None:
        state = self._views[tid]["builder"]
        sampled = self._qb_types(tid)
        rows_html = [self._qb_row_html(tid, index, row) for index, row in enumerate(state["rows"])]
        datalist = "".join(f'<option value="{_esc(path)}">' for path in sampled)
        logic = state["logic"]
        panel = _el(f"{tid}-builder")
        panel.innerHTML = (
            f'<datalist id="{tid}-qb-fields">{datalist}</datalist>'
            + (f'<div class="mg-qb-note" role="status"><span class="mdi mdi-information-outline"></span> '
               f'{_esc(state["note"])}</div>' if state.get("note") else "") +
            f'<div class="mg-qb-rows">{"".join(rows_html)}</div>'
            '<div class="mg-qb-foot">'
            '<span class="mg-seg" role="group" aria-label="Combine conditions">'
            f'<button type="button" class="mg-seg-btn mg-seg-text" data-mg="qb_logic" data-logic="and" '
            f'aria-pressed="{"true" if logic == "and" else "false"}">AND</button>'
            f'<button type="button" class="mg-seg-btn mg-seg-text" data-mg="qb_logic" data-logic="or" '
            f'aria-pressed="{"true" if logic == "or" else "false"}">OR</button></span>'
            '<button type="button" class="mg-btn" data-mg="qb_add"><span class="mdi mdi-plus"></span>'
            "<span>Condition</span></button>"
            f'<code class="mg-qb-preview" id="{tid}-qb-preview"></code>'
            '<button type="button" class="mg-btn" data-mg="qb_read" '
            'title="Replace these rows with the filter as it is now">'
            '<span class="mdi mdi-import"></span><span>Read filter</span></button>'
            '<button type="button" class="mg-btn" data-mg="qb_clear">Clear</button>'
            '<button type="button" class="mg-btn mg-primary" data-mg="qb_apply" '
            'title="Replace the filter with this and run it (Ctrl+Z in the filter undoes)">'
            '<span class="mdi mdi-check"></span><span>Apply</span></button>'
            "</div>"
        )
        self._qb_preview(tid)

    def _qb_preview(self, tid: str) -> str | None:
        """Show the filter the rows make, or why they cannot. Returns the text."""
        state = self._views[tid]["builder"]
        preview = _el(f"{tid}-qb-preview")
        try:
            text = build_filter(state["rows"], state["logic"])
        except BuilderError as exc:
            if preview:
                preview.textContent = str(exc)
                preview.dataset.state = "error"
            return None
        if preview:
            preview.textContent = text
            preview.dataset.state = "ok"
        return text

    def _qb_read(self, tid: str, target, *, rerender: bool) -> None:
        row_el = target.closest(".mg-qb-row")
        if not row_el:
            return
        state = self._views[tid]["builder"]
        index = int(row_el.dataset.row)
        if index >= len(state["rows"]):
            return
        row = state["rows"][index]
        key = str(target.dataset.qb)
        row[key] = str(target.value)
        if key == "type":
            row["type_set"] = True
        if key == "field" and rerender and not row["type_set"]:
            # A field was picked: write its values as the type it was sampled with.
            row["type"] = default_type(self._qb_types(tid).get(row["field"].strip(), []))
        if key in ("type", "field") and rerender:
            allowed = operators_for(row["type"], self._qb_types(tid).get(row["field"].strip(), []))
            if row["op"] not in allowed:
                row["op"] = "$eq"
        if rerender and key in ("field", "op", "type"):
            # Redraw this row only: redrawing the panel would replace a button
            # mid-click (a field's change fires on blur, i.e. on the way to
            # Apply). Focus stays on the same control of the new row.
            active = js.document.activeElement
            focused = (str(active.getAttribute("data-qb") or "")
                       if active and row_el.contains(active) else "")
            holder = js.document.createElement("div")
            holder.innerHTML = self._qb_row_html(tid, index, row)
            fresh = holder.firstElementChild
            row_el.replaceWith(fresh)
            if focused:
                control = fresh.querySelector(f"[data-qb={focused}]")
                if control:
                    control.focus()
        self._qb_preview(tid)

    def _qb_action(self, tid: str, action: str, button) -> None:
        state = self._views[tid]["builder"]
        if action == "qb_add":
            state["rows"].append(self._qb_blank())
            self._qb_render(tid)
            fields = _el(f"{tid}-builder").querySelectorAll("[data-qb=field]")
            fields.item(fields.length - 1).focus()
        elif action == "qb_remove":
            index = int(button.dataset.row)
            if 0 <= index < len(state["rows"]):
                state["rows"].pop(index)
            if not state["rows"]:
                state["rows"].append(self._qb_blank())
            self._qb_render(tid)
        elif action == "qb_logic":
            state["logic"] = str(button.dataset.logic)
            self._qb_render(tid)
        elif action == "qb_clear":
            state["rows"] = [self._qb_blank()]
            state["logic"] = "and"
            self._qb_render(tid)
        elif action == "qb_read":
            _spawn(self._qb_load(tid, explicit=True), "builder read filter")
        elif action == "qb_apply":
            text = self._qb_preview(tid)
            if text is None:
                self._toast("Fix the highlighted condition first.", kind="warning")
                return
            self._set_text(f"{tid}-filter", text)
            state["source"], state["note"] = text, ""
            view = self._views[tid]
            if view["mode"] == "aggregate":
                _el(f"{tid}-mode").value = "find"
                self._apply_mode(tid, "find")
            view["page"] = 1
            _spawn(self._run(tid), "builder apply")

    # -- the pipeline stage list (ROADMAP phase 34) -------------------------

    def _editor_available(self) -> bool:
        ready = self._editor_ready
        return bool(ready is not None and ready.done() and not ready.cancelled()
                    and not ready.exception() and ready.result())

    def _pipeline_mode(self, tid: str, mode: str) -> None:
        """Switch between the stage cards and the raw text."""
        view = self._views[tid]
        if mode == view["pl_mode"]:
            return
        if mode == "stages" and not self._stages_from_text(tid):
            return  # the raw text does not split; stay on it, the status says why
        if mode == "stages":
            self._status(tid)  # clear an earlier "cannot be shown as stages"
        view["pl_mode"] = mode
        root = js.document.querySelector(f'.mg-view[data-tab="{tid}"]')
        for button in root.querySelectorAll('[data-mg="pl_mode"]'):
            button.setAttribute("aria-pressed", "true" if button.dataset.plmode == mode else "false")
        self._apply_mode(tid, view["mode"])
        if mode == "raw":
            self._focus_field(f"{tid}-pipeline")

    def _stages_from_text(self, tid: str, quiet: bool = False) -> bool:
        """Cards from the raw text. False (and a status) when it does not split."""
        view = self._views[tid]
        text = str(_el(f"{tid}-pipeline").value)
        try:
            parsed = split_pipeline(text)
        except PipelineTextError as exc:
            if not quiet:
                self._status(tid, f"The pipeline text cannot be shown as stages: {exc}")
            return False
        current = [{k: stage[k] for k in ("op", "body", "enabled")} for stage in view["stages"]]
        if parsed != current:
            view["stages"] = []
            for stage in parsed:
                view["stage_counter"] += 1
                view["stages"].append({"id": view["stage_counter"], **stage})
        self._render_stages(tid)
        return True

    def _stages_to_text(self, tid: str) -> None:
        """Rewrite the raw text (what Run sends) from the cards."""
        view = self._views[tid]
        try:
            text = join_pipeline(view["stages"])
        except PipelineTextError as exc:
            self._status(tid, str(exc))
            return
        self._set_text(f"{tid}-pipeline", "" if text == "[]" else text)

    def _stage_index(self, tid: str, stage_id: int) -> int:
        stages = self._views[tid]["stages"]
        return next((i for i, stage in enumerate(stages) if stage["id"] == stage_id), -1)

    def _stage_add(self, tid: str, op: str) -> None:
        view = self._views[tid]
        template = dict(_STAGE_TEMPLATES).get(op)
        body = split_pipeline(template)[0]["body"] if template else "{}"
        view["stage_counter"] += 1
        view["stages"].append({"id": view["stage_counter"], "op": op, "body": body, "enabled": True})
        self._stages_to_text(tid)
        self._render_stages(tid, focus=view["stage_counter"])

    def _stage_set(self, tid: str, stage_id: int, *, op: str | None = None,
                   body: str | None = None, rerender: bool = True) -> None:
        index = self._stage_index(tid, stage_id)
        if index < 0:
            return
        stage = self._views[tid]["stages"][index]
        if op is not None:
            stage["op"] = op
        if body is not None:
            stage["body"] = body
        self._stages_to_text(tid)
        if rerender and op is not None:
            self._render_stages(tid)

    def _stage_action(self, tid: str, action: str, stage_id: int) -> None:
        view = self._views[tid]
        stages = view["stages"]
        index = self._stage_index(tid, stage_id)
        if index < 0:
            return
        if action == "st_run":
            stage = stages[index]
            if not stage["enabled"]:
                self._toast("That stage is disabled; enable it to run up to it.", kind="warning")
                return
            label = f"After stage {index + 1} ({stage['op']})"
            _spawn(self._aggregate(tid, compose_pipeline(stages, upto=index), label), "run to stage")
            return
        if action in ("st_up", "st_down"):
            other = index - 1 if action == "st_up" else index + 1
            if not 0 <= other < len(stages):
                return
            stages[index], stages[other] = stages[other], stages[index]
        elif action == "st_toggle":
            stages[index]["enabled"] = not stages[index]["enabled"]
        elif action == "st_remove":
            stages.pop(index)
        self._stages_to_text(tid)
        self._render_stages(tid)

    def _render_stages(self, tid: str, focus: int | None = None) -> None:
        """
        Draw the cards. Their editors are rebuilt each time (moving a card
        moves its text, not its CodeMirror instance), so the old ones are
        destroyed first.
        """
        view = self._views[tid]
        for element_id in [key for key in self._editors if key.startswith(f"{tid}-stage-")]:
            self._editors.pop(element_id)["editor"].destroy()
        host = _el(f"{tid}-stages")
        stages = view["stages"]
        if not stages:
            host.innerHTML = ('<div class="mg-hint mg-stages-empty">No stages yet. Add one with '
                              "“+ stage”, or switch to Raw to paste a pipeline.</div>")
            return
        cards = []
        for number, stage in enumerate(stages, start=1):
            sid = stage["id"]
            ops = list(_STAGE_OPS) if stage["op"] in _STAGE_OPS else [stage["op"], *_STAGE_OPS]
            options = "".join(
                f'<option value="{_esc(op)}"{" selected" if op == stage["op"] else ""}>{_esc(op)}</option>'
                for op in ops
            )
            on = stage["enabled"]
            first, last = number == 1, number == len(stages)
            cards.append(
                f'<div class="mg-stage" data-stage="{sid}" data-enabled="{"true" if on else "false"}">'
                '<div class="mg-stage-head">'
                f'<span class="mg-stage-num">{number}</span>'
                f'<select class="mg-select mg-stage-op" data-stage-op="{sid}" title="Stage">{options}</select>'
                '<span class="mg-stage-spacer"></span>'
                f'<button type="button" class="mg-icon-btn" data-mg="st_run" data-stage="{sid}" '
                'title="Run the pipeline up to and including this stage">'
                '<span class="mdi mdi-play-outline"></span></button>'
                f'<button type="button" class="mg-icon-btn" data-mg="st_up" data-stage="{sid}" '
                f'title="Move up"{" disabled" if first else ""}><span class="mdi mdi-arrow-up"></span></button>'
                f'<button type="button" class="mg-icon-btn" data-mg="st_down" data-stage="{sid}" '
                f'title="Move down"{" disabled" if last else ""}><span class="mdi mdi-arrow-down"></span></button>'
                f'<button type="button" class="mg-icon-btn" data-mg="st_toggle" data-stage="{sid}" '
                f'title="{"Disable" if on else "Enable"} this stage" aria-pressed="{"false" if on else "true"}">'
                f'<span class="mdi {"mdi-eye-outline" if on else "mdi-eye-off-outline"}"></span></button>'
                f'<button type="button" class="mg-icon-btn mg-danger-icon" data-mg="st_remove" '
                f'data-stage="{sid}" title="Remove this stage"><span class="mdi mdi-close"></span></button>'
                "</div>"
                f'<textarea class="mg-input mg-code mg-stage-body" id="{tid}-stage-{sid}" '
                f'data-stage-body="{sid}" rows="3" spellcheck="false" '
                f'aria-label="{_esc(stage["op"])} stage">{_esc(stage["body"])}</textarea>'
                "</div>"
            )
        host.innerHTML = "".join(cards)
        if not self._editor_available():
            return  # the plain text boxes stay; _on_input keeps the stages current

        def _run() -> None:
            view["page"] = 1
            _spawn(self._run(tid), "run")

        for stage in stages:
            sid = stage["id"]
            element_id = f"{tid}-stage-{sid}"
            self._editors[element_id] = self._mount_editor(
                _el(element_id), role=f"stage-{sid}", multiline=True, on_run=_run,
                on_change=lambda text, sid=sid: self._stage_set(tid, sid, body=text, rerender=False),
                max_height="14em", fields=self._field_list(tid),
            )
        if focus is not None and f"{tid}-stage-{focus}" in self._editors:
            self._editors[f"{tid}-stage-{focus}"]["editor"].focus()

    # -- running queries -------------------------------------------------

    async def _run(self, tid: str) -> None:
        view = self._views.get(tid)
        if view is None or view["busy"]:
            return
        mode = view["mode"]
        if mode == "find":
            await self._find(tid)
        elif mode == "aggregate":
            await self._aggregate(tid)
        else:
            await self._preview_write(tid)

    async def _find(self, tid: str) -> None:
        view = self._views[tid]
        query = self._query(tid)
        view["busy"] = True
        view["table"].set_busy(True)
        try:
            result = await MongoService().find_async(
                view["conn"], view["db"], view["coll"], query["filter"], query["sort"],
                query["projection"], view["page"], view["page_size"],
            )
        finally:
            view["busy"] = False
            if tid in self._views:
                view["table"].set_busy(False)
        if tid not in self._views:
            return
        if not result.get("ok"):
            self._status(tid, result.get("error", "Query failed"))
            return
        self._add_braces(tid, query, ("filter", "sort", "projection"))
        self._status(tid)
        view.update({
            "docs": result["docs"], "total": result.get("total"),
            "exact": result.get("exact", True), "page": result["page"],
            "readonly": view["kind"] == "view",
        })
        self._sync_table_sort(tid, result.get("sort") or [])
        self._render(tid)

    async def _aggregate(self, tid: str, pipeline: str | None = None, label: str = "") -> None:
        view = self._views[tid]
        view["busy"] = True
        view["table"].set_busy(True)
        try:
            result = await MongoService().aggregate_async(
                view["conn"], view["db"], view["coll"],
                self._query(tid)["pipeline"] if pipeline is None else pipeline,
            )
        finally:
            view["busy"] = False
            if tid in self._views:
                view["table"].set_busy(False)
        if tid not in self._views:
            return
        if not result.get("ok"):
            self._status(tid, result.get("error", "Aggregation failed"))
            return
        docs = result["docs"]
        view.update({"docs": docs, "total": len(docs), "exact": True, "page": 1,
                     "readonly": True})
        prefix = f"{label}: " if label else ""
        self._status(
            tid,
            prefix + ("Showing the first 1,000 results." if result.get("truncated") else
                      f"{len(docs):,} result(s). Aggregation results are read-only."),
            "info",
        )
        self._sync_table_sort(tid, [])
        self._render(tid)

    async def _count(self, tid: str) -> None:
        view = self._views[tid]
        self._status(tid, "Counting…", "info")
        result = await MongoService().count_async(
            view["conn"], view["db"], view["coll"], self._query(tid)["filter"]
        )
        if not result.get("ok"):
            self._status(tid, result.get("error", "Count failed"))
            return
        view["total"], view["exact"] = result["total"], True
        self._status(tid, f"{result['total']:,} document(s) match.", "info")
        self._render_pager(tid)

    async def _explain(self, tid: str) -> None:
        view = self._views[tid]
        query = self._query(tid)
        result = await MongoService().explain_async(
            view["conn"], view["db"], view["coll"], query["filter"], query["sort"]
        )
        if not result.get("ok"):
            self._status(tid, result.get("error", "Explain failed"))
            return
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Query plan", width=760, height=600))
        facts = [
            ("Plan", result.get("summary", "")),
            ("Documents examined", result.get("docs_examined")),
            ("Index keys examined", result.get("keys_examined")),
            ("Returned", result.get("returned")),
            ("Time", f"{result['ms']} ms" if result.get("ms") is not None else None),
        ]
        modal.body.innerHTML = (
            '<div class="mg-split">'
            '<table class="mg-kv">' + "".join(
                f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"
                for label, value in facts if value is not None
            ) + "</table>"
            f'<pre class="mg-json mg-split-bottom">{_esc(to_pretty(result.get("plan")))}</pre>'
            "</div>"
        )
        modal.show()

    def _on_table_sort(self, tid: str, payload: dict) -> None:
        """
        A header click sorts on the **server**, not just the page on screen —
        sorting 50 rows of a million locally answers a different question.
        """
        view = self._views.get(tid)
        column = payload.get("column")
        if view is None or view["syncing_sort"] or not column or view["mode"] != "find":
            return
        direction = -1 if payload.get("direction") == "desc" else 1
        view["tsort"] = payload.get("direction")
        self._set_text(f"{tid}-sort", "{" + json.dumps(str(column)) + f": {direction}" + "}")
        view["page"] = 1
        _spawn(self._run(tid), "sort")

    def _sync_table_sort(self, tid: str, keys: list) -> None:
        """Put the header caret where the server's sort is, and nowhere else."""
        view = self._views[tid]
        view["syncing_sort"] = True
        try:
            if len(keys) == 1 and keys[0][1] in (1, -1):
                view["tsort"] = "asc" if keys[0][1] == 1 else "desc"
                view["table"].sort(str(keys[0][0]), view["tsort"])
            else:
                view["tsort"] = None
                view["table"].sort(None, "asc")
        finally:
            view["syncing_sort"] = False

    # -- rendering -------------------------------------------------------

    def _render(self, tid: str) -> None:
        view = self._views[tid]
        self._render_pager(tid)
        shown = view["shown"]
        if shown == "table":
            self._render_table(tid)
        elif shown == "json":
            self._render_json(tid)
        else:
            self._render_tree(tid)

    def _render_pager(self, tid: str) -> None:
        view = self._views[tid]
        root = js.document.querySelector(f'.mg-view[data-tab="{tid}"]')
        paged = view["mode"] == "find"
        total = view.get("total")
        pages = self._page_count(view)
        _el(f"{tid}-page").value = str(view["page"])
        _el(f"{tid}-pages").textContent = f"/ {pages:,}" if total is not None else "/ ?"
        if paged:
            summary = (
                page_summary(view["page"], view["page_size"], len(view["docs"]), total, view["exact"])
                if total is not None else
                f"{len(view['docs']):,} shown · total unknown (Count to find out)"
            )
        else:
            summary = f"{len(view['docs']):,} result(s)"
        _el(f"{tid}-summary").textContent = summary
        at_start = view["page"] <= 1
        at_end = total is not None and view["page"] >= pages
        for action, disabled in (
            ("first", at_start or not paged), ("prev", at_start or not paged),
            ("next", at_end or not paged or len(view["docs"]) < view["page_size"]),
            ("last", at_end or not paged or total is None),
        ):
            root.querySelector(f'[data-mg="{action}"]').disabled = disabled

    def _render_table(self, tid: str) -> None:
        view = self._views[tid]
        docs = [row["doc"] for row in view["docs"]]
        keys = union_columns(docs)
        # The saved layout is for this collection's documents; aggregation
        # output has its own shape and is laid out fresh.
        saved = view.get("columns") if view["mode"] == "find" else None
        defaults = {key: default_column_width(key) for key in keys}
        defaults["_id"] = 230
        layout = apply_column_state(keys, saved, defaults)
        columns = [key for key, _width in layout]
        view["table"].set_columns([
            ColumnConfig(
                id=key, header=key, sort_by=_RANK_KEY, width=width, icon_by=_TYPE_KEY + key,
                # A header click re-sorts on the server; see _on_table_sort.
                sortable=view["mode"] == "find",
            )
            for key, width in layout
        ] or [ColumnConfig(id="_id", header="_id")])

        # Every column sorts by rank, i.e. by the server's order. The table
        # applies its own sort to whatever rows it is given, and ordering a
        # page of mixed BSON types locally would contradict the server's order.
        descending = view.get("tsort") == "desc"
        rows = []
        for index, row in enumerate(view["docs"]):
            doc = row["doc"]
            cells = {_ROW_KEY: str(index), _RANK_KEY: -index if descending else index}
            for key in columns:
                if key in doc:
                    cells[key] = cell_text(doc[key])
                    kind = type_label(doc[key])
                    cells[_TYPE_KEY + key] = _TYPE_ICONS.get(kind, "mdi-circle-small")
                    cells[_TYPE_KEY + key + "_title"] = kind
            rows.append(cells)
        view["table"].set_rows(rows)
        view["table"].set_empty_text("No documents match." if view["mode"] == "find" else "No results.")

    def _render_json(self, tid: str) -> None:
        view = self._views[tid]
        docs = [row["doc"] for row in view["docs"]]
        _el(f"{tid}-json").textContent = to_pretty(docs) if docs else "[]"

    def _render_tree(self, tid: str) -> None:
        view = self._views[tid]
        budget = [TREE_MAX_NODES]
        truncated = [False]

        def node(path: str, key: str, value, depth: int) -> TreeItem:
            budget[0] -= 1
            kind = type_label(value)
            icon = _TYPE_ICONS.get(kind, "mdi-circle-small")
            container = isinstance(value, (dict, list)) and not tagged_type(value)
            if not container:
                return TreeItem(id=path, label=f"{key}: {cell_text(value, 160)}", icon=icon,
                                badge=kind, tooltip=f"{key} ({kind})\n{scalar_text(value)}")
            entries = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
            children = []
            if depth >= TREE_MAX_DEPTH:
                truncated[0] = True
                children = [TreeItem(id=f"{path}/…", label=f"… nested deeper than {TREE_MAX_DEPTH} levels",
                                     icon="mdi-dots-horizontal")]
            else:
                for child_key, child in entries:
                    if budget[0] <= 0:
                        truncated[0] = True
                        children.append(TreeItem(id=f"{path}/…", label="… (not shown)",
                                                 icon="mdi-dots-horizontal"))
                        break
                    children.append(node(f"{path}/{child_key}", str(child_key), child, depth + 1))
            label = f"{key}  {{{len(entries)}}}" if isinstance(value, dict) else f"{key}  [{len(entries)}]"
            return TreeItem(id=path, label=label, icon=icon, badge=kind, items=children
                            or [TreeItem(id=f"{path}/empty", label="(empty)")])

        items = []
        for index, row in enumerate(view["docs"]):
            if budget[0] <= 0:
                truncated[0] = True
                break
            doc = row["doc"]
            ident = scalar_text(doc.get("_id")) if isinstance(doc, dict) and "_id" in doc else f"#{index + 1}"
            items.append(node(f"d{index}", ident, doc, 0))

        host = _el(f"{tid}-tree")
        if view["tree"] is None:
            view["tree"] = Tree(TreeConfig(empty_text="No documents", filterable=True,
                                           filter_placeholder="Filter fields and values"),
                                container=host)
        view["tree"].set_items(items)
        if truncated[0]:
            nodes = sum(count_nodes(row["doc"]) for row in view["docs"])
            self._status(
                tid,
                f"The tree shows about {TREE_MAX_NODES:,} of {nodes:,} values (and at most "
                f"{TREE_MAX_DEPTH} levels). Use the JSON view or a smaller page for the rest.",
                "info",
            )

    @staticmethod
    def _view_key(view: dict) -> str:
        return f"view:{view['conn']}:{view['db']}:{view['coll']}"

    async def _load_view_mode(self, tid: str) -> None:
        """Open a collection in the view (table/JSON/tree) last used for it."""
        view = self._views.get(tid)
        if view is None:
            return
        result = await UiStateService().get_async(self._view_key(view))
        shown = ((result.get("value") or {}).get("shown")) if result.get("ok") else None
        if tid in self._views and shown in ("json", "tree") and view["shown"] == "table":
            self._show_as(tid, shown, remember=False)

    def _show_as(self, tid: str, shown: str, remember: bool = True) -> None:
        view = self._views[tid]
        if remember and shown != view["shown"]:
            _spawn(UiStateService().set_async(self._view_key(view), {"shown": shown}), "remember view")
        view["shown"] = shown
        root = js.document.querySelector(f'.mg-view[data-tab="{tid}"]')
        for button in root.querySelectorAll('[data-mg="view"]'):
            button.setAttribute("aria-pressed", "true" if button.dataset.view == shown else "false")
        _el(f"{tid}-table").hidden = shown != "table"
        _el(f"{tid}-json").hidden = shown != "json"
        _el(f"{tid}-tree").hidden = shown != "tree"
        self._render(tid)

    async def _load_head_stats(self, tid: str) -> None:
        view = self._views.get(tid)
        if view is None or view["kind"] == "view":
            return
        result = await MongoService().stats_async(view["conn"], view["db"], view["coll"])
        label = _el(f"{tid}-stats")
        if label and result.get("ok"):
            parts = [f"{result['count']:,} docs"]
            if result.get("size") is not None:
                parts += [format_bytes(result["size"]),
                          f"{len(result['index_sizes'])} index(es)"]
            label.textContent = " · ".join(parts)

    # -- column layout (ROADMAP phase 35) ---------------------------------

    @staticmethod
    def _columns_key(view: dict) -> str:
        return f"columns:{view['conn']}:{view['db']}:{view['coll']}"

    async def _load_columns(self, tid: str) -> None:
        view = self._views.get(tid)
        if view is None:
            return
        result = await UiStateService().get_async(self._columns_key(view))
        if tid not in self._views or not result.get("ok") or not result.get("value"):
            return
        view["columns"] = result["value"]
        if view["docs"] and view["shown"] == "table" and view["mode"] == "find":
            self._render_table(tid)

    def _on_columns(self, tid: str, payload: dict) -> None:
        """A column was resized or moved: remember it, saved after a pause."""
        view = self._views.get(tid)
        if view is None or view["mode"] != "find":
            return
        view["columns"] = merge_column_state(view.get("columns"), payload.get("columns") or [])
        view["columns_gen"] = view.get("columns_gen", 0) + 1
        _spawn(self._save_columns(tid, view["columns_gen"]), "save column layout")

    async def _save_columns(self, tid: str, generation: int) -> None:
        # One save per burst of changes, not one per dragged header.
        await asyncio.sleep(0.4)
        view = self._views.get(tid)
        if view is None or view.get("columns_gen") != generation:
            return
        result = await UiStateService().set_async(self._columns_key(view), view["columns"])
        if not result.get("ok"):
            js.console.warn(f"[monguana] column layout not saved: {result.get('error')}")

    async def _reset_columns(self, tid: str) -> None:
        view = self._views[tid]
        view["columns"] = None
        view["columns_gen"] = view.get("columns_gen", 0) + 1
        result = await UiStateService().clear_async(self._columns_key(view))
        if not result.get("ok"):
            self._toast(result.get("error", "Could not reset the columns"), kind="error")
            return
        if view["shown"] == "table":
            self._render_table(tid)
        self._toast("Column widths and order reset.", kind="success")

    # -- documents -------------------------------------------------------

    def _selected_row(self, tid: str, quiet: bool = False) -> dict | None:
        view = self._views[tid]
        if view["readonly"]:
            if not quiet:
                self._toast("These results are read-only. Switch to find to edit.", kind="warning")
            return None
        ids = view["table"].get_selected_ids() if view["shown"] == "table" else []
        if len(ids) != 1:
            if not quiet:
                self._toast("Select one document in the table first.", kind="warning")
            return None
        return view["docs"][int(ids[0])]

    def _row_from_payload(self, tid: str, payload: dict) -> dict | None:
        view = self._views.get(tid)
        try:
            return view["docs"][int(payload.get("id"))]
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    def _row_edit(self, tid: str, payload: dict) -> None:
        view = self._views.get(tid)
        row = self._row_from_payload(tid, payload)
        if view is None or row is None:
            return
        if view["readonly"] or row.get("id") is None:
            self._document_viewer(row["doc"])
            return
        _spawn(self._edit_document(tid, row), "edit")

    def _row_action(self, tid: str, payload: dict) -> None:
        view = self._views.get(tid)
        row = self._row_from_payload(tid, payload)
        if view is None or row is None:
            return
        action = payload.get("action")
        if action == "copy":
            _spawn(self._copy_text(to_pretty(row["doc"])), "copy")
            return
        if view["readonly"] or row.get("id") is None:
            self._toast("These results are read-only.", kind="warning")
            return
        if action == "edit":
            _spawn(self._edit_document(tid, row), "edit")
        elif action == "clone":
            _spawn(self._clone_document(tid, row), "clone")
        elif action == "delete":
            _spawn(self._delete_rows(tid, [row]), "delete")
        elif action == "filter_id":
            self._set_text(f"{tid}-filter", "{_id: " + scalar_text(row["doc"].get("_id")) + "}")
            view["page"] = 1
            _spawn(self._run(tid), "filter by id")

    async def _copy_text(self, text: str) -> None:
        try:
            await js.navigator.clipboard.writeText(text)
            self._toast("Copied.", kind="success")
        except Exception:  # noqa: BLE001 - a browser may refuse without a gesture
            self._toast("The browser refused clipboard access.", kind="error")

    async def _insert_document(self, tid: str) -> None:
        view = self._views[tid]
        if view["kind"] == "view":
            self._toast("Views are read-only.", kind="warning")
            return
        await self._document_editor(
            tid, title=f"Insert into {view['coll']}",
            text="{\n  \n}",
            hint="One document {…} or an array [{…}, {…}]. Shell syntax works: "
                 "ObjectId(\"…\"), ISODate(\"…\"), unquoted keys. Omit _id to have one generated.",
            save=lambda text: MongoService().insert_async(view["conn"], view["db"], view["coll"], text),
            done=lambda result: f"Inserted {result.get('count', 1)} document(s).",
        )

    async def _edit_document(self, tid: str, row: dict) -> None:
        view = self._views[tid]
        # Always the whole document from the server: a projection may have
        # hidden fields, and saving the projected copy would delete them.
        result = await MongoService().get_document_async(view["conn"], view["db"], view["coll"], row["id"])
        if not result.get("ok"):
            self._toast(result.get("error", "Could not load the document"), kind="error")
            return
        await self._document_editor(
            tid, title="Edit document", text=to_shell(result["doc"]),
            hint="Saved as a whole: a field you delete here is removed. The _id cannot change.",
            save=lambda text: MongoService().replace_async(
                view["conn"], view["db"], view["coll"], row["id"], text),
            done=lambda _result: "Saved.",
        )

    async def _clone_document(self, tid: str, row: dict) -> None:
        view = self._views[tid]
        result = await MongoService().get_document_async(view["conn"], view["db"], view["coll"], row["id"])
        if not result.get("ok"):
            self._toast(result.get("error", "Could not load the document"), kind="error")
            return
        doc = dict(result["doc"])
        doc.pop("_id", None)
        await self._document_editor(
            tid, title="Clone document", text=to_shell(doc),
            hint="Inserted as a new document with a new _id.",
            save=lambda text: MongoService().insert_async(view["conn"], view["db"], view["coll"], text),
            done=lambda _result: "Cloned.",
        )

    async def _document_editor(self, tid: str, title: str, text: str, hint: str, save, done) -> None:
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=820, height=680))
        modal.body.innerHTML = (
            '<div class="mg-editor">'
            f'  <div class="mg-editor-hint">{_esc(hint)}</div>'
            '  <textarea class="mg-code mg-tabbable mg-editor-text" spellcheck="false"></textarea>'
            '  <div class="mg-editor-error" hidden></div>'
            '  <div class="mg-editor-actions">'
            '    <span class="mg-editor-keys">Ctrl+S saves · Tab indents</span>'
            '    <button type="button" class="mg-btn" data-editor="cancel">Cancel</button>'
            '    <button type="button" class="mg-btn mg-primary" data-editor="save">'
            '<span class="mdi mdi-content-save"></span><span>Save</span></button>'
            "  </div>"
            "</div>"
        )
        area = modal.body.querySelector(".mg-editor-text")
        error = modal.body.querySelector(".mg-editor-error")
        save_button = modal.body.querySelector('[data-editor="save"]')
        area.value = text
        mounted: dict = {}

        def _close() -> None:
            if mounted:
                mounted["editor"].destroy()
                mounted.clear()
            modal.close()

        async def _save() -> None:
            if save_button.disabled:
                return
            save_button.disabled = True
            try:
                result = await save(str(area.value))
            finally:
                save_button.disabled = False
            if not result.get("ok"):
                error.hidden = False
                error.textContent = result.get("error", "Could not save")
                return
            _close()
            self._toast(done(result), kind="success")
            if tid in self._views:
                await self._run(tid)

        def _on_click(event) -> None:
            which = event.target.closest("[data-editor]")
            if not which:
                return
            if which.dataset.editor == "cancel":
                _close()
            else:
                _spawn(_save(), "document save")

        def _on_key(event) -> None:
            # The editor handles its own Ctrl+S and prevents the default.
            if event.defaultPrevented:
                return
            if str(event.key).lower() == "s" and (event.ctrlKey or event.metaKey):
                event.preventDefault()
                _spawn(_save(), "document save")

        for event_name, handler in (("click", _on_click), ("keydown", _on_key)):
            proxy = create_proxy(handler)
            self._proxies.append(proxy)
            modal.body.addEventListener(event_name, proxy)
        modal.show()
        # Put the caret inside an empty template, not after it.
        caret = 4 if text == "{\n  \n}" else 0
        if await self._ensure_editor() and area.isConnected:
            mounted.update(self._mount_editor(
                area, role="document", multiline=True, fill=True, height="100%",
                on_save=lambda: _spawn(_save(), "document save"),
                on_run=lambda: _spawn(_save(), "document save"),
                fields=self._field_list(tid),
            ))
            mounted["editor"].setValue(text, caret)
            mounted["editor"].focus()
            modal.body.querySelector(".mg-editor-keys").textContent = (
                "Ctrl+S or Ctrl+Enter saves · Tab indents · Ctrl+Space completes"
            )
        else:
            area.focus()
            area.selectionStart = area.selectionEnd = caret

    def _document_viewer(self, doc) -> None:
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Document (read-only)", width=760, height=600))
        modal.body.innerHTML = f'<pre class="mg-json mg-fill">{_esc(to_shell(doc))}</pre>'
        modal.show()

    def _selected_rows(self, tid: str) -> list:
        view = self._views[tid]
        if view["readonly"]:
            self._toast("These results are read-only.", kind="warning")
            return []
        rows = [view["docs"][int(i)] for i in view["table"].get_selected_ids()]
        rows = [row for row in rows if row.get("id") is not None]
        if not rows:
            self._toast("Select documents in the table first (Ctrl/Shift+click for several).", kind="warning")
        return rows

    async def _delete_selected(self, tid: str) -> None:
        rows = self._selected_rows(tid)
        if rows:
            await self._delete_rows(tid, rows)

    async def _delete_rows(self, tid: str, rows: list) -> None:
        view = self._views[tid]
        preview = "\n".join(f"  {scalar_text(row['doc'].get('_id'))}" for row in rows[:8])
        more = f"\n  … and {len(rows) - 8} more" if len(rows) > 8 else ""
        if not await message.confirm(
            f"Delete {len(rows)} document(s) from {view['coll']}?\n\n{preview}{more}",
            title="Delete documents", ok_text="Delete", danger=True,
        ):
            return
        if len(rows) == 1:
            result = await MongoService().delete_document_async(
                view["conn"], view["db"], view["coll"], rows[0]["id"])
        else:
            result = await MongoService().bulk_delete_async(
                view["conn"], view["db"], view["coll"], [row["id"] for row in rows])
        if not result.get("ok"):
            self._toast(result.get("error", "Could not delete"), kind="error")
            return
        self._toast(f"Deleted {result.get('deleted', 0)} document(s).", kind="success")
        view["table"].clear_selection()
        await self._run(tid)

    async def _bulk_update(self, tid: str) -> None:
        rows = self._selected_rows(tid)
        if not rows:
            return
        view = self._views[tid]
        ids = [row["id"] for row in rows]
        await self._document_editor(
            tid, title=f"Update {len(ids)} selected document(s)",
            text="{\n  $set: {\n    \n  }\n}",
            hint="Any update operators: $set, $unset, $inc, $push … applied to every selected document.",
            save=lambda text: MongoService().bulk_update_async(
                view["conn"], view["db"], view["coll"], ids, text),
            done=lambda result: f"Matched {result.get('matched', 0)}, modified {result.get('modified', 0)}.",
        )

    # -- query-mode writes -------------------------------------------------

    async def _preview_write(self, tid: str) -> None:
        """
        updateOne/updateMany/deleteOne/deleteMany show what they will touch
        and wait for a confirmation before anything is written.
        """
        view = self._views[tid]
        mode = view["mode"]
        query = self._query(tid)
        multi = mode.endswith("Many")
        is_update = mode.startswith("update")
        if is_update and not query["update"].strip():
            self._status(tid, "Write the update first, e.g. {$set: {status: \"archived\"}}.")
            return
        result = await MongoService().preview_write_async(
            view["conn"], view["db"], view["coll"], query["filter"], multi)
        if not result.get("ok"):
            self._status(tid, result.get("error", "Preview failed"))
            return
        self._add_braces(tid, query, ("filter", "update") if is_update else ("filter",))
        query = self._query(tid)
        self._status(tid)
        matched = result["matched"]
        verb = "update" if is_update else "delete"
        empty_filter = not query["filter"].strip() or query["filter"].strip() == "{}"

        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"{mode} — preview", width=820, height=640))
        warning = (
            '<div class="mg-warn"><span class="mdi mdi-alert"></span> The filter is empty: '
            "this matches every document in the collection.</div>" if empty_filter else ""
        )
        count_text = (
            f"{matched:,} document(s) match; {'all' if multi else 'the first'} "
            f"{'of them ' if multi else ''}will be {verb}d."
            if matched else "No documents match. Nothing will change"
            + (" unless upsert creates one." if is_update else ".")
        )
        sample = "\n".join(compact(row["doc"], 300) for row in result["docs"])
        modal.body.innerHTML = (
            '<div class="mg-split">'
            f"{warning}"
            f'<div class="mg-preview-count">{_esc(count_text)}</div>'
            + (f'<div class="mg-preview-label">Update</div><pre class="mg-json">{_esc(query["update"])}</pre>'
               if is_update else "")
            + (f'<div class="mg-preview-label">First {len(result["docs"])} match(es)</div>'
               f'<pre class="mg-json mg-split-bottom">{_esc(sample)}</pre>' if sample else "")
            + '<div class="mg-editor-actions">'
            '<button type="button" class="mg-btn" data-confirm="cancel">Cancel</button>'
            f'<button type="button" class="mg-btn {"mg-danger" if not is_update else "mg-primary"}" '
            f'data-confirm="go">{_esc(mode)}</button>'
            "</div></div>"
        )
        go = modal.body.querySelector('[data-confirm="go"]')
        upsert_box = _el(f"{tid}-upsert")
        upsert = bool(upsert_box.checked) if upsert_box else False
        if not matched and not (is_update and upsert):
            go.disabled = True

        async def _execute() -> None:
            go.disabled = True
            service = MongoService()
            if is_update:
                outcome = await service.update_where_async(
                    view["conn"], view["db"], view["coll"], query["filter"], query["update"],
                    multi, upsert)
                summary = (f"Matched {outcome.get('matched', 0):,}, modified "
                           f"{outcome.get('modified', 0):,}"
                           + (", upserted 1" if outcome.get("upserted") else "") + ".")
            else:
                outcome = await service.delete_where_async(
                    view["conn"], view["db"], view["coll"], query["filter"], multi, empty_filter)
                summary = f"Deleted {outcome.get('deleted', 0):,} document(s)."
            modal.close()
            if not outcome.get("ok"):
                self._status(tid, outcome.get("error", "Failed"))
                return
            self._toast(summary, kind="success")
            self._status(tid, summary, "info")
            # Back to find, so the result of the write is what is on screen.
            _el(f"{tid}-mode").value = "find"
            self._apply_mode(tid, "find")
            view["page"] = 1
            await self._find(tid)

        def _on_click(event) -> None:
            which = event.target.closest("[data-confirm]")
            if not which:
                return
            if which.dataset.confirm == "cancel":
                modal.close()
            elif not which.disabled:
                _spawn(_execute(), f"{mode} execute")

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()

    # -- schema ------------------------------------------------------------

    async def _fields_dialog(self, tid: str, refresh: bool = False) -> None:
        view = self._views[tid]
        if view["schema"] is None or refresh:
            self._status(tid, "Sampling documents…", "info")
            result = await MongoService().schema_async(view["conn"], view["db"], view["coll"])
            if not result.get("ok"):
                self._status(tid, result.get("error", "Could not sample"))
                return
            self._status(tid)
            view["schema"] = result
            self._editor_fields(tid)
        schema = view["schema"]
        sampled = max(1, schema["sampled"])

        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Fields — {view['coll']}", width=720, height=620))
        modal.body.innerHTML = (
            '<div class="mg-split">'
            f'<div class="mg-hint">From a random sample of {schema["sampled"]:,} document(s); rare fields '
            'may be missing. Double-click to add to the filter; right-click for more.'
            ' <button type="button" class="mg-btn" data-schema="refresh">'
            '<span class="mdi mdi-refresh"></span><span>Resample</span></button></div>'
            '<div class="mg-split-bottom" data-slot="table"></div>'
            "</div>"
        )
        table = DataTable(
            DataTableConfig(
                columns=[
                    ColumnConfig(id="path", header="Field"),
                    ColumnConfig(id="types_text", header="Types", width=200),
                    ColumnConfig(id="presence", header="In sample", width=100, align="right",
                                 sort_by="count"),
                ],
                id_field="path", selection="single", filterable=True,
                filter_placeholder="Filter fields",
                context_actions=[
                    TableAction("filter", "Add to filter", "mdi-filter-plus-outline"),
                    TableAction("sort_asc", "Sort ascending", "mdi-sort-ascending"),
                    TableAction("sort_desc", "Sort descending", "mdi-sort-descending"),
                    TableAction("project", "Add to projection", "mdi-table-column-plus-after"),
                ],
            ),
            container=modal.body.querySelector("[data-slot=table]"),
        )
        table.set_rows([
            {**row, "types_text": ", ".join(row["types"]),
             "presence": f"{round(100 * row['count'] / sampled)}%"}
            for row in schema["fields"]
        ])

        def _apply(action: str, path: str) -> None:
            key = json.dumps(path) if not path.replace("_", "a").replace(".", "a").isalnum() else path
            if action == "filter":
                self._merge_into(f"{tid}-filter", f"{key}: ")
            elif action in ("sort_asc", "sort_desc"):
                self._set_text(f"{tid}-sort", "{" + f"{key}: {1 if action == 'sort_asc' else -1}" + "}")
            elif action == "project":
                self._merge_into(f"{tid}-projection", f"{key}: 1")
            modal.close()
            if action != "filter":
                view["page"] = 1
                _spawn(self._run(tid), "run")

        table.on_activate(lambda payload: _apply("filter", str(payload.get("id"))))
        table.on_action(lambda payload: _apply(str(payload.get("action")), str(payload.get("id"))))

        def _on_click(event) -> None:
            if event.target.closest("[data-schema=refresh]"):
                modal.close()
                _spawn(self._fields_dialog(tid, refresh=True), "resample")

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()

    def _merge_into(self, element_id: str, fragment: str) -> None:
        """Add ``fragment`` as another key of the object in a query box."""
        field = _el(element_id)
        if not field:
            return
        current = str(field.value).strip()
        if not current or current == "{}":
            text = "{" + fragment + "}"
            caret = 1 + len(fragment)
        elif current.endswith("}"):
            body = current[:-1].rstrip()
            separator = "" if body.endswith("{") else ", "
            text = f"{body}{separator}{fragment}}}"
            caret = len(body) + len(separator) + len(fragment)
        else:
            text = f"{current}, {fragment}"
            caret = len(text)
        self._set_text(element_id, text, caret)
        self._focus_field(element_id)

    # -- export, dump, restore --------------------------------------------

    async def _export(self, tid: str, fmt: str) -> None:
        view = self._views[tid]
        query = self._query(tid)
        # Validate the filter first: a bad one would otherwise surface as a
        # failed download with no explanation.
        check = await MongoService().count_async(view["conn"], view["db"], view["coll"], query["filter"])
        if not check.get("ok"):
            self._status(tid, check.get("error", "Invalid filter"))
            return
        total = check["total"]
        limit = 100_000 if fmt == "csv" else 1_000_000
        if total > limit and not await message.confirm(
            f"{total:,} documents match; the export stops at {limit:,}. Continue?",
            title="Export", ok_text="Export",
        ):
            return
        params = {
            "db": view["db"], "coll": view["coll"], "format": fmt,
            "filter": query["filter"], "sort": query["sort"],
            "projection": query["projection"], "limit": str(limit),
        }
        _download(f"/mg/export/{view['conn']}?" + urlencode(params))
        self._toast(f"Exporting {min(total, limit):,} document(s) as {fmt.upper()}…")

    async def _restore_dialog(self, conn_id: int, db: str) -> None:
        modal = ModalWindow(ModalConfig(
            dispose_on_close=True, title="Restore dump" + (f" into {db}" if db else ""), width=560, height=540,
        ))
        host = js.document.createElement("div")
        host.className = "mg-dialog"
        host.innerHTML = (
            '<div class="mg-hint">A ZIP made by Dump here, or a zipped <code>mongodump</code> '
            "directory: <code>&lt;db&gt;/&lt;collection&gt;.bson</code> with optional "
            "<code>.metadata.json</code> for indexes.</div>"
            '<input type="file" accept=".zip,application/zip" class="mg-file">'
            '<div class="mg-dialog-form"></div>'
            '<pre class="mg-json mg-restore-result" hidden></pre>'
        )
        modal.body.appendChild(host)
        form = Form(
            FormConfig(
                submit_text="Restore", cancel_text="Close",
                fields=[
                    FieldConfig(id="mode", label="When a document already exists", type="select",
                                value="skip", options=[
                                    SelectOption("skip", "Skip it (keep what is there)"),
                                    SelectOption("merge", "Replace it (upsert by _id)"),
                                    SelectOption("drop", "Drop each collection first"),
                                ]),
                    FieldConfig(id="db", label="Target database", value=db,
                                placeholder="(the folder names in the ZIP)",
                                help="Blank restores each folder into the database it names."),
                ],
            ),
            container=host.querySelector(".mg-dialog-form"),
        )
        form.on_cancel(lambda _payload: modal.close())
        result_el = host.querySelector(".mg-restore-result")

        async def _restore(values: dict) -> None:
            picker = host.querySelector(".mg-file")
            files = picker.files
            if not files or files.length == 0:
                form.set_error(None, "Choose a ZIP file first.")
                return
            upload = files.item(0)
            if values.get("mode") == "drop" and not await message.confirm(
                "Drop every collection in the archive before restoring it?",
                title="Restore", ok_text="Drop and restore", danger=True,
            ):
                return
            form.set_busy(True)
            result_el.hidden = False
            size = format_bytes(upload.size)

            def _uploaded(sent: int, total: int) -> None:
                percent = int(100 * sent / total) if total else 0
                result_el.textContent = f"Uploading {size}… {percent}%"

            params = urlencode({"mode": values.get("mode", "skip"),
                                "db": (values.get("db") or "").strip()})
            try:
                status, payload = await self._upload(f"/mg/restore/{conn_id}?{params}", upload, _uploaded)
            finally:
                form.set_busy(False)
            if status != 200 or not payload.get("job"):
                result_el.textContent = f"Refused: {payload.get('detail') or payload.get('error') or status}"
                return
            # The upload is done; the restore now runs as a job with its own
            # console, and this dialog has nothing more to show.
            modal.close()
            title = f"Restore {upload.name}" + (f" into {values.get('db').strip()}" if (values.get("db") or "").strip() else "")
            await self._job_console(payload["job"], title, "restore", conn_id)

        form.on_submit(lambda values: _spawn(_restore(values), "restore"))
        modal.show()

    async def _upload(self, url: str, body, on_progress) -> tuple:
        """
        POST a file with upload progress. XMLHttpRequest, not fetch: fetch
        reports nothing while a request body is being sent. Returns
        ``(status, parsed JSON or {})``; status 0 means the network failed.
        """
        future = asyncio.get_event_loop().create_future()
        xhr = js.XMLHttpRequest.new()
        xhr.open("POST", url)
        xhr.withCredentials = True
        xhr.setRequestHeader("X-CSRF-Token", _csrf_token())
        xhr.setRequestHeader("Content-Type", "application/zip")

        def _progress(event) -> None:
            if event.lengthComputable:
                on_progress(int(event.loaded), int(event.total))

        def _settle(*_args) -> None:
            if not future.done():
                future.set_result(None)

        proxies = [create_proxy(_progress), create_proxy(_settle)]
        self._proxies.extend(proxies)
        xhr.upload.addEventListener("progress", proxies[0])
        for name in ("load", "error", "abort", "timeout"):
            xhr.addEventListener(name, proxies[1])
        xhr.send(body)
        await future
        try:
            payload = json.loads(str(xhr.responseText or "{}"))
        except ValueError:
            payload = {}
        return int(xhr.status), payload if isinstance(payload, dict) else {}

    async def _copy_dialog(self, conn_id: int, db: str, coll: str) -> None:
        """Copy a database or collection to any connection, any backend (phase 39)."""
        what = f"{db}.{coll}" if coll else db
        modal = ModalWindow(ModalConfig(
            dispose_on_close=True, title=f"Copy {what}", width=560, height=520 if coll else 460,
        ))
        host = js.document.createElement("div")
        host.className = "mg-dialog"
        host.innerHTML = (
            '<div class="mg-hint">Copies the documents, then the indexes, into any of your '
            "connections — another server, or a tinymongo store. It runs as a job.</div>"
            '<div class="mg-dialog-form"></div>'
        )
        modal.body.appendChild(host)
        conns = sorted(self._conns, key=lambda conn: (conn["id"] != conn_id, conn["name"].lower()))
        fields = [
            FieldConfig(id="target", label="Copy into", type="select", value=str(conn_id),
                        options=[SelectOption(str(conn["id"]), conn["name"]
                                              + (" (this one)" if conn["id"] == conn_id else ""))
                                 for conn in conns]),
            FieldConfig(id="target_db", label="Database", value=db, required=True),
        ]
        if coll:
            fields.append(FieldConfig(id="target_coll", label="Collection", value=coll,
                                      required=True))
        fields.append(FieldConfig(
            id="mode", label="When a document already exists", type="select", value="skip",
            options=[
                SelectOption("skip", "Skip it (keep what is there)"),
                SelectOption("merge", "Replace it (upsert by _id)"),
                SelectOption("drop", "Drop each target collection first"),
            ],
        ))
        form = Form(FormConfig(submit_text="Copy", cancel_text="Cancel", fields=fields),
                    container=host.querySelector(".mg-dialog-form"))
        form.on_cancel(lambda _payload: modal.close())

        async def _copy(values: dict) -> None:
            target = int(values.get("target") or conn_id)
            form.set_busy(True)
            try:
                result = await JobService().start_copy_async(
                    conn_id, db, target, values.get("target_db") or "",
                    coll=coll, target_coll=values.get("target_coll") or "",
                    mode=values.get("mode") or "skip",
                )
            finally:
                form.set_busy(False)
            if not result.get("ok"):
                form.set_error(None, result.get("error", "Could not start the copy"))
                return
            modal.close()
            await self._job_console(result["job"], f"Copy {what}", "copy", target)

        form.on_submit(lambda values: _spawn(_copy(values), "copy"))
        modal.show()

    async def _start_dump(self, conn_id: int, db: str, coll: str) -> None:
        result = await JobService().start_dump_async(conn_id, db, coll)
        if not result.get("ok"):
            self._toast(result.get("error", "Could not start the dump"), kind="error")
            return
        title = f"Dump {db}.{coll}" if coll else f"Dump {db}"
        await self._job_console(result["job"], title, "dump", conn_id)

    async def _job_console(self, job_id: str, title: str, kind: str, conn_id: int) -> None:
        """
        Follow a job: a bar per collection, the log, Cancel, and at the end
        the summary (and the download, for a dump). Closing the console does
        not stop the job; a dump still downloads when it finishes.
        """
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=title, width=720, height=560))
        modal.body.innerHTML = (
            '<div class="mg-console">'
            '<div class="mg-console-head"><span class="mg-console-state" data-state="running">'
            '<span class="mdi mdi-progress-clock"></span><span>Running…</span></span>'
            '<span class="mg-console-elapsed"></span></div>'
            '<div class="mg-console-items"></div>'
            '<pre class="mg-json mg-console-log"></pre>'
            '<div class="mg-editor-actions">'
            '<button type="button" class="mg-btn mg-danger" data-job="cancel">'
            '<span class="mdi mdi-stop"></span><span>Cancel</span></button>'
            '<button type="button" class="mg-btn mg-primary" data-job="download" hidden>'
            '<span class="mdi mdi-download"></span><span>Download again</span></button>'
            '<button type="button" class="mg-btn" data-job="close">Close</button>'
            "</div></div>"
        )
        body = modal.body
        items_el = body.querySelector(".mg-console-items")
        log_el = body.querySelector(".mg-console-log")
        state_el = body.querySelector(".mg-console-state")
        cancel_el = body.querySelector('[data-job="cancel"]')
        download_el = body.querySelector('[data-job="download"]')
        download_url = f"/mg/jobs/{job_id}/download"

        def _on_click(event) -> None:
            which = event.target.closest("[data-job]")
            if not which:
                return
            action = str(which.getAttribute("data-job"))
            if action == "close":
                modal.close()
            elif action == "download":
                _download(download_url)
            elif action == "cancel" and not which.disabled:
                which.disabled = True
                _spawn(JobService().cancel_async(job_id), "cancel job")

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        body.addEventListener("click", proxy)
        modal.show()

        def _amount(value, unit: str) -> str:
            if value is None:
                return "?"
            return format_bytes(value) if unit == "bytes" else f"{int(value):,}"

        def _render(status: dict) -> None:
            rows = []
            for item in status["items"]:
                total, done, unit = item.get("total"), item.get("done") or 0, item.get("unit", "")
                if total:
                    percent = max(0, min(100, int(100 * done / total)))
                    amount = f"{_amount(done, unit)} / {_amount(total, unit)}"
                else:
                    percent = 100 if item["state"] != "running" else 0
                    amount = f"{_amount(done, unit)} {unit}"
                rows.append(
                    f'<div class="mg-console-item" data-state="{_esc(item["state"])}">'
                    f'<span class="mg-console-label" title="{_esc(item["label"])}">{_esc(item["label"])}</span>'
                    f'<span class="mg-console-track"><span class="mg-console-bar" style="width:{percent}%">'
                    "</span></span>"
                    f'<span class="mg-console-amount">{_esc(item.get("note") or amount)}</span>'
                    "</div>"
                )
            items_el.innerHTML = "".join(rows) or '<div class="mg-hint">Starting…</div>'
            for line in status["log"]:
                log_el.textContent = str(log_el.textContent) + line + "\n"
            log_el.scrollTop = log_el.scrollHeight
            body.querySelector(".mg-console-elapsed").textContent = f"{status['elapsed']:.0f} s"
            state = status["state"]
            label = {
                "running": "Cancelling…" if status.get("cancelling") else "Running…",
                "done": "Done", "failed": f"Failed: {status.get('error', '')}",
                "cancelled": "Cancelled — what finished is kept",
            }.get(state, state)
            icon = {"running": "mdi-progress-clock", "done": "mdi-check-circle",
                    "failed": "mdi-alert-circle", "cancelled": "mdi-stop-circle"}.get(state, "mdi-circle")
            state_el.dataset.state = state
            state_el.innerHTML = f'<span class="mdi {icon}"></span><span>{_esc(label)}</span>'
            cancel_el.hidden = state != "running"
            download_el.hidden = not status.get("download")

        seen = 0
        while True:
            status = await JobService().status_async(job_id, seen)
            if not status.get("ok"):
                state_el.dataset.state = "failed"
                state_el.textContent = status.get("error", "The job is gone")
                return
            seen = status["log_total"]
            _render(status)
            if status["state"] != "running":
                break
            await asyncio.sleep(0.5)

        if status["state"] == "done" and kind == "dump" and status.get("download"):
            _download(download_url)
            self._toast(f"{title}: done, downloading.", kind="success")
        elif status["state"] == "done":
            self._toast(f"{title}: done.", kind="success")
        if kind in ("restore", "copy"):
            # For a copy, conn_id is the target.
            await self._refresh_server(conn_id)

    # ------------------------------------------------------------------
    # Account dialogs
    # ------------------------------------------------------------------

    def _password_dialog(self) -> None:
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Change password", width=440, height=360))
        form = Form(
            FormConfig(
                submit_text="Change password", cancel_text="Cancel",
                fields=[
                    FieldConfig(id="current_password", label="Current password",
                                type="password", required=True, autocomplete="current-password"),
                    FieldConfig(id="new_password", label="New password", type="password",
                                required=True, min_length=8, autocomplete="new-password"),
                    FieldConfig(id="confirm", label="Confirm new password", type="password",
                                required=True, matches="new_password",
                                matches_message="Passwords do not match",
                                autocomplete="new-password"),
                ],
            ),
            container=modal.body,
        )
        form.on_cancel(lambda _payload: modal.close())

        async def _submit(values: dict) -> None:
            form.set_busy(True)
            try:
                result = await UserService().change_own_password_async(
                    values["current_password"], values["new_password"])
                if not result.get("ok"):
                    if result.get("errors"):
                        form.set_errors(result["errors"])
                    else:
                        form.set_error(None, result.get("error", "Could not change"))
                    return
                modal.close()
                self._me["must_change_password"] = False
                self._toast("Password changed.", kind="success")
            finally:
                form.set_busy(False)

        form.on_submit(lambda values: _spawn(_submit(values), "password change"))
        modal.show()
        form.focus_first()

    async def _admin_panel(self) -> None:
        if not self._me.get("is_admin"):
            self._toast("Administrator access required.", kind="error")
            return
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Users", width=760, height=560))
        modal.body.innerHTML = (
            '<div class="mg-split">'
            '  <div class="mg-split-top" data-slot="table"></div>'
            '  <div class="mg-split-bottom-auto" data-slot="form"></div>'
            "</div>"
        )
        table = DataTable(
            DataTableConfig(
                columns=[
                    ColumnConfig(id="username", header="Username"),
                    ColumnConfig(id="role", header="Role", width=90),
                    ColumnConfig(id="connection_count", header="Connections", width=110, align="right"),
                    ColumnConfig(id="created_at", header="Created", width=110),
                ],
                selection="single", empty_text="No users",
                context_actions=[
                    TableAction("toggle_admin", "Toggle admin", "mdi-shield-account"),
                    TableAction("reset", "Reset password", "mdi-lock-reset"),
                    TableAction(separator=True),
                    TableAction("delete", "Delete user", "mdi-delete", danger=True),
                ],
            ),
            container=modal.body.querySelector("[data-slot=table]"),
        )

        async def _refresh() -> None:
            users = await UserService().list_async()
            table.set_rows([{**user, "role": "admin" if user["is_admin"] else "user"} for user in users])

        def _on_action(payload: dict) -> None:
            user_id = payload.get("id")
            row = payload.get("row") or {}
            action = payload.get("action")

            async def _run() -> None:
                service = UserService()
                if action == "delete":
                    if not await message.confirm(
                        f"Delete “{row.get('username')}” and their saved connections?",
                        title="Delete user", ok_text="Delete", danger=True,
                    ):
                        return
                    result = await service.delete_async(int(user_id))
                elif action == "toggle_admin":
                    result = await service.set_admin_async(int(user_id), not row.get("is_admin"))
                elif action == "reset":
                    new_password = await message.prompt(
                        f"New password for {row.get('username')} (min 8 chars):",
                        title="Reset password", ok_text="Reset", password=True,
                    )
                    if not new_password:
                        return
                    result = await service.reset_password_async(int(user_id), str(new_password))
                else:
                    return
                if not result.get("ok"):
                    self._toast(result.get("error")
                                or "; ".join((result.get("errors") or {}).values())
                                or "Action failed", kind="error")
                await _refresh()

            _spawn(_run(), f"admin {action}")

        table.on_action(_on_action)

        create_form = Form(
            FormConfig(
                columns=3, submit_text="Add user",
                fields=[
                    FieldConfig(id="username", label="New user", required=True),
                    FieldConfig(id="password", label="Password", type="password",
                                required=True, min_length=8),
                    FieldConfig(id="is_admin", label="Administrator", type="checkbox"),
                ],
            ),
            container=modal.body.querySelector("[data-slot=form]"),
        )

        async def _create(values: dict) -> None:
            create_form.set_busy(True)
            try:
                result = await UserService().create_async(
                    values["username"], values["password"], bool(values.get("is_admin")))
                if not result.get("ok"):
                    if result.get("errors"):
                        create_form.set_errors(result["errors"])
                    else:
                        create_form.set_error(None, result.get("error", "Failed"))
                    return
                create_form.set_values({"username": "", "password": "", "is_admin": False})
                await _refresh()
            finally:
                create_form.set_busy(False)

        create_form.on_submit(lambda values: _spawn(_create(values), "create user"))
        modal.show()
        await _refresh()

    async def _check_release(self) -> None:
        """
        Ask the server whether a newer release is out (``about_service``:
        cached there, and off with MONGUANA_UPDATE_CHECK=off) and, if so,
        show the toolbar badge. Quiet on any failure.
        """
        result = await AboutService().latest_async()
        self._release = dict(result) if result.get("ok") else {
            "checked": False, "reason": "unreachable", "error": result.get("error", "")}
        badge = _el("mg-update")
        if badge and self._release.get("newer"):
            _el("mg-update-text").textContent = f"Update {self._release['latest']}"
            badge.title = f"Monguana {self._release['latest']} is out — see About"
            badge.hidden = False
        status = _el("mg-about-update")
        if status:
            status.innerHTML = self._release_html()

    def _release_html(self) -> str:
        """The About dialog's line on the release check."""
        release = self._release
        if not release:
            return '<span class="mdi mdi-loading mdi-spin"></span> Checking for a newer release…'
        if release.get("newer"):
            published = f" (released {_esc(release['published'])})" if release.get("published") else ""
            return (
                f'<span class="mdi mdi-arrow-up-circle"></span> <b>Monguana {_esc(release["latest"])} '
                f'is out</b>{published}. <a href="{_esc(release.get("url") or RELEASES_URL)}" '
                'target="_blank" rel="noopener">What\'s new and how to update</a>'
            )
        if release.get("checked"):
            return '<span class="mdi mdi-check-circle-outline"></span> This is the latest release.'
        if release.get("reason") == "off":
            return ('<span class="mdi mdi-minus-circle-outline"></span> Release checks are off '
                    "(MONGUANA_UPDATE_CHECK). "
                    f'<a href="{RELEASES_URL}" target="_blank" rel="noopener">See releases</a>')
        return ('<span class="mdi mdi-cloud-off-outline"></span> Could not check for a newer release. '
                f'<a href="{RELEASES_URL}" target="_blank" rel="noopener">See releases</a>')

    def _about_dialog(self) -> None:
        links = (
            ("mdi-book-open-variant", "Wiki", "How to use it", WIKI_URL),
            ("mdi-github", "Source", REPO, REPO_URL),
            ("mdi-tag-outline", "Releases", "Downloads and what changed", RELEASES_URL),
            ("mdi-bug-outline", "Issues", "Report a problem or ask for a feature",
             f"{REPO_URL}/issues"),
        )
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="About Monguana", width=520, height=400))
        modal.body.innerHTML = (
            '<div class="mg-about">'
            '<div class="mg-about-head">'
            '<img class="mg-about-logo" src="/static/el_iguana_avatar.webp" alt="">'
            '<div><div class="mg-about-name">Monguana '
            f'<span class="mg-about-version">{_esc(VERSION)}</span></div>'
            '<div class="mg-about-sub">A MongoDB GUI in the browser · pytincture · wapyt · '
            f'{_esc(ABOUT_LICENSE)} license</div></div></div>'
            f'<div class="mg-about-update" id="mg-about-update">{self._release_html()}</div>'
            '<div class="mg-about-links">' + "".join(
                f'<a class="mg-about-link" href="{_esc(url)}" target="_blank" rel="noopener">'
                f'<span class="mdi {icon}"></span><span><b>{_esc(label)}</b>'
                f'<small>{_esc(hint)}</small></span></a>'
                for icon, label, hint, url in links
            ) + "</div></div>"
        )
        modal.show()
        if not self._release:
            _spawn(self._check_release(), "release check")

    def _shortcuts_dialog(self) -> None:
        rows = (
            ("Enter", "Run the query (in the filter, sort or projection box)"),
            ("Ctrl+Enter", "Run the query (anywhere in a view, pipelines included)"),
            ("Shift+Enter", "New line in the filter box"),
            ("/", "Focus the filter"),
            ("N", "Insert a document"),
            ("I", "Indexes of the current collection"),
            ("Ctrl+S", "Save, in the document editor"),
            ("Tab", "Indent, in the editor and pipeline boxes"),
            ("?", "This list"),
        )
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title="Keyboard shortcuts", width=520, height=440))
        modal.body.innerHTML = '<table class="mg-kv">' + "".join(
            f"<tr><th><kbd>{_esc(key)}</kbd></th><td>{_esc(text)}</td></tr>" for key, text in rows
        ) + "</table>"
        modal.show()

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Dashboard and query profiler: tabs of their own (``self._dashes``),
    # routed by their .mg-dash root the way query views are by .mg-view.
    # ------------------------------------------------------------------

    def _new_tab(self, title: str):
        self._tab_counter += 1
        tid = f"v{self._tab_counter}"
        self.tabs.add_tab(TabConfig(id=tid, title=title, closable=True))
        cell = self.tabs.get_cell(tid)
        return tid, cell.getContainer() if hasattr(cell, "getContainer") else cell

    def _find_dash(self, kind: str, conn_id: int, db: str = "") -> str | None:
        return next((tid for tid, dash in self._dashes.items()
                     if (dash["kind"], dash["conn"], dash["db"]) == (kind, conn_id, db)), None)

    def _start_poll(self, tid: str) -> None:
        """Every DASH_POLL_SECONDS, while auto is on and the tab is in front."""
        def _tick(*_args) -> None:
            dash = self._dashes.get(tid)
            if (dash is None or not dash["auto"] or dash["busy"] or js.document.hidden
                    or self.tabs.get_active() != tid):
                return
            refresh = self._dash_refresh(tid) if dash["kind"] == "dashboard" \
                else self._profiler_rows(tid)
            _spawn(refresh, f"{dash['kind']} auto refresh")

        dash = self._dashes[tid]
        dash["tick"] = create_proxy(_tick)
        dash["timer"] = js.window.setInterval(dash["tick"], DASH_POLL_SECONDS * 1000)

    def _discard_dash(self, tid: str) -> None:
        dash = self._dashes.pop(tid, None)
        if dash is None:
            return
        js.window.clearInterval(dash.get("timer") or 0)
        if dash.get("tick") is not None:
            dash["tick"].destroy()
        for key in ("dbs_table", "ops_table", "table"):
            widget = dash.get(key)
            if widget is not None:
                try:
                    widget.destroy()
                except Exception:  # noqa: BLE001 - already gone with its tab
                    pass

    def _close_dash(self, tid: str) -> None:
        self._discard_dash(tid)
        self.tabs.remove_tab(tid)

    def _dash_status(self, tid: str, text: str = "", kind: str = "error") -> None:
        box = _el(f"{tid}-status")
        if box:
            box.textContent = text
            box.dataset.kind = kind
            box.hidden = not text

    def _dash_action(self, tid: str, action: str, button) -> None:
        dash = self._dashes[tid]
        if action == "refresh":
            if dash["kind"] == "dashboard":
                _spawn(self._dash_refresh(tid, full=True), "dashboard refresh")
            else:
                _spawn(self._profiler_load(tid), "profiler refresh")
        elif action == "level":
            _spawn(self._profiler_set(tid, int(button.getAttribute("data-level"))), "profiler level")
        elif action == "apply":
            _spawn(self._profiler_set(tid, dash["settings"].get("level", 0)), "profiler settings")
        elif action == "clear":
            _spawn(self._profiler_clear(tid), "profiler clear")
        elif action == "pfilter":
            _spawn(self._profile_filter_dialog(tid), "profile filter")
        elif action == "show":
            dash["show"] = str(button.getAttribute("data-show"))
            for item in js.document.querySelectorAll(f'.mg-dash[data-tab="{tid}"] [data-mg="show"]'):
                item.setAttribute("aria-pressed",
                                  "true" if item.getAttribute("data-show") == dash["show"] else "false")
            dash["table"].set_columns(self._profiler_columns(dash["show"]))
            _spawn(self._profiler_rows(tid), "profiler view")

    def _dash_change(self, tid: str, target) -> None:
        dash = self._dashes.get(tid)
        if dash is None:
            return
        role = _role(target)
        if role == "auto":
            dash["auto"] = bool(target.checked)
            if dash["auto"]:
                refresh = self._dash_refresh(tid) if dash["kind"] == "dashboard" \
                    else self._profiler_rows(tid)
                _spawn(refresh, "auto refresh on")
        elif role in ("p-op", "p-coll", "p-min", "p-limit"):
            _spawn(self._profiler_rows(tid), "profiler filter")

    # -- the dashboard ------------------------------------------------------

    def _open_dashboard(self, conn_id) -> None:
        if conn_id is None:
            self._toast("Select a connection first.", kind="warning")
            return
        conn_id = int(conn_id)
        existing = self._find_dash("dashboard", conn_id)
        if existing:
            self.tabs.set_active(existing)
            return
        conn = self._conn(conn_id) or {}
        caps = self._caps(conn_id)
        tid, container = self._new_tab(f"{conn.get('name', '?')} · dashboard")
        live = "server_status" in caps
        container.innerHTML = self._dashboard_html(tid, conn.get("name", "?"), live)
        dash = {"tid": tid, "kind": "dashboard", "conn": conn_id, "db": "", "caps": caps,
                "busy": False, "auto": live, "history": [], "status": None}
        self._dashes[tid] = dash
        profiler = "profiler" in caps
        dash["dbs_table"] = DataTable(
            DataTableConfig(
                columns=[
                    ColumnConfig(id="name", header="Database", width=180),
                    ColumnConfig(id="collections_t", header="Collections", width=100,
                                 align="right", sort_by="collections"),
                    ColumnConfig(id="objects_t", header="Documents", width=110, align="right",
                                 sort_by="objects"),
                    ColumnConfig(id="data_t", header="Data", width=95, align="right",
                                 sort_by="data_size"),
                    ColumnConfig(id="storage_t", header="Storage", width=95, align="right",
                                 sort_by="storage_size"),
                    ColumnConfig(id="indexes_t", header="Indexes", width=80, align="right",
                                 sort_by="indexes"),
                    ColumnConfig(id="index_t", header="Index size", width=95, align="right",
                                 sort_by="index_size"),
                    ColumnConfig(id="disk_t", header="On disk", width=95, align="right",
                                 sort_by="size_on_disk"),
                    *([ColumnConfig(id="profiler_t", header="Profiler", width=150,
                                    icon_by="profiler_icon")] if profiler else []),
                ],
                id_field="name",
                selection="single",
                empty_text="Loading…",
                context_actions=[
                    *([TableAction("profiler", "Query profiler", "mdi-speedometer"),
                       TableAction(separator=True),
                       TableAction("prof_1", "Profile slow operations", "mdi-record-circle-outline"),
                       TableAction("prof_2", "Profile every operation", "mdi-record-circle"),
                       TableAction("prof_0", "Profiler off", "mdi-stop-circle-outline"),
                       TableAction(separator=True)] if profiler else []),
                    TableAction("refresh", "Refresh", "mdi-refresh"),
                ],
            ),
            container=_el(f"{tid}-dbs"),
        )
        dash["dbs_table"].on_action(lambda payload: self._dash_db_action(tid, payload))
        if profiler:
            dash["dbs_table"].on_activate(
                lambda payload: self._open_profiler(conn_id, str(payload.get("id") or "")))
        if live:
            dash["ops_table"] = DataTable(
                DataTableConfig(
                    columns=[
                        ColumnConfig(id="secs_t", header="Running", width=85, align="right",
                                     sort_by="secs"),
                        ColumnConfig(id="op", header="Op", width=85),
                        ColumnConfig(id="ns", header="Namespace", width=200),
                        ColumnConfig(id="plan", header="Plan", width=200),
                        ColumnConfig(id="app", header="App", width=130),
                        ColumnConfig(id="client", header="Client", width=160),
                        ColumnConfig(id="opid", header="opid", width=90),
                        ColumnConfig(id="query", header="Command", width=520),
                    ],
                    id_field="opid",
                    selection="single",
                    empty_text="Loading…",
                    context_actions=[
                        TableAction("kill", "Kill operation…", "mdi-close-octagon-outline",
                                    danger=True),
                    ],
                ),
                container=_el(f"{tid}-ops"),
            )
            dash["ops_table"].on_action(lambda payload: self._dash_op_action(tid, payload))
            self._start_poll(tid)
        self.tabs.set_active(tid)
        _spawn(self._dash_refresh(tid, full=True), "dashboard")

    @staticmethod
    def _dashboard_html(tid: str, conn_name: str, live: bool) -> str:
        server = f"""
    <section class="mg-dash-section">
      <h3>Server</h3>
      <div class="mg-tiles" id="{tid}-tiles"><div class="mg-hint">Loading…</div></div>
    </section>
    <section class="mg-dash-section">
      <h3>Throughput <small>per second, last {DASH_HISTORY * DASH_POLL_SECONDS // 60} minutes</small></h3>
      <div class="mg-tiles" id="{tid}-rates"></div>
    </section>""" if live else """
    <div class="mg-hint">This backend reports no server status: databases only.</div>"""
        ops = f"""
    <section class="mg-dash-section">
      <h3>Running operations <small id="{tid}-ops-note"></small>
        <small>right-click one to kill it</small></h3>
      <div class="mg-dash-table" id="{tid}-ops"></div>
    </section>""" if live else ""
        auto = f"""
    <label class="mg-check mg-check-inline"><input type="checkbox" data-role="auto" checked>
      Auto-refresh every {DASH_POLL_SECONDS} s</label>""" if live else ""
        return f"""
<div class="mg-dash" data-tab="{tid}">
  <div class="mg-head">
    <span class="mdi mdi-server"></span><span>{_esc(conn_name)}</span>
    <span class="mg-head-sep">›</span>
    <span class="mdi mdi-view-dashboard-outline"></span><strong>Dashboard</strong>
    <span class="mg-head-stats" id="{tid}-meta"></span>
  </div>
  <div class="mg-bar">
    <button type="button" class="mg-btn" data-mg="refresh" title="Refresh everything, databases included">
      <span class="mdi mdi-refresh"></span><span>Refresh</span></button>{auto}
    <span class="mg-bar-spacer"></span>
    <span class="mg-summary" id="{tid}-updated"></span>
  </div>
  <div class="mg-status" id="{tid}-status" hidden></div>
  <div class="mg-dash-body">{server}
    <section class="mg-dash-section">
      <h3>Databases <small>double-click one for its query profiler; right-click to switch profiling</small></h3>
      <div class="mg-dash-table" id="{tid}-dbs"></div>
    </section>{ops}
  </div>
</div>"""

    async def _dash_refresh(self, tid: str, full: bool = False) -> None:
        dash = self._dashes.get(tid)
        if dash is None or dash["busy"]:
            return
        dash["busy"] = True
        try:
            if "server_status" in dash["caps"]:
                result = await MongoService().server_status_async(dash["conn"])
                if tid not in self._dashes:
                    return
                if not result.get("ok"):
                    self._dash_status(tid, result.get("error", "Could not read the server status"))
                else:
                    self._dash_status(tid)
                    self._render_server(tid, result.get("status"), result.get("status_error", ""))
                    self._render_ops(tid, result.get("ops"), result.get("ops_error", ""))
            if full:
                await self._dash_databases(tid)
            label = _el(f"{tid}-updated")
            if label:
                label.textContent = f"Updated {js.Date.new().toLocaleTimeString()}"
        finally:
            dash["busy"] = False

    def _render_server(self, tid: str, status: dict | None, error: str) -> None:
        dash = self._dashes[tid]
        tiles, rates = _el(f"{tid}-tiles"), _el(f"{tid}-rates")
        if not status:
            tiles.innerHTML = (f'<div class="mg-hint">No server status: {_esc(error)}<br>'
                               "It needs the clusterMonitor role.</div>")
            rates.innerHTML = ""
            return
        history = dash["history"]
        uptime = status.get("uptime_ms") or 0
        if history and uptime < history[-1]["uptime"]:
            history.clear()  # the server restarted; its counters did too
        counters = {**status["opcounters"], **status["network"]}
        if not history or uptime > history[-1]["uptime"]:
            sample = {"uptime": uptime, "counters": counters, "rates": {},
                      "time": str(js.Date.new().toLocaleTimeString())}
            if history:
                previous = history[-1]
                seconds = (uptime - previous["uptime"]) / 1000
                sample["rates"] = {
                    key: max(0, value - previous["counters"][key]) / seconds
                    for key, value in counters.items()
                    if value is not None and previous["counters"].get(key) is not None
                }
            history.append(sample)
            del history[:-DASH_HISTORY]

        meta = _el(f"{tid}-meta")
        if meta:
            meta.textContent = " · ".join(part for part in (
                f"MongoDB {status.get('version', '')}", status.get("host", ""),
                status.get("process", ""), status.get("engine", "")) if part)
        conns, mem, cache = status["connections"], status["mem"], status.get("cache")
        active, queued = status["active"], status["queued"]

        def num(value) -> str:
            return "—" if value is None else f"{value:,}"

        def megabytes(value) -> str:
            return "—" if value is None else format_bytes(value * 1024 * 1024)

        parts = [
            _tile("Uptime", _duration(uptime), f"since {_ago_label(uptime)}"),
            _tile("Connections", num(conns.get("current")),
                  f"{num(conns.get('active'))} active · {num(conns.get('available'))} available"),
            _tile("Clients working", f"{num(active.get('readers'))} r · {num(active.get('writers'))} w",
                  f"queued {num(queued.get('readers'))} r · {num(queued.get('writers'))} w"),
            _tile("Memory, resident", megabytes(mem.get("resident")),
                  f"virtual {megabytes(mem.get('virtual'))}"),
        ]
        if cache and cache.get("max"):
            share = (cache.get("used") or 0) / cache["max"]
            parts.append(_tile(
                "WiredTiger cache", format_bytes(cache.get("used")),
                f"{share:.0%} of {format_bytes(cache['max'])} · dirty {format_bytes(cache.get('dirty'))}",
                meter=share))
        tiles.innerHTML = "".join(parts)

        latest = history[-1]["rates"]
        cells = []
        for key, label, is_bytes in _RATE_TILES:
            points = [(sample["rates"][key], sample["time"]) for sample in history
                      if key in sample["rates"]]
            text = _rate_text(latest.get(key), is_bytes)
            cells.append(_tile(label, text, "", spark=_sparkline(
                [value for value, _ in points],
                [f"{when} · {_rate_text(value, is_bytes)}" for value, when in points],
                label)))
        rates.innerHTML = "".join(cells)

    def _render_ops(self, tid: str, ops: list | None, error: str) -> None:
        dash = self._dashes[tid]
        table = dash.get("ops_table")
        if table is None:
            return
        note = _el(f"{tid}-ops-note")
        if ops is None:
            table.set_empty_text(f"Cannot list operations: {error}")
            table.set_rows([])
            if note:
                note.textContent = ""
            return
        dash["ops"] = {op["opid"]: op for op in ops}
        table.set_empty_text("Nothing is running.")
        table.set_rows([{
            **op,
            "secs_t": "" if op.get("secs") is None else f"{op['secs']:,.2f} s",
            "secs": op.get("secs") or 0,
            "op": op.get("op") + (" (waiting for a lock)" if op.get("waiting") else ""),
        } for op in ops])
        if note:
            note.textContent = f"{len(ops)} active, longest first"

    def _dash_op_action(self, tid: str, payload: dict) -> None:
        dash = self._dashes.get(tid)
        if dash is None or payload.get("action") != "kill":
            return
        opid = str(payload.get("id") or "")
        op = (dash.get("ops") or {}).get(opid)
        if op is None:
            self._toast("That operation is no longer listed.", kind="warning")
            return
        about = " · ".join(part for part in (
            op.get("op"), op.get("ns"), op.get("app"), op.get("client")) if part)
        async def _kill() -> None:
            if not await message.confirm(
                f"Kill operation {opid}?\n\n{about}\n{op.get('query', '')[:200]}\n\n"
                "Its client gets an error. A write may have changed some "
                "documents already; those changes stay.",
                title="Kill operation", ok_text="Kill", danger=True,
            ):
                return
            result = await MongoService().kill_op_async(dash["conn"], opid)
            if not result.get("ok"):
                self._toast(result.get("error", "Could not kill the operation"), kind="error")
                return
            self._toast(f"Asked the server to kill operation {opid}.")
            # It stops at its next interrupt check; look again shortly.
            await asyncio.sleep(1)
            await self._dash_refresh(tid)

        _spawn(_kill(), "kill operation")

    async def _dash_databases(self, tid: str) -> None:
        dash = self._dashes.get(tid)
        if dash is None:
            return
        result = await MongoService().database_stats_async(dash["conn"])
        if tid not in self._dashes:
            return
        table = dash["dbs_table"]
        if not result.get("ok"):
            table.set_empty_text(result.get("error", "Could not list databases"))
            table.set_rows([])
            return

        def num(value) -> str:
            return "" if value is None else f"{value:,}"

        def size(value) -> str:
            return "" if value is None else format_bytes(value)

        rows = []
        for row in result["databases"]:
            level = row.get("profile_level")
            rows.append({
                "name": row["name"],
                "collections": row.get("collections") or 0, "collections_t": num(row.get("collections")),
                "objects": row.get("objects") or 0, "objects_t": num(row.get("objects")),
                "data_size": row.get("data_size") or 0, "data_t": size(row.get("data_size")),
                "storage_size": row.get("storage_size") or 0, "storage_t": size(row.get("storage_size")),
                "indexes": row.get("indexes") or 0, "indexes_t": num(row.get("indexes")),
                "index_size": row.get("index_size") or 0, "index_t": size(row.get("index_size")),
                "size_on_disk": row.get("size_on_disk") or 0, "disk_t": size(row.get("size_on_disk")),
                "profiler_t": row.get("error") and "no access" or _PROFILE_LEVELS.get(level, "—"),
                "profiler_icon": "mdi-record-circle" if level else "mdi-circle-outline",
                "profiler_icon_title": row.get("error") or (
                    "Profiling" if level else "Not profiling"),
            })
        table.set_empty_text("No databases.")
        table.set_rows(rows)
        if result.get("truncated"):
            self._dash_status(tid, f"Statistics for the first {len(rows)} databases only.", "info")

    def _dash_db_action(self, tid: str, payload: dict) -> None:
        dash = self._dashes.get(tid)
        if dash is None:
            return
        db = str(payload.get("id") or "")
        action = str(payload.get("action") or "")
        if action == "refresh":
            _spawn(self._dash_databases(tid), "databases")
        elif action == "profiler" and db:
            self._open_profiler(dash["conn"], db)
        elif action.startswith("prof_") and db:
            async def _set() -> None:
                result = await MongoService().set_profiler_async(dash["conn"], db, int(action[-1]))
                if not result.get("ok"):
                    self._toast(result.get("error", "Could not change the profiler"), kind="error")
                    return
                self._toast(f"{db}: {_profile_sentence(result)}", kind="success")
                await self._after_profiler_change(dash["conn"], db, result)

            _spawn(_set(), "profiler level")

    async def _after_profiler_change(self, conn_id: int, db: str, settings: dict) -> None:
        """Every dashboard and profiler tab on this server shows the new state."""
        profiler = self._find_dash("profiler", conn_id, db)
        if profiler:
            self._profiler_settings(profiler, settings)
        for tid in [tid for tid, dash in self._dashes.items()
                    if dash["kind"] == "dashboard" and dash["conn"] == conn_id]:
            await self._dash_databases(tid)

    # -- the query profiler -------------------------------------------------

    def _open_profiler(self, conn_id, db: str) -> None:
        if not db:
            return
        conn_id = int(conn_id)
        existing = self._find_dash("profiler", conn_id, db)
        if existing:
            self.tabs.set_active(existing)
            return
        if "profiler" not in self._caps(conn_id):
            self._toast("This connection's backend has no query profiler.", kind="warning")
            return
        conn = self._conn(conn_id) or {}
        tid, container = self._new_tab(f"{db} · profiler")
        colls = [row["name"] for row in self._colls.get((conn_id, db)) or []]
        container.innerHTML = self._profiler_html(tid, conn.get("name", "?"), db, colls)
        dash = {"tid": tid, "kind": "profiler", "conn": conn_id, "db": db,
                "caps": self._caps(conn_id), "busy": False, "auto": False,
                "show": "shapes", "rows": [], "settings": {}}
        self._dashes[tid] = dash
        dash["table"] = DataTable(
            DataTableConfig(
                columns=self._profiler_columns("shapes"),
                id_field="rid",
                selection="single",
                empty_text="Loading…",
                context_actions=[
                    TableAction("details", "Details…", "mdi-text-box-search-outline"),
                    TableAction("open_query", "Open in a query tab", "mdi-tab-plus"),
                ],
            ),
            container=_el(f"{tid}-table"),
        )
        dash["table"].on_activate(lambda payload: self._profiler_details(tid, str(payload.get("id"))))
        dash["table"].on_action(lambda payload: self._profiler_row_action(tid, payload))
        self._start_poll(tid)
        self.tabs.set_active(tid)
        _spawn(self._profiler_load(tid), "profiler")

    @staticmethod
    def _profiler_html(tid: str, conn_name: str, db: str, colls: list) -> str:
        ops = '<option value="">All operations</option>' + "".join(
            f'<option value="{op}">{op}</option>' for op in _PROFILE_OPS)
        names = "".join(f'<option value="{_esc(name)}"></option>' for name in colls)
        return f"""
<div class="mg-dash" data-tab="{tid}">
  <div class="mg-head">
    <span class="mdi mdi-server"></span><span>{_esc(conn_name)}</span>
    <span class="mg-head-sep">›</span>
    <span class="mdi mdi-database"></span><span>{_esc(db)}</span>
    <span class="mg-head-sep">›</span>
    <span class="mdi mdi-speedometer"></span><strong>Query profiler</strong>
    <span class="mg-head-stats" id="{tid}-pstate"></span>
  </div>
  <div class="mg-bar mg-prof-settings">
    <span class="mg-bar-label">Profiling</span>
    <span class="mg-seg" role="group" aria-label="Profiling level">
      <button type="button" class="mg-seg-btn mg-seg-text" data-mg="level" data-level="0"
        aria-pressed="false" title="Record nothing">Off</button>
      <button type="button" class="mg-seg-btn mg-seg-text" data-mg="level" data-level="1"
        aria-pressed="false" title="Record operations slower than the threshold">Slow only</button>
      <button type="button" class="mg-seg-btn mg-seg-text" data-mg="level" data-level="2"
        aria-pressed="false" title="Record every operation. This costs the server; turn it off when done.">All</button>
    </span>
    <label class="mg-check mg-check-inline" title="Server-wide: also what the server log calls slow">
      slower than <input class="mg-input mg-num" type="number" min="0" step="10" id="{tid}-slowms"> ms</label>
    <label class="mg-check mg-check-inline" title="Server-wide: the share of slow operations recorded">
      sample <input class="mg-input mg-num" type="number" min="0.01" max="1" step="0.05" id="{tid}-rate"></label>
    <button type="button" class="mg-btn" data-mg="apply" title="Apply the threshold and sample rate">
      <span class="mdi mdi-check"></span><span>Apply</span></button>
    <button type="button" class="mg-btn" data-mg="pfilter" aria-pressed="false"
      title="Decide what Slow only records with a query on the profiler's fields">
      <span class="mdi mdi-filter-cog-outline"></span><span>Filter…</span></button>
    <span class="mg-bar-spacer"></span>
    <button type="button" class="mg-btn" data-mg="clear" title="Delete everything recorded so far">
      <span class="mdi mdi-delete-sweep-outline"></span><span>Clear</span></button>
  </div>
  <div class="mg-bar">
    <span class="mg-seg" role="group" aria-label="Show">
      <button type="button" class="mg-seg-btn mg-seg-text" data-mg="show" data-show="shapes"
        aria-pressed="true" title="Grouped by query shape, most total time first">By query shape</button>
      <button type="button" class="mg-seg-btn mg-seg-text" data-mg="show" data-show="recent"
        aria-pressed="false" title="Each recorded operation, newest first">Recent</button>
    </span>
    <input class="mg-input mg-prof-coll" data-role="p-coll" list="{tid}-colls" placeholder="All collections"
      spellcheck="false" autocomplete="off">
    <datalist id="{tid}-colls">{names}</datalist>
    <select class="mg-select" data-role="p-op">{ops}</select>
    <label class="mg-check mg-check-inline">at least
      <input class="mg-input mg-num" type="number" min="0" step="10" data-role="p-min" value="0"> ms</label>
    <select class="mg-select" data-role="p-limit" title="How many recent operations">
      <option value="100">100</option><option value="250">250</option><option value="500">500</option></select>
    <button type="button" class="mg-btn" data-mg="refresh"><span class="mdi mdi-refresh"></span><span>Refresh</span></button>
    <label class="mg-check mg-check-inline"><input type="checkbox" data-role="auto">
      Auto-refresh</label>
    <span class="mg-bar-spacer"></span>
    <span class="mg-summary" id="{tid}-summary"></span>
  </div>
  <div class="mg-status" id="{tid}-status" hidden></div>
  <div class="mg-results"><div class="mg-panel" id="{tid}-table"></div></div>
</div>"""

    @staticmethod
    def _profiler_columns(show: str) -> list:
        plan = ColumnConfig(id="plan", header="Plan", width=230, icon_by="plan_icon")
        query = ColumnConfig(id="query", header="Query", width=480)
        if show == "recent":
            return [
                ColumnConfig(id="ts_t", header="Time", width=100, sort_by="ts"),
                ColumnConfig(id="op", header="Op", width=80),
                ColumnConfig(id="coll", header="Collection", width=150),
                ColumnConfig(id="millis_t", header="ms", width=70, align="right", sort_by="millis"),
                plan,
                ColumnConfig(id="docs_t", header="Docs examined", width=115, align="right",
                             sort_by="docs_examined"),
                ColumnConfig(id="keys_t", header="Keys examined", width=115, align="right",
                             sort_by="keys_examined"),
                ColumnConfig(id="returned_t", header="Returned", width=85, align="right",
                             sort_by="returned"),
                ColumnConfig(id="affected_t", header="Changed", width=80, align="right",
                             sort_by="affected"),
                ColumnConfig(id="app", header="App", width=120),
                query,
            ]
        return [
            ColumnConfig(id="coll", header="Collection", width=150),
            ColumnConfig(id="op", header="Op", width=80),
            ColumnConfig(id="count_t", header="Count", width=70, align="right", sort_by="count"),
            ColumnConfig(id="avg_t", header="Avg ms", width=75, align="right", sort_by="avg_ms"),
            ColumnConfig(id="max_t", header="Max ms", width=75, align="right", sort_by="max_ms"),
            ColumnConfig(id="total_t", header="Total ms", width=85, align="right", sort_by="total_ms"),
            ColumnConfig(id="ratio_t", header="Examined : returned", width=140, align="right",
                         sort_by="ratio"),
            plan,
            query,
        ]

    def _profiler_values(self, tid: str) -> dict:
        root = js.document.querySelector(f'.mg-dash[data-tab="{tid}"]')

        def value(role: str) -> str:
            element = root.querySelector(f'[data-role="{role}"]')
            return str(element.value).strip() if element else ""

        try:
            min_ms = max(0, int(value("p-min") or 0))
        except ValueError:
            min_ms = 0
        return {"coll": value("p-coll"), "op": value("p-op"), "min_ms": min_ms,
                "limit": int(value("p-limit") or 100)}

    async def _profiler_load(self, tid: str) -> None:
        dash = self._dashes.get(tid)
        if dash is None:
            return
        result = await MongoService().profiler_status_async(dash["conn"], dash["db"])
        if tid not in self._dashes:
            return
        if not result.get("ok"):
            self._dash_status(tid, result.get("error", "Could not read the profiler"))
            return
        self._profiler_settings(tid, result)
        await self._profiler_rows(tid)

    def _profiler_settings(self, tid: str, settings: dict) -> None:
        dash = self._dashes.get(tid)
        if dash is None:
            return
        dash["settings"] = settings
        level = settings.get("level", 0)
        for button in js.document.querySelectorAll(f'.mg-dash[data-tab="{tid}"] [data-mg="level"]'):
            button.setAttribute("aria-pressed",
                                "true" if button.getAttribute("data-level") == str(level) else "false")
        filtered = bool(settings.get("filter"))
        root = js.document.querySelector(f'.mg-dash[data-tab="{tid}"]')
        slow = root.querySelector('[data-mg="level"][data-level="1"]')
        slow.textContent = "Filtered" if filtered else "Slow only"
        slow.title = ("Record the operations the profile filter matches" if filtered
                      else "Record operations slower than the threshold")
        # A filter replaces the threshold and the sample rate.
        for element_id, value in ((f"{tid}-slowms", settings.get("slowms", 100)),
                                  (f"{tid}-rate", settings.get("sample_rate", 1.0))):
            box = _el(element_id)
            box.value = str(value)
            box.disabled = filtered
            box.title = "Not used while the profile filter is set" if filtered else ""
        root.querySelector('[data-mg="apply"]').disabled = filtered
        button = root.querySelector('[data-mg="pfilter"]')
        button.setAttribute("aria-pressed", "true" if filtered else "false")
        button.title = (f"Profile filter: {compact(settings['filter'], 200)}" if filtered
                        else "Decide what Slow only records with a query on the profiler's fields")
        state = _el(f"{tid}-pstate")
        if state:
            state.textContent = f"{_profile_sentence(settings)} · {settings.get('entries', 0):,} recorded"
            state.dataset.on = "true" if level else "false"

    async def _profile_filter_dialog(self, tid: str) -> None:
        """Edit the database's profile filter in the code editor."""
        dash = self._dashes.get(tid)
        if dash is None:
            return
        current = dash["settings"].get("filter")
        text = to_shell(current) if current else "{\n  \n}"
        examples = "".join(
            f'<button type="button" class="mg-btn mg-chip" data-example="{_esc(body)}">{_esc(label)}</button>'
            for label, body in _PROFILE_FILTER_EXAMPLES)
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"Profile filter — {dash['db']}",
                                        width=760, height=600))
        modal.body.innerHTML = (
            '<div class="mg-editor">'
            '<div class="mg-editor-hint">A query on the fields of a profiler entry — <code>op</code>, '
            "<code>millis</code>, <code>ns</code>, <code>planSummary</code>, <code>docsExamined</code>, "
            "<code>appName</code>… While it is set, <b>Filtered</b> (level 1) records what it matches "
            "instead of what is slower than the threshold, and the server log uses it too. "
            "<b>All</b> still records everything. MongoDB keeps it in its own form, so it may read "
            "back differently.</div>"
            f'<div class="mg-chips"><span class="mg-bar-label">Examples</span>{examples}</div>'
            '<textarea class="mg-code mg-tabbable mg-editor-text" spellcheck="false"'
            ' aria-label="Profile filter"></textarea>'
            '<div class="mg-editor-error" hidden></div>'
            '<div class="mg-editor-actions">'
            '<span class="mg-editor-keys">Ctrl+S saves</span>'
            + ('<button type="button" class="mg-btn mg-danger" data-editor="remove">'
               '<span class="mdi mdi-filter-remove-outline"></span><span>Remove filter</span></button>'
               if current else "")
            + '<button type="button" class="mg-btn" data-editor="cancel">Cancel</button>'
            '<button type="button" class="mg-btn mg-primary" data-editor="save">'
            '<span class="mdi mdi-content-save"></span><span>Save</span></button>'
            "</div></div>"
        )
        area = modal.body.querySelector(".mg-editor-text")
        error = modal.body.querySelector(".mg-editor-error")
        area.value = text
        mounted: dict = {}

        def _close() -> None:
            if mounted:
                mounted["editor"].destroy()
                mounted.clear()
            modal.close()

        async def _save(remove: bool = False) -> None:
            body = "" if remove else str(area.value)
            result = await MongoService().set_profile_filter_async(dash["conn"], dash["db"], body)
            if not result.get("ok"):
                error.hidden = False
                error.textContent = result.get("error", "Could not set the filter")
                return
            _close()
            self._toast(f"{dash['db']}: profile filter {'set' if result.get('filter') else 'removed'}.", kind="success")
            self._profiler_settings(tid, result)

        def _on_click(event) -> None:
            target = event.target
            example = target.closest("[data-example]") if hasattr(target, "closest") else None
            if example:
                value = str(example.getAttribute("data-example"))
                area.value = value
                if mounted:
                    mounted["editor"].setValue(value, len(value))
                    mounted["editor"].focus()
                return
            which = target.closest("[data-editor]") if hasattr(target, "closest") else None
            if not which:
                return
            action = str(which.getAttribute("data-editor"))
            if action == "cancel":
                _close()
            elif action == "remove":
                _spawn(_save(remove=True), "profile filter remove")
            else:
                _spawn(_save(), "profile filter save")

        def _on_key(event) -> None:
            if event.defaultPrevented:
                return
            if str(event.key).lower() == "s" and (event.ctrlKey or event.metaKey):
                event.preventDefault()
                _spawn(_save(), "profile filter save")

        for event_name, handler in (("click", _on_click), ("keydown", _on_key)):
            proxy = create_proxy(handler)
            self._proxies.append(proxy)
            modal.body.addEventListener(event_name, proxy)
        modal.show()
        caret = 4 if not current else len(text)
        if await self._ensure_editor() and area.isConnected:
            mounted.update(self._mount_editor(
                area, role="pfilter", multiline=True, fill=True, height="100%",
                on_save=lambda: _spawn(_save(), "profile filter save"),
                on_run=lambda: _spawn(_save(), "profile filter save"),
                fields=to_js([{"path": path, "types": types} for path, types in _PROFILE_FIELDS],
                             dict_converter=js.Object.fromEntries),
            ))
            mounted["editor"].setValue(text, caret)
            mounted["editor"].focus()
            modal.body.querySelector(".mg-editor-keys").textContent = (
                "Ctrl+S or Ctrl+Enter saves · Ctrl+Space completes fields")
        else:
            area.focus()
            area.selectionStart = area.selectionEnd = caret

    async def _profiler_set(self, tid: str, level: int) -> None:
        dash = self._dashes.get(tid)
        if dash is None:
            return
        try:
            slowms = int(str(_el(f"{tid}-slowms").value).strip() or 100)
            rate = float(str(_el(f"{tid}-rate").value).strip() or 1)
        except ValueError:
            self._dash_status(tid, "The threshold and the sample rate must be numbers.")
            return
        result = await MongoService().set_profiler_async(dash["conn"], dash["db"], level, slowms, rate)
        if not result.get("ok"):
            self._dash_status(tid, result.get("error", "Could not change the profiler"))
            return
        self._dash_status(tid)
        if level == 2:
            self._toast("Recording every operation slows the server. Turn it off when done.", kind="warning")
        await self._after_profiler_change(dash["conn"], dash["db"], result)
        await self._profiler_rows(tid)

    async def _profiler_clear(self, tid: str) -> None:
        dash = self._dashes.get(tid)
        if dash is None or not await message.confirm(
                f"Delete everything the profiler recorded in {dash['db']}?",
                title="Clear profiler", ok_text="Delete", danger=True):
            return
        result = await MongoService().profiler_clear_async(dash["conn"], dash["db"])
        if not result.get("ok"):
            self._dash_status(tid, result.get("error", "Could not clear"))
            return
        self._profiler_settings(tid, result)
        await self._profiler_rows(tid)

    async def _profiler_rows(self, tid: str) -> None:
        dash = self._dashes.get(tid)
        if dash is None or dash["busy"]:
            return
        dash["busy"] = True
        try:
            values = self._profiler_values(tid)
            service = MongoService()
            show = dash["show"]
            if show == "recent":
                result = await service.profiler_entries_async(
                    dash["conn"], dash["db"], values["limit"], values["min_ms"], values["op"],
                    values["coll"])
                rows = result.get("entries") or []
            else:
                result = await service.profiler_summary_async(
                    dash["conn"], dash["db"], values["min_ms"], values["op"], values["coll"])
                rows = result.get("groups") or []
            if tid not in self._dashes or dash["show"] != show:
                return
            table = dash["table"]
            if not result.get("ok"):
                self._dash_status(tid, result.get("error", "Could not read the profile"))
                table.set_rows([])
                return
            dash["rows"] = rows
            table.set_empty_text(
                "Nothing recorded yet." if dash["settings"].get("level")
                else "Nothing recorded. Turn profiling on above, then use the database.")
            table.set_rows([_profile_row(index, row, show) for index, row in enumerate(rows)])
            summary = _el(f"{tid}-summary")
            if summary:
                noun = "operation" if show == "recent" else "query shape"
                summary.textContent = f"{len(rows):,} {noun}{'' if len(rows) == 1 else 's'}"
        finally:
            dash["busy"] = False

    def _profiler_row_action(self, tid: str, payload: dict) -> None:
        action = payload.get("action")
        rid = str(payload.get("id"))
        if action == "details":
            self._profiler_details(tid, rid)
        elif action == "open_query":
            self._open_profiled(tid, rid)

    def _profiled_row(self, tid: str, rid: str) -> dict | None:
        dash = self._dashes.get(tid)
        try:
            return dash["rows"][int(rid)] if dash else None
        except (ValueError, IndexError):
            return None

    def _open_profiled(self, tid: str, rid: str) -> None:
        """Re-run a profiled find or aggregate in a query tab, Explain at hand."""
        dash, row = self._dashes.get(tid), self._profiled_row(tid, rid)
        spec = (row or {}).get("open")
        if not spec:
            self._toast("Only a find or an aggregate can be opened in a query tab.", kind="warning")
            return
        known = {item["name"]: item for item in self._colls.get((dash["conn"], dash["db"])) or []}
        kind = "view" if (known.get(spec["coll"]) or {}).get("type") == "view" else "collection"
        view = self._open_view(dash["conn"], dash["db"], spec["coll"], kind)
        # _open_view's first query is spawned, so it runs after these land.
        if spec["mode"] == "aggregate":
            self._set_text(f"{view}-pipeline", spec["pipeline"])
            _el(f"{view}-mode").value = "aggregate"
            self._apply_mode(view, "aggregate")
        else:
            for role in ("filter", "sort", "projection"):
                if spec.get(role):
                    self._set_text(f"{view}-{role}", spec[role])

    def _profiler_details(self, tid: str, rid: str) -> None:
        row = self._profiled_row(tid, rid)
        if row is None:
            return
        shape = "count" in row

        def num(key: str) -> str:
            return "—" if row.get(key) is None else f"{row[key]:,}"

        if shape:
            facts = [("Namespace", row["ns"]), ("Operation", row["op"]),
                     ("Times recorded", num("count")), ("Average", f"{row['avg_ms']:,} ms"),
                     ("Slowest", f"{row['max_ms']:,} ms"), ("Total", f"{row['total_ms']:,} ms"),
                     ("Docs examined", num("docs_examined")), ("Keys examined", num("keys_examined")),
                     ("Returned", num("returned")), ("Plan (latest)", row.get("plan") or "—"),
                     ("Query shape hash", row.get("shape") or "—"),
                     ("Latest", _local_time(row.get("last_ts", "")))]
        else:
            facts = [("Time", _local_time(row["ts"])), ("Namespace", row["ns"]), ("Operation", row["op"]),
                     ("Duration", f"{row['millis']:,} ms"), ("Plan", row.get("plan") or "—"),
                     ("Docs examined", num("docs_examined")), ("Keys examined", num("keys_examined")),
                     ("Returned", num("returned")), ("Changed", num("affected")),
                     ("Response", format_bytes(row.get("response_length") or 0)),
                     ("App", row.get("app") or "—"), ("Client", row.get("client") or "—"),
                     ("User", row.get("user") or "—"), ("Query shape hash", row.get("shape") or "—")]
            if row.get("error"):
                facts.append(("Error", row["error"]))
        advice = _profile_advice(row)
        modal = ModalWindow(ModalConfig(dispose_on_close=True, title=f"{row['ns']} · {row['op']}",
                                        width=780, height=640))
        modal.body.innerHTML = (
            '<div class="mg-editor">'
            + (f'<div class="mg-warn">{_esc(advice)}</div>' if advice else "")
            + '<div class="mg-facts">' + "".join(
                f"<div><span>{_esc(label)}</span><b>{_esc(value)}</b></div>" for label, value in facts)
            + "</div>"
            + f'<pre class="mg-json mg-prof-command">{_esc(to_pretty(row.get("command") or {}))}</pre>'
            + '<div class="mg-editor-actions">'
            + ('<button type="button" class="mg-btn mg-primary" data-prof="open">'
               '<span class="mdi mdi-tab-plus"></span><span>Open in a query tab</span></button>'
               if row.get("open") else "")
            + '<button type="button" class="mg-btn" data-prof="close">Close</button></div></div>'
        )

        def _on_click(event) -> None:
            button = event.target.closest("[data-prof]") if hasattr(event.target, "closest") else None
            if not button:
                return
            if button.getAttribute("data-prof") == "open":
                self._open_profiled(tid, rid)
            modal.close()

        proxy = create_proxy(_on_click)
        self._proxies.append(proxy)
        modal.body.addEventListener("click", proxy)
        modal.show()

    def _toast(self, text: str, kind: str = "info") -> None:
        message.toast(text, kind=kind)


# -- dashboard and profiler: pure helpers ----------------------------------

def _tile(label: str, value: str, sub: str = "", meter: float | None = None,
          spark: str = "") -> str:
    """A stat tile: label, headline value, a muted line, and a meter or a sparkline."""
    bar = ""
    if meter is not None:
        width = max(0.0, min(1.0, meter)) * 100
        bar = f'<div class="mg-meter"><span style="width:{width:.1f}%"></span></div>'
    return (f'<div class="mg-tile"><div class="mg-tile-label">{_esc(label)}</div>'
            f'<div class="mg-tile-value">{_esc(value)}</div>'
            + (f'<div class="mg-tile-sub">{_esc(sub)}</div>' if sub else "")
            + bar + spark + "</div>")


def _sparkline(values: list, labels: list, name: str) -> str:
    """
    One series as inline SVG (no chart library: pytincture's CSP allows no
    CDN). A transparent column per sample carries its tooltip.
    """
    if len(values) < 2:
        return '<div class="mg-spark mg-spark-empty">measuring…</div>'
    width, height = 120, 32
    top = max(values) or 1
    step = width / (len(values) - 1)
    points = [(i * step, height - 2 - (value / top) * (height - 4)) for i, value in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    hits = "".join(
        f'<rect x="{max(0.0, x - step / 2):.1f}" y="0" width="{step:.1f}" height="{height}">'
        f"<title>{_esc(label)}</title></rect>"
        for (x, _y), label in zip(points, labels)
    )
    return (f'<svg class="mg-spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'role="img" aria-label="{_esc(name)}, last {len(values)} samples">'
            f'<polygon class="mg-spark-area" points="0,{height} {line} {width},{height}"/>'
            f'<polyline class="mg-spark-line" points="{line}"/>{hits}</svg>')


def _rate_text(value, is_bytes: bool) -> str:
    if value is None:
        return "—"
    if is_bytes:
        return f"{format_bytes(value)}/s"
    return f"{value:,.1f}/s" if value < 100 else f"{value:,.0f}/s"


def _duration(ms) -> str:
    seconds = int((ms or 0) // 1000)
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


def _local_time(iso: str) -> str:
    return str(js.Date.new(iso).toLocaleString()) if iso else "—"


def _ago_label(ms) -> str:
    return str(js.Date.new(js.Date.now() - (ms or 0)).toLocaleString())


def _profile_sentence(settings: dict) -> str:
    level = settings.get("level", 0)
    filtered = bool(settings.get("filter"))
    if not level:
        return "Profiling is off" + (" (a filter is set)" if filtered else "")
    if level == 1 and filtered:
        return "Recording operations that match the filter"
    rate = settings.get("sample_rate", 1.0)
    sampled = f", sampling {rate:.0%}" if rate < 1 else ""
    if level == 1:
        return f"Recording operations slower than {settings.get('slowms', 0):,} ms{sampled}"
    return f"Recording every operation{sampled}"


def _plan_icon(row: dict) -> tuple:
    plan = row.get("plan") or ""
    if row.get("collscan"):
        return "mdi-alert", "Collection scan: no index was used"
    if any(stage in plan for stage in ("IXSCAN", "COUNT_SCAN", "IDHACK", "EXPRESS")):
        return "mdi-key", "An index was used"
    return "", ""


def _profile_row(index: int, row: dict, show: str) -> dict:
    """A profiler entry or query-shape group as a table row (``show``)."""
    ns = row.get("ns") or ""
    icon, title = _plan_icon(row)

    def num(key: str) -> str:
        return "" if row.get(key) is None else f"{row[key]:,}"

    cells = {
        "rid": str(index), "op": row.get("op", ""), "coll": ns.partition(".")[2] or ns,
        "plan": row.get("plan") or "—", "plan_icon": icon, "plan_icon_title": title,
        "query": row.get("query", ""),
    }
    for key in ("docs_examined", "keys_examined", "returned", "affected"):
        cells[key] = row.get(key) or 0
    if show == "recent":
        cells.update(
            ts=row.get("ts", ""), ts_t=str(js.Date.new(row.get("ts", "")).toLocaleTimeString()),
            millis=row.get("millis", 0), millis_t=num("millis"), docs_t=num("docs_examined"),
            keys_t=num("keys_examined"), returned_t=num("returned"), affected_t=num("affected"),
            app=row.get("app", ""),
        )
        return cells
    examined, returned = row.get("docs_examined") or 0, row.get("returned") or 0
    cells.update(
        count=row.get("count", 0), count_t=num("count"),
        avg_ms=row.get("avg_ms", 0), avg_t=f"{row.get('avg_ms', 0):,}",
        max_ms=row.get("max_ms", 0), max_t=num("max_ms"),
        total_ms=row.get("total_ms", 0), total_t=num("total_ms"),
        ratio=examined / returned if returned else float(examined),
        ratio_t=f"{examined:,} : {returned:,}",
    )
    return cells


def _profile_advice(row: dict) -> str:
    """One line on what to look at, from what the profiler saw."""
    examined, returned = row.get("docs_examined") or 0, row.get("returned") or 0
    if row.get("collscan"):
        return ("Collection scan: no index served this. An index on the fields it filters "
                "(and sorts) on would.")
    if examined >= 1000 and examined > 100 * max(returned, 1):
        return (f"It examined {examined:,} documents to return {returned:,}. "
                "A more selective index would read fewer.")
    if row.get("in_memory_sort"):
        return "Sorted in memory. An index ending with the sort fields would avoid it."
    return ""


_CSS = """
:root{--mg-accent:#10b981;--mg-accent-strong:#047857;--mg-bg:#0f172a;--mg-panel:#111827;
  --mg-line:#1f2937;--mg-line-2:#334155;--mg-text:#e2e8f0;--mg-muted:#94a3b8;--mg-dim:#64748b;}
.mg-toolbar{display:flex;align-items:center;gap:4px;padding:6px 10px;height:100%;
  background:var(--mg-panel);border-bottom:1px solid var(--mg-line);font:13px system-ui,sans-serif;}
.mg-toolbar-btn{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;color:#cbd5f5;
  background:transparent;border:1px solid transparent;border-radius:6px;cursor:pointer;font:inherit;}
.mg-toolbar-btn:hover{background:var(--mg-line);border-color:var(--mg-line-2);}
.mg-toolbar-btn .mdi{font-size:16px;}
.mg-toolbar-sep{width:1px;height:20px;margin:0 6px;background:var(--mg-line-2);}
.mg-toolbar-spacer{flex:1 1 auto;}
.mg-toolbar-user{color:var(--mg-dim);font-size:12px;padding-right:6px;}

.mg-sidebar{display:flex;flex-direction:column;height:100%;min-height:0;}
.mg-brand{display:flex;align-items:center;gap:10px;padding:10px 12px;flex:0 0 auto;
  background:var(--mg-panel);border-bottom:1px solid var(--mg-line);}
.mg-brand-logo{width:40px;height:40px;border-radius:50%;object-fit:cover;
  border:1px solid var(--mg-accent-strong);background:var(--mg-bg);}
.mg-brand-text{display:flex;flex-direction:column;min-width:0;line-height:1.25;}
.mg-brand-name{font:600 14px system-ui,sans-serif;color:var(--mg-text);}
.mg-brand-sub{font:10.5px system-ui,sans-serif;color:var(--mg-dim);letter-spacing:.03em;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.mg-sidebar-tree{flex:1 1 auto;min-height:0;}

.mg-workspace{height:100%;min-height:0;}
.wapyt-tabwidget-panels:empty::after{
  content:"Double-click a collection in the sidebar to open it.";
  display:flex;align-items:center;justify-content:center;height:100%;
  color:var(--mg-dim);font:14px system-ui,sans-serif;text-align:center;padding:0 24px;}
.wapyt-tabwidget-tabs:empty{display:none;}

.mg-view,.mg-dash{display:flex;flex-direction:column;height:100%;min-height:0;
  font:13px system-ui,sans-serif;color:var(--mg-text);container-type:inline-size;}
.mg-view [hidden],.mg-dash [hidden]{display:none !important;}
/* .mg-btn is display:inline-flex, which beats the hidden attribute: without
   this a "hidden" Cancel stayed on screen after the job finished. */
.mg-btn[hidden],.mg-console [hidden]{display:none !important;}
.mg-head{display:flex;align-items:center;gap:6px;flex:0 0 auto;padding:7px 12px;
  background:var(--mg-bg);border-bottom:1px solid var(--mg-line);color:var(--mg-muted);
  white-space:nowrap;overflow:hidden;}
.mg-head strong{color:var(--mg-text);}
.mg-head .mdi{font-size:15px;color:var(--mg-dim);}
.mg-head-sep{color:var(--mg-line-2);}
.mg-head-stats{margin-left:auto;color:var(--mg-dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;}
.mg-query{display:flex;flex-direction:column;gap:6px;flex:0 0 auto;padding:8px 10px;
  background:var(--mg-panel);border-bottom:1px solid var(--mg-line);}
.mg-qrow{display:flex;align-items:flex-start;gap:6px;}
.mg-qrow > .mg-input{flex:1 1 0;min-width:0;}
.mg-grow{flex:1 1 auto;min-width:0;}
.mg-cm{flex:1 1 0;min-width:0;}
.mg-cm .cm-editor{min-height:31px;}
.mg-cm-fill{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;}
.mg-cm-fill .cm-editor{flex:1 1 auto;min-height:0;}
.mg-qbuttons{display:flex;gap:4px;flex:0 0 auto;}
.mg-btn[aria-pressed="true"]{border-color:var(--mg-accent);color:#6ee7b7;}
.mg-stages{display:flex;flex-direction:column;gap:6px;max-height:45vh;overflow:auto;
  padding-right:2px;}
.mg-stages-empty{padding:10px 4px;}
.mg-stage{border:1px solid var(--mg-line-2);border-radius:8px;background:var(--mg-bg);
  padding:6px;display:flex;flex-direction:column;gap:5px;}
.mg-stage[data-enabled="false"]{opacity:.55;border-style:dashed;}
.mg-stage-head{display:flex;align-items:center;gap:4px;}
.mg-stage-num{min-width:20px;text-align:center;color:var(--mg-dim);font:600 11px system-ui,sans-serif;}
.mg-stage-op{font:600 12px ui-monospace,Menlo,Consolas,monospace;color:var(--mg-accent);
  padding:3px 6px;}
.mg-stage-spacer{flex:1 1 auto;}
.mg-stage-body{resize:vertical;}
.mg-danger-icon:hover:not(:disabled){background:#7f1d1d;color:#fecaca;}
.mg-icon-btn[aria-pressed="true"]{color:#fbbf24;}
.mg-builder{display:flex;flex-direction:column;gap:6px;padding:8px;border:1px dashed var(--mg-line-2);
  border-radius:8px;background:var(--mg-bg);}
.mg-qb-rows{display:flex;flex-direction:column;gap:5px;}
.mg-qb-row{display:flex;align-items:center;gap:6px;}
.mg-qb-field{flex:0 1 240px;min-width:120px;}
.mg-qb-op{flex:0 0 108px;}
.mg-qb-type{flex:0 0 108px;}
.mg-qb-value{flex:1 1 auto;min-width:100px;}
.mg-qb-foot{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
.mg-seg .mg-seg-text{width:auto;padding:0 10px;font:600 11px system-ui,sans-serif;letter-spacing:.04em;}
.mg-qb-preview{flex:1 1 200px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  padding:5px 8px;border-radius:5px;background:var(--mg-panel);color:#6ee7b7;
  font:12px ui-monospace,Menlo,Consolas,monospace;}
.mg-qb-preview[data-state="error"]{color:#fca5a5;}
.mg-qb-note{padding:5px 8px;border-radius:5px;background:var(--mg-panel);color:#fcd34d;
  font:12px system-ui,sans-serif;}
.mg-input,.mg-select{background:var(--mg-bg);color:var(--mg-text);border:1px solid var(--mg-line-2);
  border-radius:6px;padding:6px 8px;font:12.5px system-ui,sans-serif;box-sizing:border-box;}
.mg-input:focus,.mg-select:focus{outline:none;border-color:var(--mg-accent);
  box-shadow:0 0 0 3px rgba(16,185,129,.18);}
.mg-code{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
textarea.mg-input{resize:vertical;min-height:31px;line-height:1.45;}
.mg-mode{flex:0 0 auto;width:118px;font-weight:600;color:var(--mg-accent);}
.mg-check{display:inline-flex;align-items:center;gap:5px;color:var(--mg-muted);padding-top:7px;}
.mg-hint{color:var(--mg-dim);font-size:12px;padding-top:6px;}
.mg-hint code{color:var(--mg-muted);}
.mg-btn{display:inline-flex;align-items:center;gap:6px;padding:6px 11px;color:#cbd5f5;
  background:#1e293b;border:1px solid var(--mg-line-2);border-radius:6px;cursor:pointer;
  font:12.5px system-ui,sans-serif;white-space:nowrap;}
.mg-btn:hover:not(:disabled){background:var(--mg-line-2);}
.mg-btn:disabled{opacity:.45;cursor:not-allowed;}
.mg-btn .mdi{font-size:15px;}
.mg-primary{background:var(--mg-accent-strong);border-color:var(--mg-accent);color:#ecfdf5;}
.mg-primary:hover:not(:disabled){background:#065f46;}
.mg-danger{background:#7f1d1d;border-color:#b91c1c;color:#fee2e2;}
.mg-danger:hover:not(:disabled){background:#991b1b;}
.mg-bar{display:flex;align-items:center;gap:4px;flex:0 0 auto;padding:6px 10px;
  background:var(--mg-bg);border-bottom:1px solid var(--mg-line);flex-wrap:wrap;}
.mg-bar .mg-btn{background:transparent;border-color:transparent;padding:5px 9px;}
.mg-bar .mg-btn:hover:not(:disabled){background:var(--mg-line);border-color:var(--mg-line-2);}
.mg-bar-sep{width:1px;height:18px;margin:0 4px;background:var(--mg-line-2);}
.mg-bar-spacer{flex:1 1 auto;}
.mg-seg{display:inline-flex;gap:2px;padding:2px;margin-right:8px;background:var(--mg-panel);
  border:1px solid var(--mg-line);border-radius:7px;}
.mg-seg-btn{width:28px;height:24px;padding:0;color:var(--mg-dim);background:transparent;border:0;
  border-radius:5px;cursor:pointer;font-size:15px;}
.mg-seg-btn:hover{color:#cbd5f5;background:var(--mg-line);}
.mg-seg-btn[aria-pressed="true"]{color:var(--mg-accent);background:#1e293b;}
.mg-pager{display:inline-flex;align-items:center;gap:3px;}
.mg-icon-btn{width:26px;height:26px;padding:0;color:#cbd5f5;background:transparent;border:0;
  border-radius:5px;cursor:pointer;font-size:17px;}
.mg-icon-btn:hover:not(:disabled){background:var(--mg-line);}
.mg-icon-btn:disabled{opacity:.3;cursor:default;}
.mg-page{width:52px;text-align:center;padding:4px;}
.mg-pages{color:var(--mg-dim);min-width:34px;}
.mg-summary{color:var(--mg-muted);font-size:12px;margin-left:6px;white-space:nowrap;}
.mg-status{flex:0 0 auto;padding:6px 12px;font:12.5px ui-monospace,Menlo,Consolas,monospace;
  white-space:pre-wrap;border-bottom:1px solid var(--mg-line);}
.mg-status[data-kind="error"]{background:#2a1215;color:#fca5a5;}
.mg-status[data-kind="info"]{background:#0b1f1a;color:#6ee7b7;}
.mg-results{position:relative;flex:1 1 auto;min-height:0;}
.mg-panel{position:absolute;inset:0;min-width:0;min-height:0;}
.mg-json{margin:0;padding:10px 14px;overflow:auto;background:var(--mg-bg);color:#d1fae5;
  font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;
  border:1px solid var(--mg-line);border-radius:6px;box-sizing:border-box;}
.mg-panel.mg-json{border:0;border-radius:0;}
/* Columns have pixel widths, so the table is their sum and scrolls sideways;
   when that is narrower than the panel, stretch to fill it instead. */
.mg-panel .wapyt-datatable-table{min-width:100%;}
/* Each cell's BSON type icon (_TYPE_ICONS), tinted by type; its tooltip names it. */
.wapyt-datatable-cell-icon{opacity:.9;}
.wapyt-datatable-cell-icon.mdi-identifier{color:#fbbf24;}
.wapyt-datatable-cell-icon.mdi-format-quote-close{color:#34d399;}
.wapyt-datatable-cell-icon.mdi-numeric,.wapyt-datatable-cell-icon.mdi-decimal{color:#38bdf8;}
.wapyt-datatable-cell-icon.mdi-calendar,.wapyt-datatable-cell-icon.mdi-clock-outline{color:#a78bfa;}
.wapyt-datatable-cell-icon.mdi-toggle-switch-outline{color:#fb923c;}
.wapyt-datatable-cell-icon.mdi-barcode{color:#f472b6;}
.wapyt-datatable-cell-icon.mdi-null{color:#64748b;}
.wapyt-datatable-cell-icon.mdi-code-braces,.wapyt-datatable-cell-icon.mdi-code-brackets{color:#94a3b8;}
.mg-fill{height:100%;}
@container (max-width: 760px){
  .mg-bar .mg-btn span:not(.mdi){display:none;}
  .mg-qbuttons .mg-btn span:not(.mdi){display:none;}
  .mg-summary{display:none;}
}

.mg-dialog{display:flex;flex-direction:column;gap:10px;}
.mg-test-row{display:flex;align-items:center;gap:10px;padding-top:4px;}
.mg-test-result{font:12.5px ui-monospace,Menlo,Consolas,monospace;color:var(--mg-muted);}
.mg-test-result[data-state="ok"]{color:#34d399;}
.mg-test-result[data-state="fail"]{color:#f87171;}
.mg-file{color:var(--mg-muted);}
.mg-restore-result{max-height:180px;}
.mg-editor{display:flex;flex-direction:column;gap:8px;height:100%;min-height:0;}
.mg-editor-hint{color:var(--mg-dim);font-size:12px;}
.mg-editor-text{flex:1 1 auto;min-height:0;resize:none;padding:10px 12px;border-radius:6px;
  background:var(--mg-bg);color:#d1fae5;border:1px solid var(--mg-line-2);line-height:1.5;
  tab-size:2;}
.mg-editor-text:focus{outline:none;border-color:var(--mg-accent);}
.mg-editor-error{padding:6px 10px;border-radius:6px;background:#2a1215;color:#fca5a5;
  font:12.5px ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;}
.mg-nag{display:flex;flex-direction:column;gap:10px;padding:4px 2px;font:13px/1.55 system-ui,sans-serif;color:#cbd5f5;}
.mg-nag p{margin:0;}
.mg-update{display:inline-flex;align-items:center;gap:5px;margin-right:8px;padding:4px 10px;
  border-radius:999px;border:1px solid rgba(16,185,129,.45);background:rgba(16,185,129,.12);
  color:#6ee7b7;font:600 12px system-ui,sans-serif;cursor:pointer;}
.mg-update[hidden]{display:none;}
.mg-update:hover{background:rgba(16,185,129,.22);}
.mg-about{display:flex;flex-direction:column;gap:14px;padding:4px 2px;font:13px/1.5 system-ui,sans-serif;color:#cbd5f5;}
.mg-about-head{display:flex;align-items:center;gap:14px;}
.mg-about-logo{width:56px;height:56px;border-radius:50%;}
.mg-about-name{font-size:20px;font-weight:600;color:var(--mg-text);}
.mg-about-version{font:600 13px ui-monospace,Menlo,Consolas,monospace;color:#6ee7b7;margin-left:4px;}
.mg-about-sub{color:var(--mg-muted);font-size:12px;}
.mg-about-update{padding:8px 10px;border-radius:6px;background:var(--mg-panel);border:1px solid var(--mg-line-2);}
.mg-about-update .mdi-arrow-up-circle{color:#34d399;}
.mg-about-update a,.mg-about-link{color:#6ee7b7;}
.mg-about-links{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.mg-about-link{display:flex;align-items:flex-start;gap:9px;padding:9px 10px;border-radius:6px;
  border:1px solid var(--mg-line-2);text-decoration:none;}
.mg-about-link:hover{background:var(--mg-line);}
.mg-about-link .mdi{font-size:20px;line-height:1.1;}
.mg-about-link b{display:block;color:var(--mg-text);font-weight:600;}
.mg-about-link small{color:var(--mg-muted);font-size:11.5px;}
.mg-nag .mdi-shield-alert-outline{color:#fbbf24;font-size:17px;vertical-align:-2px;}
.mg-nag code{padding:1px 5px;border-radius:4px;background:var(--mg-panel);color:#fde68a;}
.mg-nag .mg-editor-actions{margin-top:6px;}
.mg-editor-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex:0 0 auto;}
.mg-editor-keys{margin-right:auto;color:var(--mg-dim);font-size:11.5px;}
.mg-split{display:flex;flex-direction:column;gap:10px;height:100%;min-height:0;}
.mg-split-top{flex:1 1 auto;min-height:0;border:1px solid var(--mg-line);border-radius:6px;overflow:hidden;}
.mg-split-bottom{flex:1 1 auto;min-height:0;}
.mg-split-bottom-auto{flex:0 0 auto;}
.mg-console{display:flex;flex-direction:column;gap:10px;height:100%;min-height:0;}
.mg-console-head{display:flex;align-items:center;justify-content:space-between;gap:10px;}
.mg-console-state{display:inline-flex;align-items:center;gap:7px;font-weight:600;color:var(--mg-text);}
.mg-console-state .mdi{font-size:18px;color:#94a3b8;}
.mg-console-state[data-state="done"] .mdi{color:#34d399;}
.mg-console-state[data-state="failed"]{color:#fca5a5;}
.mg-console-state[data-state="failed"] .mdi{color:#f87171;}
.mg-console-state[data-state="cancelled"] .mdi{color:#fbbf24;}
.mg-console-elapsed{color:var(--mg-dim);font:12px ui-monospace,Menlo,Consolas,monospace;}
.mg-console-items{display:flex;flex-direction:column;gap:6px;max-height:40%;overflow:auto;flex:0 0 auto;}
.mg-console-item{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(80px,1fr) minmax(0,1.2fr);
  align-items:center;gap:10px;font-size:12.5px;}
.mg-console-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mg-text);
  font-family:ui-monospace,Menlo,Consolas,monospace;}
.mg-console-track{height:6px;border-radius:3px;background:#1e293b;overflow:hidden;}
.mg-console-bar{display:block;height:100%;border-radius:3px;background:#38bdf8;transition:width .3s linear;}
.mg-console-item[data-state="done"] .mg-console-bar{background:#34d399;}
.mg-console-item[data-state="failed"] .mg-console-bar{background:#f87171;}
.mg-console-item[data-state="cancelled"] .mg-console-bar{background:#fbbf24;}
.mg-console-amount{color:var(--mg-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font:12px ui-monospace,Menlo,Consolas,monospace;}
.mg-console-log{flex:1 1 auto;min-height:80px;}
.mg-plan{padding:10px 12px;border-radius:6px;background:#0b1f1a;border:1px solid #065f46;
  display:flex;flex-direction:column;gap:6px;}
.mg-plan[data-strategy="drop-then-build"]{background:#2a1d06;border-color:#92400e;}
.mg-plan-head{display:flex;align-items:center;gap:8px;color:var(--mg-text);font-weight:600;}
.mg-plan-head .mdi{font-size:17px;color:#34d399;}
.mg-plan[data-strategy="drop-then-build"] .mg-plan-head .mdi{color:#fbbf24;}
.mg-plan-how{color:var(--mg-muted);font-size:12.5px;line-height:1.45;}
.mg-warn{padding:8px 12px;border-radius:6px;background:#3b2506;color:#fcd34d;}
.mg-preview-count{font-size:14px;color:var(--mg-text);}
.mg-preview-label{color:var(--mg-dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em;}
.mg-kv{border-collapse:collapse;width:100%;font:13px system-ui,sans-serif;}
.mg-kv th{text-align:left;font-weight:500;color:var(--mg-muted);padding:6px 12px 6px 0;
  white-space:pre;vertical-align:top;width:40%;}
.mg-kv td{color:var(--mg-text);padding:6px 0;font-family:ui-monospace,Menlo,Consolas,monospace;
  word-break:break-word;}
.mg-kv kbd{padding:2px 6px;border:1px solid var(--mg-line-2);border-radius:4px;background:var(--mg-bg);}

/* Dashboard and query profiler */
.mg-dash-body{flex:1 1 auto;min-height:0;overflow:auto;padding:12px 14px 18px;
  display:flex;flex-direction:column;gap:18px;}
.mg-dash-section h3{display:flex;align-items:baseline;gap:10px;margin:0 0 8px;
  font:600 11px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.06em;color:var(--mg-muted);}
.mg-dash-section h3 small{font-weight:400;text-transform:none;letter-spacing:0;color:var(--mg-dim);font-size:11.5px;}
.mg-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px;}
.mg-tile{display:flex;flex-direction:column;gap:2px;min-width:0;padding:10px 12px;
  border:1px solid var(--mg-line);border-radius:8px;background:var(--mg-panel);}
.mg-tile-label{color:var(--mg-muted);font-size:11.5px;}
.mg-tile-value{font:600 19px/1.25 system-ui,sans-serif;color:var(--mg-text);
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mg-tile-sub{color:var(--mg-dim);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mg-meter{height:6px;margin-top:6px;border-radius:3px;background:#1e293b;overflow:hidden;}
.mg-meter span{display:block;height:100%;border-radius:3px;background:var(--mg-accent);}
.mg-spark{display:block;width:100%;height:32px;margin-top:6px;overflow:visible;}
.mg-spark-line{fill:none;stroke:var(--mg-accent);stroke-width:2;stroke-linejoin:round;
  stroke-linecap:round;vector-effect:non-scaling-stroke;}
.mg-spark-area{fill:rgba(16,185,129,.12);stroke:none;}
.mg-spark rect{fill:transparent;}
.mg-spark rect:hover{fill:rgba(148,163,184,.14);}
.mg-spark-empty{display:flex;align-items:flex-end;color:var(--mg-dim);font-size:11px;}
.mg-dash-table{position:relative;height:280px;border:1px solid var(--mg-line);border-radius:6px;overflow:hidden;}
.mg-dash-table .wapyt-datatable-table{min-width:100%;}
.mg-check-inline{padding-top:0;margin-left:6px;}
.mg-bar-label{color:var(--mg-muted);font-size:12px;margin:0 4px 0 2px;}
.mg-num{width:84px;padding:4px 6px;}
.mg-prof-coll{width:180px;padding:5px 8px;}
.mg-prof-settings{background:var(--mg-panel);}
.mg-prof-settings .mg-seg{margin-right:4px;}
.mg-head-stats[data-on="true"]{color:#fbbf24;}
.mg-chips{display:flex;flex-wrap:wrap;align-items:center;gap:5px;flex:0 0 auto;}
.mg-chips .mg-bar-label{margin-left:0;}
.mg-chip{padding:3px 9px;border-radius:999px;font-size:12px;}
.mg-editor-hint code{color:var(--mg-muted);}
.mg-prof-command{flex:1 1 auto;min-height:100px;margin:0;}
.mg-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 28px;flex:0 0 auto;}
.mg-facts div{display:flex;justify-content:space-between;gap:12px;min-width:0;
  padding-bottom:4px;border-bottom:1px solid var(--mg-line);}
.mg-facts span{color:var(--mg-muted);font-size:12.5px;white-space:nowrap;}
.mg-facts b{font:500 12.5px ui-monospace,Menlo,Consolas,monospace;color:var(--mg-text);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.wapyt-datatable-cell-icon.mdi-record-circle{color:#f59e0b;}
.wapyt-datatable-cell-icon.mdi-circle-outline{color:#64748b;}
.wapyt-datatable-cell-icon.mdi-alert{color:#f87171;}
.wapyt-datatable-cell-icon.mdi-key{color:#34d399;}
"""
