"""The demonstration must stay separate from personal data and credentials."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from showcase import NAMES, ShowcaseService, manifest, prepare
from llm_provider import ProviderError


class ShowcaseTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "showcase"

    def test_repeated_start_keeps_named_fictional_data_and_examples_match_sources(self):
        data = prepare(self.path)
        prepare(self.path)
        overview = data.overview("demo")
        self.assertEqual(overview["summary"]["total_reviews"], 15)
        self.assertEqual({item["name"] for item in overview["products"]}, set(NAMES.values()))
        info = manifest(data)
        self.assertIn("not a live AI answer", info["example_label"])
        for example in info["examples"]:
            source = example["source"]
            review = data.review("demo", source["asin"], source["review_id"])
            self.assertEqual(source["quote"], review["review_body"])
        self.assertFalse(data.path("workspace").exists())

    def test_non_showcase_folder_is_refused_without_touching_its_contents(self):
        self.path.mkdir()
        sentinel = self.path / "personal.txt"
        sentinel.write_text("Keep this", encoding="utf-8")
        with self.assertRaises(ValueError):
            prepare(self.path)
        self.assertEqual(list(self.path.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(), "Keep this")

    def test_saved_and_environment_credentials_are_never_loaded_and_external_jobs_are_refused(self):
        data = prepare(self.path)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "SYNTHETIC-ENV-KEY"}), patch("credential_store.system_credential_store", side_effect=AssertionError("No vault access")):
            service = ShowcaseService(data, autostart=False)
            self.addCleanup(service.close)
            self.assertFalse(service.provider_metadata()["configured"])
            self.assertEqual(service.provider_metadata()["key_source"], "none")
            for payload in [{"kind": "auth"}, {"kind": "collection"},
                            {"kind": "research", "dataset": "demo", "question": "Battery?", "allow_external": True},
                            {"kind": "analysis", "dataset": "demo", "analysis_method": "openai"},
                            {"kind": "analysis", "dataset": "workspace"}]:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    service.submit(payload)
            with self.assertRaises(ProviderError):
                service.check_provider_quota()
            with self.assertRaises(ProviderError):
                service.configure_provider({"api_key": "SYNTHETIC"})
            self.assertEqual(service.status()["jobs"], [])

