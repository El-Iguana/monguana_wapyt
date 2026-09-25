"""
Drive the real UI in Chromium against a real MongoDB.

Not collected by pytest (no ``test_`` prefix): it needs the service running,
a MongoDB with the ``shop`` sample (``seed_shop.py``) and Playwright.

    MONGUANA_ADMIN_PASS=... uv run python service.py &
    uv run python tests/smoke/seed_shop.py
    python3 tests/smoke/ui_smoke.py

Environment: MONGUANA_URL (default http://127.0.0.1:8766/monguana),
MONGUANA_USER / MONGUANA_PASS (required) for the app login, and SMOKE_MONGO_HOST,
SMOKE_MONGO_PORT, SMOKE_MONGO_USER, SMOKE_MONGO_PASS for the server profile it
creates. Screenshots land next to this file (git-ignored).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

HERE = Path(__file__).resolve().parent
APP = os.environ.get("MONGUANA_URL", "http://127.0.0.1:8766/monguana")
USER = os.environ.get("MONGUANA_USER", "admin")
PASS = os.environ.get("MONGUANA_PASS", "")
MONGO = {
    "host": os.environ.get("SMOKE_MONGO_HOST", "127.0.0.1"),
    "port": os.environ.get("SMOKE_MONGO_PORT", "27018"),
    "username": os.environ.get("SMOKE_MONGO_USER", "root"),
    "password": os.environ.get("SMOKE_MONGO_PASS", "p@ss:w/rd"),
}
PROFILE = "smoke-local"


def shot(page, name: str) -> None:
    page.screenshot(path=str(HERE / f"{name}.png"))


def step(label: str) -> None:
    print(f"--> {label}", flush=True)


def main() -> int:
    if not PASS:
        print("Set MONGUANA_PASS to the app's admin password.", file=sys.stderr)
        return 2
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1500, "height": 950})
        # Every CSP violation, not just the ones that surface as console
        # errors: the editor bundle has to live inside pytincture's policy.
        context.add_init_script(
            "window.__csp = [];"
            "document.addEventListener('securitypolicyviolation', e =>"
            " window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));"
        )
        page = context.new_page()
        page.on("pageerror", lambda exc: problems.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: problems.append(f"console.{msg.type}: {msg.text}")
                if msg.type == "error" else None)
        page.on("dialog", lambda dialog: dialog.accept() if dialog.type == "confirm" else None)

        step("login")
        page.goto(APP, wait_until="domcontentloaded", timeout=30000)
        if "/login" in page.url:
            page.fill('input[name="email"]', USER)
            page.fill('input[name="password"]', PASS)
            page.click('input[type="submit"], button[type="submit"]')
        page.wait_for_selector(".mg-toolbar", timeout=180000)
        page.wait_for_timeout(800)
        assert page.title() == "Monguana", page.title()

        step("create a connection profile, test it, save it")
        existing = page.locator(f".wapyt-tree-row:has-text('{PROFILE}')")
        if existing.count() == 0:
            page.click("[data-top=new_conn]")
            modal = page.locator(".wapyt-modal-overlay").last
            modal.locator('input[name="name"]').fill(PROFILE)
            modal.locator('input[name="host"]').fill(MONGO["host"])
            modal.locator('input[name="port"]').fill(MONGO["port"])
            modal.locator('input[name="username"]').fill(MONGO["username"])
            modal.locator('input[name="password"]').fill(MONGO["password"])
            modal.locator("[data-test]").click()
            expect(modal.locator(".mg-test-result")).to_contain_text("✓", timeout=15000)
            shot(page, "01-connection-editor")
            modal.locator(".wapyt-form-button-primary").click()
            expect(existing).to_have_count(1, timeout=10000)

        step("expand server, database; open a collection")
        page.locator(f".wapyt-tree-row:has-text('{PROFILE}')").first.click()
        shop = page.locator(".wapyt-tree-row[data-node-id$=':shop']").first
        expect(shop).to_be_visible(timeout=15000)
        shop.click()
        orders = page.locator(".wapyt-tree-row[data-node-id$=':shop:orders']").first
        expect(orders).to_be_visible(timeout=15000)
        orders.dblclick()
        view = page.locator(".mg-view").last
        rows = view.locator(".wapyt-datatable-table tbody tr[data-row-id]")
        expect(rows).to_have_count(50, timeout=20000)
        expect(view.locator(".mg-summary")).to_have_text("1–50 of 137")
        expect(view.locator(".mg-head-stats")).to_contain_text("137 docs")
        shot(page, "02-collection")

        step("the filter box is a CodeMirror editor (ROADMAP phase 31)")
        tid = view.get_attribute("data-tab")
        filter_box = view.locator("[data-role=filter-editor] .cm-content")
        expect(filter_box).to_be_visible(timeout=15000)
        assert not view.locator(f"#{tid}-filter").is_visible(), "the textarea should be hidden"
        filter_box.click()
        page.keyboard.type("{status: {$gt")
        completions = page.locator(".cm-tooltip-autocomplete li")
        expect(completions.first).to_contain_text("$gt", timeout=5000)
        # Enter straight away -- inside CodeMirror's 75 ms accept guard, which
        # once let a newline through -- must neither run the query nor break
        # the line.
        page.keyboard.press("Enter")
        page.wait_for_timeout(600)
        expect(view.locator(".mg-summary")).to_have_text("1–50 of 137")
        page.keyboard.type(": ObjectI")
        expect(completions.first).to_contain_text("ObjectId", timeout=5000)
        page.keyboard.press("Escape")
        # Brackets auto-close; the hidden textarea mirrors the whole text.
        assert "\n" not in view.locator(f"#{tid}-filter").input_value()
        assert view.locator(f"#{tid}-filter").input_value().startswith("{status: {$gt"), \
            view.locator(f"#{tid}-filter").input_value()
        shot(page, "02b-editor")

        def set_filter(text: str) -> None:
            filter_box.fill(text)
            filter_box.press("Enter")

        step("field paths complete without opening Fields (background sample)")
        sort_box = view.locator("[data-role=sort-editor] .cm-content")
        sort_box.click()
        page.keyboard.type("{customer.add")
        expect(page.locator(".cm-tooltip-autocomplete li").first).to_contain_text(
            "customer.address", timeout=10000)
        page.keyboard.press("Escape")
        sort_box.fill("")

        step("filter with shell syntax: dates and nested fields")
        set_filter(
            '{status: "paid", placed: {$gte: ISODate("2026-03-01")}, "customer.vip": false}'
        )
        page.wait_for_timeout(1200)
        summary = view.locator(".mg-summary").inner_text()
        print("    filtered:", summary)
        assert "of" in summary and "137" not in summary, summary

        step("header click sorts on the server")
        set_filter("")
        page.wait_for_timeout(800)
        view.locator("th[data-column-id='number']").click()  # asc
        page.wait_for_timeout(800)
        view.locator("th[data-column-id='number']").click()  # desc
        page.wait_for_timeout(1200)
        assert view.locator(f"#{tid}-sort").input_value() == '{"number": -1}'
        first = rows.first.locator("td[data-column-id='number']").inner_text()
        assert first == "1136", f"expected the global max, got {first}"

        step("pagination: last page")
        view.locator("[data-mg=last]").click()
        page.wait_for_timeout(1200)
        expect(view.locator(".mg-summary")).to_have_text("101–137 of 137")
        view.locator("[data-mg=first]").click()
        page.wait_for_timeout(1000)

        step("columns: resize by dragging an edge, reorder by dragging a header (phase 35)")
        def header_order(scope):
            return scope.locator("thead th[data-column-id]").evaluate_all(
                "els => els.map(e => e.dataset.columnId)")

        status_th = view.locator("th[data-column-id='status']")
        before_width = status_th.bounding_box()["width"]
        grip = status_th.locator(".wapyt-datatable-resizer").bounding_box()
        page.mouse.move(grip["x"] + grip["width"] / 2, grip["y"] + grip["height"] / 2)
        page.mouse.down()
        page.mouse.move(grip["x"] + 60, grip["y"] + grip["height"] / 2, steps=6)
        page.mouse.move(grip["x"] + 95, grip["y"] + grip["height"] / 2, steps=6)
        page.mouse.up()
        resized_width = status_th.bounding_box()["width"]
        assert abs(resized_width - (before_width + 95)) <= 4, (before_width, resized_width)
        # The drag must not have sorted by status.
        assert view.locator(f"#{tid}-sort").input_value() == '{"number": -1}'
        view.locator("th[data-column-id='total']").drag_to(
            view.locator("th[data-column-id='number']"), target_position={"x": 4, "y": 10})
        order = header_order(view)
        assert order.index("total") < order.index("number"), order
        page.wait_for_timeout(900)  # the save is debounced

        step("JSON and tree views")
        view.locator("[data-mg=view][data-view=json]").click()
        json_text = view.locator(f"#{tid}-json").inner_text()
        assert '"$numberDecimal"' in json_text and '"$date"' in json_text
        shot(page, "03-json")
        view.locator("[data-mg=view][data-view=tree]").click()
        expect(view.locator(f"#{tid}-tree .wapyt-tree-row").first).to_be_visible()
        shot(page, "04-tree")
        view.locator("[data-mg=view][data-view=table]").click()

        step("edit a document in the code editor, keep its types")
        rows.first.dblclick()
        editor = page.locator(".wapyt-modal-overlay").last
        doc_box = editor.locator("[data-role=document-editor] .cm-content")
        expect(doc_box).to_be_visible(timeout=10000)
        mirror = editor.locator(".mg-editor-text")  # hidden, the source of truth
        text = mirror.input_value()
        assert '"$date"' in text and '"$numberDecimal"' in text
        # Escape that closes a completion list must not close the dialog.
        doc_box.press("Control+End")
        page.keyboard.type("$in")
        expect(page.locator(".cm-tooltip-autocomplete")).to_be_visible(timeout=5000)
        page.keyboard.press("Escape")
        expect(page.locator(".cm-tooltip-autocomplete")).to_have_count(0)
        expect(doc_box).to_be_visible()
        doc_box.fill(text.replace('"status": "', '"status": "edited-', 1))
        shot(page, "05-editor")
        doc_box.press("Control+s")
        expect(page.locator("#mg-toast")).to_have_text("Saved.", timeout=10000)
        page.wait_for_timeout(800)
        assert "edited-" in rows.first.inner_text()

        step("updateMany shows a preview, then applies")
        view.locator(f"#{tid}-mode").select_option("updateMany")
        filter_box.fill('{status: /^edited-/}')
        update_box = view.locator("[data-role=update-editor] .cm-content")
        update_box.fill('{$set: {status: "paid"}}')
        view.locator("[data-mg=run]").click()
        confirm = page.locator(".wapyt-modal-overlay").last
        expect(confirm.locator(".mg-preview-count")).to_contain_text("1 document(s) match")
        shot(page, "06-preview")
        confirm.locator('[data-confirm="go"]').click()
        expect(page.locator("#mg-toast")).to_contain_text("modified 1", timeout=10000)
        expect(view.locator(f"#{tid}-mode")).to_have_value("find")

        step("aggregate: the pipeline as stage cards (phase 34)")
        view.locator(f"#{tid}-mode").select_option("aggregate")
        stages_host = view.locator(f"#{tid}-stages")
        expect(stages_host).to_contain_text("No stages yet")
        add_stage = view.locator("select[data-role=stage]")
        cards = stages_host.locator(".mg-stage")

        def card_box(n: int):
            return cards.nth(n).locator(".cm-content")

        add_stage.select_option("$match")
        expect(cards).to_have_count(1)
        card_box(0).fill('{status: {$ne: "cancelled"}}')
        add_stage.select_option("$group")
        expect(card_box(1)).to_contain_text("$sum")  # the template, in the card
        card_box(1).fill('{_id: "$status", n: {$sum: 1}}')
        add_stage.select_option("$sort")
        card_box(2).fill("{n: -1}")
        raw = view.locator(f"#{tid}-pipeline")  # hidden: what Run sends
        assert raw.input_value().startswith("[\n  {$match: {status: {$ne:"), raw.input_value()
        card_box(2).press("Control+Enter")
        expect(view.locator(".mg-summary")).to_have_text("3 result(s)", timeout=10000)
        shot(page, "07-aggregate")

        # Run to here: only the $match stage.
        cards.nth(0).locator("[data-mg=st_run]").click()
        expect(view.locator(".mg-status")).to_contain_text("After stage 1 ($match)", timeout=10000)
        after_match = view.locator(".mg-summary").inner_text()
        assert after_match not in ("3 result(s)", "137 result(s)"), after_match

        # Reorder: $sort up above $group.
        cards.nth(2).locator("[data-mg=st_up]").click()
        expect(cards.nth(1).locator("[data-stage-op]")).to_have_value("$sort")
        cards.nth(1).locator("[data-mg=st_down]").click()
        expect(cards.nth(2).locator("[data-stage-op]")).to_have_value("$sort")

        # Disable $match: it becomes a comment and no longer runs.
        cards.nth(0).locator("[data-mg=st_toggle]").click()
        expect(cards.nth(0)).to_have_attribute("data-enabled", "false")
        assert "/* off: {$match" in raw.input_value(), raw.input_value()
        view.locator("[data-mg=run]").click()
        expect(view.locator(".mg-summary")).to_have_text("4 result(s)", timeout=10000)

        # Raw and back: nothing is lost, the disabled stage included.
        view.locator('[data-mg=pl_mode][data-plmode="raw"]').click()
        raw_box = view.locator("[data-role=pipeline-editor] .cm-content")
        expect(raw_box).to_be_visible()
        expect(raw_box).to_contain_text("/* off: {$match")
        expect(stages_host).to_be_hidden()
        view.locator('[data-mg=pl_mode][data-plmode="stages"]').click()
        expect(cards).to_have_count(3)
        expect(cards.nth(0)).to_have_attribute("data-enabled", "false")

        # A raw pipeline that does not split stays raw, with the reason.
        view.locator('[data-mg=pl_mode][data-plmode="raw"]').click()
        raw_box.fill("[{$match: {a: 1}, $limit: 2}]")
        view.locator('[data-mg=pl_mode][data-plmode="stages"]').click()
        expect(view.locator(".mg-status")).to_contain_text("more than one key")
        expect(raw_box).to_be_visible()
        raw_box.fill('[{$group: {_id: "$status", n: {$sum: 1}}}, {$limit: 2}]')
        view.locator('[data-mg=pl_mode][data-plmode="stages"]').click()
        expect(cards).to_have_count(2)
        expect(view.locator(".mg-status")).to_be_hidden()
        cards.nth(1).locator("[data-mg=st_remove]").click()
        expect(cards).to_have_count(1)
        assert "$limit" not in raw.input_value()
        shot(page, "07c-stages")

        step("visual query builder: typed values, live preview, apply")
        view.locator(f"#{tid}-mode").select_option("find")
        view.locator("[data-mg=reset]").click()
        page.wait_for_timeout(600)
        view.locator("[data-mg=builder]").click()
        builder = view.locator(f"#{tid}-builder")
        expect(builder).to_be_visible()
        rows_qb = builder.locator(".mg-qb-row")
        rows_qb.nth(0).locator("[data-qb=field]").fill("status")
        rows_qb.nth(0).locator("[data-qb=field]").press("Tab")
        rows_qb.nth(0).locator("[data-qb=op]").select_option("$in")
        rows_qb.nth(0).locator("[data-qb=value]").fill("paid, new")
        builder.locator("[data-mg=qb_add]").click()
        rows_qb.nth(1).locator("[data-qb=field]").fill("placed")
        rows_qb.nth(1).locator("[data-qb=field]").press("Tab")
        # The sampled type of `placed` is Date, so values are written as dates.
        expect(rows_qb.nth(1).locator("[data-qb=type]")).to_have_value("date")
        rows_qb.nth(1).locator("[data-qb=op]").select_option("$gte")
        rows_qb.nth(1).locator("[data-qb=value]").fill("yesterday")
        preview = builder.locator(".mg-qb-preview")
        expect(preview).to_have_attribute("data-state", "error")
        expect(preview).to_contain_text("Row 2")
        rows_qb.nth(1).locator("[data-qb=value]").fill("2026-03-01")
        built = '{status: {$in: ["paid", "new"]}, placed: {$gte: ISODate("2026-03-01")}}'
        expect(preview).to_have_text(built)
        shot(page, "07b-builder")
        builder.locator("[data-mg=qb_apply]").click()
        page.wait_for_timeout(1200)
        assert view.locator(f"#{tid}-filter").input_value() == built
        expect(filter_box).to_have_text(built)
        applied = view.locator(".mg-summary").inner_text()
        assert "of" in applied and "137" not in applied, applied
        view.locator("[data-mg=builder]").click()
        expect(builder).to_be_hidden()

        step("fields dialog")
        view.locator(f"#{tid}-mode").select_option("find")
        view.locator("[data-mg=reset]").click()
        page.wait_for_timeout(800)
        view.locator("[data-mg=fields]").click()
        fields = page.locator(".wapyt-modal-overlay").last
        expect(fields.locator("tr[data-row-id='customer.address.city']")).to_be_visible(timeout=10000)
        shot(page, "08-fields")
        fields.locator("tr[data-row-id='customer.address.city']").dblclick()
        assert view.locator(f"#{tid}-filter").input_value() == "{customer.address.city: }"

        step("tree context menu differs by node kind")
        server_row = page.locator(f".wapyt-tree-row:has-text('{PROFILE}')").first
        server_row.click(button="right")
        menu = page.locator(".wapyt-tree-menu:not([hidden])")
        expect(menu.locator("text=Create database…")).to_be_visible()
        expect(menu.locator("text=Drop…")).to_be_hidden()
        page.keyboard.press("Escape")
        orders.click(button="right")
        expect(menu.locator("text=Indexes…")).to_be_visible()
        expect(menu.locator("text=Create database…")).to_be_hidden()
        shot(page, "09-context-menu")
        page.keyboard.press("Escape")

        step("indexes dialog")
        orders.click(button="right")
        menu.locator("text=Indexes…").click()
        indexes = page.locator(".wapyt-modal-overlay").last
        expect(indexes.locator("tr[data-row-id='status_1_placed_-1']")).to_be_visible(timeout=10000)
        shot(page, "10-indexes")

        step("index CRUD: create with options, edit in place, rebuild, hide")
        create = indexes.locator("[data-slot=form]")
        create.locator('input[name="keys"]').fill("{number: 1}")
        create.locator('input[name="options"]').fill("{collation: {locale: 'en', strength: 2}}")
        create.locator(".wapyt-form-button-primary").click()
        number_row = indexes.locator("tr[data-row-id='number_1']")
        expect(number_row).to_contain_text("collation", timeout=10000)

        def edit_index(row_id: str):
            indexes.locator(f"tr[data-row-id='{row_id}']").dblclick()
            dialog = page.locator(".wapyt-modal-overlay").last
            expect(dialog.locator(".wapyt-modal-title")).to_have_text(f"Edit index {row_id}")
            return dialog

        dialog = edit_index("number_1")
        # Unchanged: nothing to do.
        dialog.locator(".wapyt-form-button-primary").click()
        expect(page.locator("#mg-toast")).to_have_text("Nothing to change.", timeout=10000)
        dialog.locator('input[name="unique"]').check()
        dialog.locator(".wapyt-form-button-primary").click()
        plan = dialog.locator("[data-slot=plan]")
        expect(plan).to_have_attribute("data-strategy", "in-place", timeout=10000)
        expect(plan).to_contain_text("unique on")
        shot(page, "10b-index-plan")
        plan.locator("[data-plan=apply]").click()
        expect(number_row).to_contain_text("unique", timeout=10000)

        dialog = edit_index("number_1")
        dialog.locator('input[name="name"]').fill("by_number")
        dialog.locator(".wapyt-form-button-primary").click()
        expect(plan := dialog.locator("[data-slot=plan]")).to_have_attribute(
            "data-strategy", "build-then-drop", timeout=10000)
        plan.locator("[data-plan=apply]").click()
        expect(indexes.locator("tr[data-row-id='by_number']")).to_contain_text("unique", timeout=15000)
        expect(indexes.locator("tr[data-row-id='number_1']")).to_have_count(0)

        indexes.locator("tr[data-row-id='by_number']").click(button="right")
        page.locator(".wapyt-datatable-menu:not([hidden]) >> text=Hide / unhide").click()
        expect(indexes.locator("tr[data-row-id='by_number']")).to_contain_text("hidden", timeout=10000)
        shot(page, "10c-indexes-edited")
        indexes.locator(".wapyt-modal-close").click()

        step("export refuses a broken filter, then exports every match")
        view.locator("[data-mg=export_csv]").click()
        expect(view.locator(".mg-status")).to_contain_text("Unexpected '}' (line 1", timeout=10000)
        view.locator("[data-mg=reset]").click()
        page.wait_for_timeout(800)
        with page.expect_download() as info:
            view.locator("[data-mg=export_csv]").click()
        csv_text = Path(info.value.path()).read_text(encoding="utf-8-sig")
        lines = csv_text.strip().splitlines()
        assert lines[0].startswith("_id,number,status"), lines[0]
        assert len(lines) == 138, len(lines)

        step("dump a collection as a job with progress, restore it into another database")
        orders.click(button="right")
        with page.expect_download(timeout=30000) as info:
            menu.locator("text=Dump collection").click()
            console = page.locator(".wapyt-modal-overlay").last
            expect(console.locator(".mg-console-state")).to_have_attribute(
                "data-state", "done", timeout=30000)
        dump_path = info.value.path()
        dumped = console.locator(".mg-console-item").first
        expect(dumped).to_contain_text("shop.orders")
        expect(dumped).to_contain_text("137 documents")
        expect(console.locator(".mg-console-log")).to_contain_text("✓ shop.orders: 137 documents")
        expect(console.locator('[data-job="download"]')).to_be_visible()
        expect(console.locator('[data-job="cancel"]')).to_be_hidden()
        shot(page, "11a-dump-console")
        console.locator('[data-job="close"]').click()

        server_row.click(button="right")
        menu.locator("text=Restore dump…").click()
        restore = page.locator(".wapyt-modal-overlay").last
        restore.locator(".mg-file").set_input_files(
            {"name": "shop_orders.zip", "mimeType": "application/zip",
             "buffer": Path(dump_path).read_bytes()}
        )
        restore.locator('input[name="db"]').fill("shop_copy")
        restore.locator(".wapyt-form-button-primary").click()
        # The upload finishes, the dialog closes, and the job's console opens.
        console = page.locator(".wapyt-modal-overlay").last
        expect(console.locator(".wapyt-modal-title")).to_contain_text("Restore shop_orders.zip into shop_copy",
                                                                      timeout=20000)
        expect(console.locator(".mg-console-state")).to_have_attribute("data-state", "done", timeout=30000)
        restored = console.locator(".mg-console-item").first
        expect(restored).to_contain_text("shop_copy.orders")
        expect(restored).to_contain_text("137 inserted, 2 index(es)")
        expect(restored).to_have_attribute("data-state", "done")
        shot(page, "11-restore")
        console.locator('[data-job="close"]').click()
        copy = page.locator(".wapyt-tree-row[data-node-id$=':shop_copy']").first
        expect(copy).to_be_visible(timeout=10000)
        copy.click(button="right")
        page.once("dialog", lambda dialog: dialog.accept("shop_copy"))
        menu.locator("text=Drop database…").click()
        expect(copy).to_have_count(0, timeout=10000)

        violations = page.evaluate("window.__csp")
        problems.extend(f"CSP: {item}" for item in violations)

        step("the column layout comes back on a fresh page, and resets")
        again = context.new_page()
        again.on("pageerror", lambda exc: problems.append(f"layout pageerror: {exc}"))
        again.goto(APP, wait_until="domcontentloaded", timeout=30000)
        again.wait_for_selector(".mg-toolbar", timeout=180000)
        again.locator(f".wapyt-tree-row:has-text('{PROFILE}')").first.click()
        again.locator(".wapyt-tree-row[data-node-id$=':shop']").first.click()
        again.locator(".wapyt-tree-row[data-node-id$=':shop:orders']").first.dblclick()
        reopened = again.locator(".mg-view").last
        expect(reopened.locator(".wapyt-datatable-table tbody tr[data-row-id]")).to_have_count(50, timeout=20000)
        again.wait_for_timeout(800)  # the layout loads alongside the first page
        order = header_order(reopened)
        assert order.index("total") < order.index("number"), order
        restored = reopened.locator("th[data-column-id='status']").bounding_box()["width"]
        assert abs(restored - resized_width) <= 4, (restored, resized_width)
        reopened.locator("[data-mg=reset_columns]").click()
        expect(again.locator("#mg-toast")).to_have_text("Column widths and order reset.", timeout=10000)
        order = header_order(reopened)
        assert order.index("number") < order.index("total"), order
        again.close()

        step("without the editor bundle, the plain text box still works")
        fallback = context.new_page()
        fallback.route("**/vendor/codemirror/**", lambda route: route.abort())
        fallback.on("pageerror", lambda exc: problems.append(f"fallback pageerror: {exc}"))
        fallback.goto(APP, wait_until="domcontentloaded", timeout=30000)
        fallback.wait_for_selector(".mg-toolbar", timeout=180000)
        fallback.locator(f".wapyt-tree-row:has-text('{PROFILE}')").first.click()
        fallback.locator(".wapyt-tree-row[data-node-id$=':shop']").first.click()
        fallback.locator(".wapyt-tree-row[data-node-id$=':shop:orders']").first.dblclick()
        plain = fallback.locator(".mg-view").last
        expect(plain.locator(".wapyt-datatable-table tbody tr[data-row-id]")).to_have_count(50, timeout=20000)
        fallback.wait_for_timeout(1500)
        assert plain.locator(".cm-editor").count() == 0
        ftid = plain.get_attribute("data-tab")
        plain.locator(f"#{ftid}-filter").fill('{status: "new"}')
        plain.locator(f"#{ftid}-filter").press("Enter")
        expect(plain.locator(".mg-summary")).not_to_have_text("1–50 of 137", timeout=10000)
        plain.locator("[data-role=sort]").fill("{number: 1}")
        assert plain.locator("[data-role=pipeline]").count() == 1
        plain.locator(f"#{ftid}-mode").select_option("aggregate")
        plain.locator("select[data-role=stage]").select_option("$limit")
        plain_stage = plain.locator(".mg-stage textarea[data-stage-body]")
        expect(plain_stage).to_be_visible()
        plain_stage.fill("3")
        plain.locator("[data-mg=run]").click()
        expect(plain.locator(".mg-summary")).to_have_text("3 result(s)", timeout=10000)
        plain.locator(f"#{ftid}-mode").select_option("find")
        plain.locator("[data-mg=run]").click()
        expect(plain.locator(".mg-summary")).to_contain_text("of", timeout=10000)
        plain.locator(".wapyt-datatable-table tbody tr[data-row-id]").first.dblclick()
        plain_doc = fallback.locator(".wapyt-modal-overlay").last.locator(".mg-editor-text")
        expect(plain_doc).to_be_visible(timeout=10000)
        assert '"_id"' in plain_doc.input_value()

        browser.close()

    noise = [line for line in problems if "favicon" not in line]
    if noise:
        print("\nBrowser problems:")
        print("\n".join(f"  {line}" for line in noise))
        return 1
    print("\nOK — no browser errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
