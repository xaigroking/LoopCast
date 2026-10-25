"""Production standalone UI smoke and fixture workflow through the shared origin."""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request

from playwright.async_api import async_playwright, expect

BASE = os.environ["LOOPCAST_BASE_URL"]


def api(path, body=None, key=None):
    headers = {"Origin": BASE}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = key or "browser-test-key"
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            value = response.read()
            return response.status, json.loads(value) if "json" in response.headers.get("Content-Type", "") else value
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


async def main():
    page_errors = []
    external = []
    async with async_playwright() as tool:
        browser = await tool.chromium.launch(channel="chromium")
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("request", lambda request: external.append(request.url) if not request.url.startswith(BASE) else None)
        response = await page.goto(BASE + "/")
        assert response and response.status == 200
        headers = response.headers
        assert "nonce-" in headers.get("content-security-policy", "")
        assert headers.get("x-content-type-options") == "nosniff"
        await expect(page.get_by_role("heading", name="Your task console")).to_be_visible()
        await expect(page.get_by_text("A fresh start.", exact=True)).to_be_visible()
        for path in ["/settings", "/runs/" + "0" * 32]:
            result = await page.request.get(BASE + path)
            assert result.status == 200
        await page.locator('aside nav a[href="/settings"]').click()
        await expect(page.get_by_role("heading", name="Settings")).to_be_visible()
        language = page.locator("#language-select")
        options = await language.locator("option").all_text_contents()
        assert len(options) == 21, options
        registry_response = await page.request.get(BASE + "/locales/registry.json")
        assert registry_response.status == 200
        locale_registry = await registry_response.json()
        for locale in locale_registry:
            await language.select_option(locale["code"])
            await expect(page.locator("html")).to_have_attribute("lang", locale["code"])
            await expect(page.locator("html")).to_have_attribute("dir", locale["dir"])
        await language.select_option("ar")
        await expect(page.locator("html")).to_have_attribute("dir", "rtl")
        await language.select_option("en")
        await page.locator("#theme-select").select_option("dark")
        await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        await page.get_by_role("link", name="Your task console").click()
        await page.get_by_role("button", name="+ New fixture").click()
        await page.get_by_label("Task name").fill("preserved browser draft")
        await page.get_by_label("Scenario").select_option("fail_once")
        await page.locator("#dialog-settings").click()
        await page.locator("#language-select").select_option("zh-Hant")
        await page.go_back()
        await expect(page.locator("main .heading .primary")).to_be_visible()
        await page.locator("main .heading .primary").click()
        await expect(page.locator('input[name="title"]')).to_have_value("preserved browser draft")
        await expect(page.locator('select[name="scenario"]')).to_have_value("fail_once")
        await page.locator("#dismiss").click()
        await page.locator('aside nav a[href="/settings"]').click()
        await page.locator("#language-select").select_option("en")
        await page.get_by_role("link", name="Your task console").click()

        seen_keys = []
        async def lose_first_create(route):
            if route.request.method != "POST":
                return await route.continue_()
            seen_keys.append(route.request.headers.get("idempotency-key"))
            response = await route.fetch()
            if len(seen_keys) == 1:
                await route.abort("failed")
            else:
                await route.fulfill(response=response)
        await page.route("**/v1/runs", lose_first_create)
        await page.get_by_role("button", name="+ New fixture").click()
        await page.get_by_label("Task name").fill("production browser fixture")
        await page.get_by_label("Scenario").select_option("success")
        await page.get_by_label("Execution delay (ms)").fill("40")
        await page.get_by_role("button", name="Create fixture →").click()
        await expect(page.locator("#form-error")).to_contain_text("retry safely")
        await page.get_by_role("button", name="Create fixture →").click()
        await page.wait_for_url("**/runs/*")
        await page.unroute("**/v1/runs", lose_first_create)
        assert len(seen_keys) == 2 and seen_keys[0] == seen_keys[1]
        await expect(page.locator('[data-state="SUCCEEDED"]').first).to_be_visible(timeout=15000)
        await expect(page.get_by_role("link", name="↗ Open fixture report", exact=False)).to_be_visible()
        await expect(page.get_by_text("Publication simulator")).to_be_visible()
        run_id = page.url.rstrip("/").split("/")[-1]
        run_status, run = api("/v1/runs/" + run_id)
        assert run_status == 200 and run["state"] == "SUCCEEDED"
        await page.get_by_role("button", name="Add simulated target").click()
        await expect(page.locator("[data-intent-id]")).to_have_count(1)
        await page.get_by_label("Simulator outcome").select_option("lost_response")
        await page.get_by_role("button", name="Dispatch simulation").click()
        await expect(page.locator('[data-state="UNKNOWN"]')).to_be_visible(timeout=10000)
        await page.get_by_role("button", name="Check simulator receipt").click()
        await expect(page.locator('[data-state="PUBLISHED"]')).to_be_visible(timeout=10000)
        assert len([item for item in api("/v1/runs?limit=100")[1]["items"] if item["input"]["title"] == "production browser fixture"]) == 1
        assert api("/v1/runs")[0] == 200
        assert api("/v1", body={})[0] == 404
        assert api("/v1/unknown")[0] == 404

        # Malformed success and server errors are ambiguous write outcomes too.
        # Each explicit retry must keep its identity; no automatic POST replay.
        ambiguous_keys = []
        async def ambiguous_create(route):
            if route.request.method != 'POST':
                await route.continue_(); return
            ambiguous_keys.append(route.request.headers.get('idempotency-key'))
            result = await route.fetch()
            if len(ambiguous_keys) == 1:
                await route.fulfill(status=200, content_type='application/json', body='{')
            elif len(ambiguous_keys) == 2:
                await route.fulfill(status=500, content_type='application/json', body='{"error":{"code":"INTERNAL_ERROR"}}')
            else:
                await route.fulfill(response=result)
        await page.goto(BASE + '/')
        await page.get_by_role('button', name='+ New fixture').click()
        await page.get_by_label('Task name').fill('ambiguous response identity')
        await page.get_by_label('Scenario').select_option('success')
        await page.route('**/v1/runs', ambiguous_create)
        for count in (1, 2):
            await page.get_by_role('button', name='Create fixture →').click()
            await expect(page.locator('#form-error')).to_contain_text('retry safely')
            assert len(ambiguous_keys) == count
        await page.get_by_role('button', name='Create fixture →').click()
        await page.wait_for_url('**/runs/*')
        await page.unroute('**/v1/runs', ambiguous_create)
        assert len(ambiguous_keys) == 3 and len(set(ambiguous_keys)) == 1
        await expect(page.locator('[data-state="SUCCEEDED"]').first).to_be_visible(timeout=15000)
        assert len([item for item in api('/v1/runs?limit=100')[1]['items'] if item['input']['title'] == 'ambiguous response identity']) == 1

        await page.goto(BASE + "/")
        await page.get_by_role("button", name="+ New fixture").click()
        await page.get_by_label("Task name").fill("retry from production UI")
        await page.get_by_label("Scenario").select_option("fail_once")
        await page.get_by_label("Execution delay (ms)").fill("30")
        await page.get_by_role("button", name="Create fixture →").click()
        await page.wait_for_url("**/runs/*")
        await expect(page.locator('[data-state="FAILED"]').first).to_be_visible(timeout=15000)
        await page.get_by_role("button", name="Retry", exact=True).click()
        await expect(page.locator('[data-state="SUCCEEDED"]').first).to_be_visible(timeout=15000)

        await page.goto(BASE + "/")
        await page.get_by_role("button", name="+ New fixture").click()
        await page.get_by_label("Task name").fill("cancel from production UI")
        await page.get_by_label("Scenario").select_option("slow")
        await page.get_by_label("Execution delay (ms)").fill("1200")
        page.on("dialog", lambda dialog: dialog.accept())
        await page.get_by_role("button", name="Create fixture →").click()
        await page.wait_for_url("**/runs/*")
        await expect(page.locator('[data-state="RUNNING"]').first).to_be_visible(timeout=10000)
        await page.get_by_role("button", name="Cancel", exact=True).click()
        await expect(page.locator('[data-state="CANCELLED"]').first).to_be_visible(timeout=10000)

        status, conflict_seed = api("/v1/runs", {"title": "two-tab conflict", "scenario": "fail_once", "delay_ms": 50}, "two-tab-conflict")
        assert status == 202
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, conflict_seed = api("/v1/runs/" + conflict_seed["run_id"])
            if conflict_seed["state"] == "FAILED":
                break
            await asyncio.sleep(.05)
        assert conflict_seed["state"] == "FAILED"
        first, second = await context.new_page(), await context.new_page()
        await first.goto(BASE + "/runs/" + conflict_seed["run_id"])
        await second.goto(BASE + "/runs/" + conflict_seed["run_id"])
        # Keep the second view stale while the first follows its live retry.
        await second.route("**/events?*", lambda route: route.abort())
        await first.get_by_role("button", name="Retry", exact=True).click()
        await expect(first.locator('[data-state="SUCCEEDED"]').first).to_be_visible(timeout=10000)
        await second.get_by_role("button", name="Retry", exact=True).click()
        await expect(second.locator('[data-state="SUCCEEDED"]').first).to_be_visible(timeout=10000)
        await expect(second.locator('#notice[role="alert"]')).to_be_visible()
        await expect(second.locator('#notice')).to_contain_text('The state changed.')
        await first.close()
        await second.close()

        # An external retry changes a settled detail back into an active run.
        # Refresh must subscribe using the newly accepted snapshot.
        status, stale_seed = api('/v1/runs', {'title': 'refresh after external retry', 'scenario': 'fail_once', 'delay_ms': 700}, 'external-retry')
        assert status == 202
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, stale_seed = api('/v1/runs/' + stale_seed['run_id'])
            if stale_seed['state'] == 'FAILED': break
            await asyncio.sleep(.05)
        assert stale_seed['state'] == 'FAILED'
        await page.goto(BASE + '/runs/' + stale_seed['run_id'])
        await expect(page.locator('[data-state="FAILED"]').first).to_be_visible()
        assert api('/v1/runs/' + stale_seed['run_id'] + '/retry', {'expected_version': stale_seed['state_version']}, 'external-command')[0] == 202
        await page.locator('#refresh').click()
        await expect(page.locator('[data-state="RUNNING"]').first).to_be_visible(timeout=2000)
        await expect(page.locator('[data-state="SUCCEEDED"]').first).to_be_visible(timeout=15000)

        page.set_default_timeout(15000)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto(BASE + "/")
        await page.locator("main .heading .primary").click()
        mode = page.locator('select[name="mode"]')
        await expect(mode.locator('option[value="mpt"]')).to_be_enabled()
        await mode.select_option("mpt")
        await page.locator('input[name="title"]').fill("production MPT browser media")
        await page.get_by_role("button", name="Generate video →").click()
        await page.wait_for_url("**/runs/*")
        video = page.locator("video")
        await expect(video).to_be_visible(timeout=20000)
        await video.evaluate("element => element.play()")
        await expect(video).to_have_js_property("videoWidth", 64, timeout=10000)
        await page.wait_for_function('document.querySelector("video").currentTime > 0')
        _, generated = api("/v1/runs/" + page.url.rstrip("/").split("/")[-1])
        assert generated["mode"] == "mpt" and generated["media"]
        await expect(page.get_by_role("link", name="↗ Open video", exact=False)).to_be_visible()
        assert not page_errors, page_errors
        assert not external, external
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path="/tmp/loopcast-next-mobile.png", full_page=True)
        await browser.close()
    print("Standalone same-origin browser checks passed: CSP, routes, all locales, RTL/theme/drafts, fixture/report/retry/cancel, lost-response identity, two-tab conflict, simulator UNKNOWN/receipt, MPT video at 390px and API routing")


if __name__ == "__main__":
    asyncio.run(main())
