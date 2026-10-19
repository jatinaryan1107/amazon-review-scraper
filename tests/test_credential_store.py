"""Synthetic credential persistence, restoration and failure behavior."""

import copy
import os
from pathlib import Path
import tempfile
import unittest

from credential_store import WindowsCredentialStore
from llm_provider import ProviderError, ProviderRegistry


KEY = "synthetic-key-never-a-real-provider-token"


class MemoryVault:
    def __init__(self):
        self.value = None
        self.fail = False

    def load(self):
        if self.fail:
            raise RuntimeError(KEY)
        return copy.deepcopy(self.value)

    def save(self, value):
        if self.fail:
            raise RuntimeError(KEY)
        self.value = copy.deepcopy(value)

    def delete(self):
        if self.fail:
            raise RuntimeError(KEY)
        self.value = None


class CredentialTests(unittest.TestCase):
    def test_explicit_opt_in_restores_key_and_preferences_after_new_registry(self):
        vault = MemoryVault()
        registry = ProviderRegistry(environ={}, credential_store=vault)
        registry.configure({"api_key": KEY, "require_zdr": False, "response_mode": "schema"})
        self.assertIsNone(vault.value)
        public = registry.configure({"remember_key": True})
        self.assertEqual(public["key_source"], "saved")
        restored = ProviderRegistry(environ={}, credential_store=vault)
        self.assertEqual(restored._key(), (KEY, "saved"))
        self.assertFalse(restored.public()["require_zdr"])
        self.assertEqual(restored.public()["response_mode"], "schema")
        self.assertNotIn(KEY, str(public) + str(restored.public()))

    def test_settings_changes_persist_and_opt_out_keeps_only_current_session(self):
        vault = MemoryVault()
        registry = ProviderRegistry(environ={}, credential_store=vault)
        registry.configure({"api_key": KEY, "remember_key": True})
        registry.configure({"require_zdr": False})
        self.assertFalse(ProviderRegistry(environ={}, credential_store=vault).public()["require_zdr"])
        registry.configure({"remember_key": False})
        self.assertEqual(registry._key(), (KEY, "session"))
        self.assertIsNone(vault.value)
        self.assertFalse(ProviderRegistry(environ={}, credential_store=vault).public()["configured"])

    def test_forget_removes_memory_and_saved_copy(self):
        vault = MemoryVault()
        registry = ProviderRegistry(environ={}, credential_store=vault)
        registry.configure({"api_key": KEY, "remember_key": True})
        registry.configure({"clear": True})
        self.assertEqual(registry._key(), ("", "none"))
        self.assertIsNone(vault.value)

    def test_failed_write_or_delete_does_not_change_existing_key_or_policy(self):
        vault = MemoryVault()
        registry = ProviderRegistry(environ={}, credential_store=vault)
        registry.configure({"api_key": KEY, "remember_key": True})
        vault.fail = True
        for changes in [{"api_key": "new-synthetic-key", "require_zdr": False},
                        {"clear": True}, {"remember_key": False}]:
            with self.subTest(changes=changes), self.assertRaises(ProviderError) as caught:
                registry.configure(changes)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(registry._key(), (KEY, "saved"))
            self.assertTrue(registry.public()["require_zdr"])

    def test_bad_options_are_rejected_before_saved_data_changes(self):
        vault = MemoryVault()
        registry = ProviderRegistry(environ={}, credential_store=vault)
        registry.configure({"api_key": KEY, "remember_key": True})
        original = copy.deepcopy(vault.value)
        for changes in [{"remember_key": "yes"}, {"api_key": None, "require_zdr": False},
                        {"clear": True, "remember_key": True}, {"response_mode": []}]:
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                registry.configure(changes)
            self.assertEqual(vault.value, original)

    def test_unreadable_or_malformed_saved_keys_fail_closed_without_disclosure(self):
        vault = MemoryVault()
        for saved in [{"api_key": KEY}, {"api_key": KEY, "require_zdr": False, "response_mode": []}]:
            vault.value = saved
            public = ProviderRegistry(environ={}, credential_store=vault).public()
            self.assertFalse(public["configured"])
            self.assertTrue(public["storage_error"])
            self.assertNotIn(KEY, str(public))
        vault.fail = True
        self.assertTrue(ProviderRegistry(environ={}, credential_store=vault).public()["storage_error"])

    def test_environment_key_cannot_be_copied_to_vault_or_replaced(self):
        vault = MemoryVault()
        registry = ProviderRegistry(environ={"OPENROUTER_API_KEY": KEY}, credential_store=vault)
        for changes in [{"remember_key": True}, {"remember_key": False}, {"api_key": "replacement"}]:
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                registry.configure(changes)
        self.assertIsNone(vault.value)

    def test_platform_without_vault_keeps_session_option_but_rejects_persistence(self):
        registry = ProviderRegistry(environ={})
        registry.configure({"api_key": KEY})
        with self.assertRaises(ProviderError):
            registry.configure({"remember_key": True})
        self.assertEqual(registry._key(), (KEY, "session"))

    @unittest.skipUnless(os.name == "nt" and os.environ.get("REVIEWDESK_TEST_WINDOWS_VAULT") == "1",
                         "Windows vault integration is an explicit synthetic test")
    def test_windows_vault_round_trip_uses_only_a_unique_temporary_target(self):
        with tempfile.TemporaryDirectory() as folder:
            vault = WindowsCredentialStore(Path(folder))
            self.assertIsNone(vault.load())
            try:
                registry = ProviderRegistry(environ={}, credential_store=vault)
                registry.configure({"api_key": KEY, "remember_key": True, "require_zdr": False})
                restored = ProviderRegistry(environ={}, credential_store=WindowsCredentialStore(Path(folder)))
                self.assertEqual(restored._key(), (KEY, "saved"))
                self.assertFalse(restored.public()["require_zdr"])
            finally:
                vault.delete()
            self.assertIsNone(vault.load())
