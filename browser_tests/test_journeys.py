"""Browser journeys use fictional reviews and in-process fake providers only."""

import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from dashboard import DashboardHandler, DashboardServer
from dashboard_data import DashboardData
from job_manager import AutomationService
from llm_provider import ProviderError, ProviderRegistry
from review_store import ReviewStore
from showcase import NAMES, ShowcaseService, manifest, prepare


class QuietHandler(DashboardHandler):
    def log_message(self, *args):
        pass


class BrowserJourneys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except BaseException:
            cls.playwright.stop()
            raise
        expect.set_options(timeout=15000)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.calls, self.errors, self.external = [], [], []

    def app(self, *, showcase=False, failure=False, width=1280, research_rows=None):
        folder = Path(self.folder.name) / "workspace"
        if showcase:
            data = prepare(folder)
            service = ShowcaseService(data)
        else:
            data = DashboardData(folder)
            data.seed_demo()
            with ReviewStore(data.path("demo")) as store:
                for asin, name in NAMES.items():
                    store.set_product_name(asin, name)
            registry = ProviderRegistry(environ={}, transport=lambda payload: self.complete(payload, failure))
            registry.configure({"api_key": "synthetic-browser-test-key"})
            def collect(urls, database, auth_file, *, cancelled, **options):
                deadline = time.monotonic() + 10
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(0.02)
                return {"status": "paused", "cancelled": cancelled(), "products": [], "pages_processed": 0, "inserted": 0}
            service = AutomationService(data, folder / "unused.json", provider_registry=registry, collection_runner=collect,
                settings=lambda: {"configured": False}, session_reader=lambda path: {"valid": True, "exists": True})
        self.addCleanup(service.close)
        if research_rows:
            names = patch.dict("dashboard_data.DEMO_NAMES", {"B0TEST0001": "Fictional Travel Bottle"})
            names.start()
            self.addCleanup(names.stop)
            with ReviewStore(data.path("demo")) as store:
                store.upsert_reviews(research_rows, product_names={"B0TEST0001": "Fictional Travel Bottle"})
        self.service = service
        server = DashboardServer(("127.0.0.1", 0), data, automation=service, showcase=manifest(data) if showcase else None)
        server.RequestHandlerClass = QuietHandler
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"
        context = self.browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce", accept_downloads=True)
        self.addCleanup(context.close)
        def guard(route):
            if route.request.url.startswith(self.base + "/"):
                route.continue_()
            else:
                self.external.append(urlsplit(route.request.url).hostname)
                route.abort()
        context.route("**/*", guard)
        self.page = context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(self.base + "/#buying")
        expect(self.page.locator("#dataset-select")).to_have_value("demo")
        expect(self.page.locator("#buying-submit")).to_be_enabled()
        return self.page

    def complete(self, payload, failure):
        self.calls.append(payload)
        if failure:
            raise ProviderError("Synthetic provider capacity limit", code="rate_limit", diagnostics={"rate_limit_source": "provider"})
        packet = json.loads(payload["messages"][-1]["content"])
        answer = {"relationship": "alternatives"}
        for product in packet["products"]:
            row = next(item for item in packet["review_evidence"]["reviews"] if item["product"] == product["product"])
            label = next(iter(row["body"] or row["title"]))
            key = product["product"]
            answer.update({f"{key}_benefit": "One selected account describes everyday use.", f"{key}_benefit_aspect": "Everyday use",
                f"{key}_benefit_sources": [label], f"{key}_concern": "", f"{key}_concern_aspect": "",
                f"{key}_concern_sources": [], f"{key}_unknowns": "Current price is not established."})
        return {"model": "fictional/browser:free", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30, "cost": 0}}

    def compare(self, ai=True):
        page = self.page
        page.get_by_role("link", name="Compare products", exact=True).click()
        page.get_by_role("combobox", name="First product", exact=True).fill("B0DEMO0001")
        page.get_by_role("combobox", name="Second product", exact=True).fill("B0DEMO0002")
        page.get_by_role("checkbox", name=re.compile("Add AI interpretation")).set_checked(ai)
        page.get_by_role("button", name="Compare products", exact=True).click()
        expect(page.get_by_role("heading", name="Your product comparison", exact=True)).to_be_visible()

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.external, [])

    def test_showcase_inspection_sources_and_guided_comparison(self):
        page = self.app(showcase=True)
        guide = page.get_by_role("region", name="Start exploring the demo")
        expect(guide).to_contain_text("fictional")
        guide.get_by_role("button", name="1. Inspect a fan", exact=True).click()
        expect(page.locator("#buying-product")).to_have_value("B0DEMO0001")
        expect(page.locator("#buying-product-preview")).to_contain_text("North Desk Fan")
        expect(page.locator("#buying-product-preview")).to_contain_text("5 saved reviews")
        expect(page.get_by_role("button", name="Inspect saved reviews", exact=True)).to_be_enabled()
        page.locator("#buying-submit").click()
        expect(page.locator("#buying-results")).to_contain_text("North Desk Fan")
        guide.get_by_text("Read an example interpretation", exact=True).click()
        expect(guide).to_contain_text("not a live AI answer")
        guide.get_by_role("button", name="Open this fictional source", exact=True).first.click()
        expect(page.get_by_role("dialog")).to_contain_text("Fictional review")
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).not_to_be_visible()
        guide.get_by_role("button", name="2. Compare the fans", exact=True).click()
        expect(page.locator("#compare-first")).to_have_value("B0DEMO0001")
        expect(page.locator("#compare-second")).to_have_value("B0DEMO0002")
        page.get_by_role("button", name="Compare products", exact=True).click()
        expect(page.get_by_role("heading", name="Your product comparison", exact=True)).to_be_visible()
        self.assertEqual(self.calls, [])

    def test_inspection_preview_validates_links_and_marks_optional_ai(self):
        page = self.app()
        product = page.locator("#buying-product")
        product.fill("https://www.amazon.in/Fan/dp/B0DEMO0001?tag=fictional")
        expect(page.locator("#buying-product-preview")).to_contain_text("North Desk Fan")
        page.locator("#buying-consent").check()
        expect(page.get_by_role("button", name="Build AI interpretation", exact=True)).to_be_enabled()
        product.fill("https://amazon.in.evil.invalid/dp/B0DEMO0001")
        expect(page.locator("#buying-product-preview")).to_contain_text("full HTTPS amazon.in")
        expect(page.get_by_role("button", name="Inspect saved reviews", exact=True)).to_be_enabled()
        product.fill("B0TEST0001")
        expect(page.locator("#buying-product-preview")).to_contain_text("Product not saved in this workspace")
        self.assertEqual(self.calls, [])

    def test_scoped_research_discloses_variant_exclusions_and_preserves_sources(self):
        def row(identity, body):
            return {"asin": "B0TEST0001", "review_id": identity, "review_title": "Fictional account",
                    "review_body": body, "rating": "4", "product_url": "https://www.amazon.in/dp/B0TEST0001"}
        page = self.app(showcase=True, width=390, research_rows=[
            row("BLUE", "Blue variant. No overheating in my use."),
            row("RED", "Red variant. It overheats."),
            row("UNKNOWN", "It overheats during charging.")])
        page.get_by_role("button", name="Open navigation", exact=True).click()
        page.get_by_role("link", name="Research workspace", exact=True).click()
        page.get_by_role("button", name="Open navigation", exact=True).click()
        page.get_by_role("link", name="Ask reviews", exact=True).click()
        page.locator("#research-product").select_option("B0TEST0001")
        expect(page.locator("#research-mode")).to_be_disabled()
        expect(page.locator("#research-mode option:checked")).to_have_text("Focused local search")
        expect(page.locator("#research-provider-note")).to_be_hidden()
        page.locator("#research-question").fill("Does the blue variant overheat?")
        page.locator("#research-submit").click()
        result = page.locator("#research-results")
        scope = result.get_by_role("region", name="Evidence selection", exact=True)
        expect(scope).to_contain_text("Fictional Travel Bottle")
        expect(scope).to_contain_text("Fictional demo sample")
        expect(scope).to_contain_text("Color: blue")
        expect(scope).to_contain_text("1 excluded without a clear variant")
        expect(scope).to_contain_text("1 excluded for conflicting")
        expect(result.locator(".evidence-card")).to_have_count(1)
        expect(result.locator(".evidence-card")).to_contain_text("No overheating")
        result.get_by_text("Why this review matched the variant", exact=True).click()
        expect(result.locator(".brief-source-details")).to_contain_text("Blue variant")
        result.get_by_role("button", name="Read source review", exact=True).click()
        expect(page.get_by_role("dialog")).to_contain_text("No overheating")
        page.keyboard.press("Escape")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        self.assertEqual(self.calls, [])

    def test_comparison_sources_export_and_checked_cache_reuse(self):
        page = self.app()
        self.compare()
        results = page.locator("#compare-results")
        expect(results).to_contain_text("AI INTERPRETATION + OWNER SOURCES")
        results.get_by_text("Check 1 source review", exact=True).first.click()
        results.get_by_role("button", name="Read original review", exact=True).first.click()
        expect(page.get_by_role("dialog")).to_contain_text("Fictional review")
        page.keyboard.press("Escape")
        with page.expect_download() as download:
            page.get_by_role("button", name="Save comparison with sources", exact=True).click()
        text = Path(download.value.path()).read_text(encoding="utf-8")
        self.assertIn("North Desk Fan", text)
        self.assertIn("Fictional review", text)
        self.assertNotIn("synthetic-browser-test-key", text)
        self.assertNotIn("Demo reviewer", text)
        self.compare()
        expect(results).to_contain_text("No new AI request")
        self.assertEqual(len(self.calls), 1)
        results.get_by_text("How this run worked", exact=True).click()
        expect(results).to_contain_text("Reused saved work")

    def test_provider_failure_keeps_local_sources_and_does_not_retry(self):
        page = self.app(failure=True)
        self.compare()
        expect(page.locator("#compare-results")).to_contain_text("LOCAL OWNER EVIDENCE")
        expect(page.locator("#compare-results")).to_contain_text(re.compile("limiting requests|unavailable", re.I))
        self.assertEqual(len(self.calls), 1)

    def test_collection_can_be_cancelled_without_a_provider_request(self):
        page = self.app()
        page.get_by_role("link", name="Research workspace", exact=True).click()
        page.get_by_role("link", name="Collection", exact=True).click()
        page.get_by_role("textbox", name="Product URLs", exact=True).fill("https://www.amazon.in/dp/B0DEMO0001")
        page.locator("#collection-analyse").uncheck()
        page.get_by_role("button", name="Start collection", exact=True).click()
        page.get_by_role("button", name="Stop job", exact=True).first.click()
        expect(page.get_by_text("CANCELLED", exact=True).first).to_be_visible()
        self.assertEqual(self.calls, [])

    def test_small_screen_keyboard_entry_and_empty_search_recovery(self):
        page = self.app(showcase=True, width=390)
        page.keyboard.press("Tab")
        expect(page.get_by_role("link", name="Skip to content", exact=True)).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#main-content")).to_be_focused()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        page.get_by_role("button", name="Open navigation", exact=True).click()
        page.get_by_role("link", name=re.compile("Products")).click()
        search = page.get_by_role("searchbox", name="Find a product", exact=True)
        search.fill("No matching fictional product")
        expect(page.get_by_text("No products match your search", exact=False)).to_be_visible()
        search.fill("North")
        expect(page.get_by_role("heading", name="North Desk Fan", exact=True)).to_be_visible()
        page.get_by_role("button", name="2. Compare the fans", exact=True).click()
        page.get_by_role("button", name="Compare products", exact=True).click()
        expect(page.get_by_role("heading", name="Your product comparison", exact=True)).to_be_visible()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)


if __name__ == "__main__":
    unittest.main()
