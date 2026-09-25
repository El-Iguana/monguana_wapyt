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
        page = browser.new_page(viewport={"width": 1500, "height": 950})
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

        step("filter with shell syntax: dates and nested fields")
        tid = view.get_attribute("data-tab")
        view.locator(f"#{tid}-filter").fill(
            '{status: "paid", placed: {$gte: ISODate("2026-03-01")}, "customer.vip": false}'
        )
        view.locator(f"#{tid}-filter").press("Enter")
        page.wait_for_timeout(1200)
        summary = view.locator(".mg-summary").inner_text()
        print("    filtered:", summary)
        assert "of" in summary and "137" not in summary, summary

        step("header click sorts on the server")
        view.locator(f"#{tid}-filter").fill("")
        view.locator(f"#{tid}-filter").press("Enter")
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
        view.locator(f"#{tid}-filter").fill('{status: /^edited-/}')
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
            "shop_copy.orders: 137 inserted, 1 index(es)", timeout=20000
        )
        shot(page, "11-restore")
        restore.locator(".wapyt-form-button-ghost").click()
        copy = page.locator(".wapyt-tree-row[data-node-id$=':shop_copy']").first
        expect(copy).to_be_visible(timeout=10000)
        copy.click(button="right")
        page.once("dialog", lambda dialog: dialog.accept("shop_copy"))
        menu.locator("text=Drop database…").click()
        expect(copy).to_have_count(0, timeout=10000)

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
