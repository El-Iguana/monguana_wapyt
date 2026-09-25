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

        step("JSON and tree views")
        view.locator("[data-mg=view][data-view=json]").click()
        json_text = view.locator(f"#{tid}-json").inner_text()
        assert '"$numberDecimal"' in json_text and '"$date"' in json_text
        shot(page, "03-json")
        view.locator("[data-mg=view][data-view=tree]").click()
        expect(view.locator(f"#{tid}-tree .wapyt-tree-row").first).to_be_visible()
        shot(page, "04-tree")
        view.locator("[data-mg=view][data-view=table]").click()

        step("edit a document in the editor, keep its types")
        rows.first.dblclick()
        editor = page.locator(".wapyt-modal-overlay").last
        area = editor.locator(".mg-editor-text")
        expect(area).to_be_visible(timeout=10000)
        text = area.input_value()
        assert '"$date"' in text and '"$numberDecimal"' in text
        area.fill(text.replace('"status": "', '"status": "edited-', 1))
        shot(page, "05-editor")
        area.press("Control+s")
        expect(page.locator("#mg-toast")).to_have_text("Saved.", timeout=10000)
        page.wait_for_timeout(800)
        assert "edited-" in rows.first.inner_text()

        step("updateMany shows a preview, then applies")
        view.locator(f"#{tid}-mode").select_option("updateMany")
        filter_box.fill('{status: /^edited-/}')
        view.locator(f"#{tid}-update").fill('{$set: {status: "paid"}}')
        view.locator("[data-mg=run]").click()
        confirm = page.locator(".wapyt-modal-overlay").last
        expect(confirm.locator(".mg-preview-count")).to_contain_text("1 document(s) match")
        shot(page, "06-preview")
        confirm.locator('[data-confirm="go"]').click()
        expect(page.locator("#mg-toast")).to_contain_text("modified 1", timeout=10000)
        expect(view.locator(f"#{tid}-mode")).to_have_value("find")

        step("aggregate")
        view.locator(f"#{tid}-mode").select_option("aggregate")
        view.locator(f"#{tid}-pipeline").fill(
            '[{$group: {_id: "$status", n: {$sum: 1}}}, {$sort: {n: -1}}]'
        )
        view.locator(f"#{tid}-pipeline").press("Control+Enter")
        expect(view.locator(".mg-summary")).to_have_text("4 result(s)", timeout=10000)
        shot(page, "07-aggregate")

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

        step("dump a collection, restore it into another database")
        orders.click(button="right")
        with page.expect_download() as info:
            menu.locator("text=Dump collection").click()
        dump_path = info.value.path()
        server_row.click(button="right")
        menu.locator("text=Restore dump…").click()
        restore = page.locator(".wapyt-modal-overlay").last
        restore.locator(".mg-file").set_input_files(
            {"name": "shop_orders.zip", "mimeType": "application/zip",
             "buffer": Path(dump_path).read_bytes()}
        )
        restore.locator('input[name="db"]').fill("shop_copy")
        restore.locator(".wapyt-form-button-primary").click()
        expect(restore.locator(".mg-restore-result")).to_contain_text(
            "shop_copy.orders: 137 inserted, 2 index(es)", timeout=20000
        )
        shot(page, "11-restore")
        restore.locator(".wapyt-form-button-ghost").click()
        copy = page.locator(".wapyt-tree-row[data-node-id$=':shop_copy']").first
        expect(copy).to_be_visible(timeout=10000)
        copy.click(button="right")
        page.once("dialog", lambda dialog: dialog.accept("shop_copy"))
        menu.locator("text=Drop database…").click()
        expect(copy).to_have_count(0, timeout=10000)

        violations = page.evaluate("window.__csp")
        problems.extend(f"CSP: {item}" for item in violations)

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
