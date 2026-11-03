import json
import tempfile
import unittest
from pathlib import Path

from smm.hosting import configure


class HostingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"SMM_DATA_DIR": str(self.root), "BOT_TOKEN": "123456:ThisIsOnlyALocalTestTokenNotReal", "OWNER_ID": "42"}

    def test_bootstrap_and_restart_keep_state(self):
        root, store = configure(self.env)
        store.put("coowners", [43])
        self.assertEqual(store.get("owner"), 42)
        self.assertTrue(store.get("paused"))
        self.assertEqual(len(store.channels()), 6)
        _, restarted = configure(self.env)
        self.assertEqual(restarted.get("coowners"), [43])
        self.assertEqual(root, self.root)

    def test_owner_cannot_be_replaced_through_env(self):
        configure(self.env)
        before = (self.root / "config.json").read_bytes()
        with self.assertRaises(ValueError):
            configure(dict(self.env, OWNER_ID="43", BOT_TOKEN="123456:AnotherLocalTestTokenNotReal"))
        self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_missing_owner_fails_closed(self):
        with self.assertRaises(ValueError):
            configure(dict(self.env, OWNER_ID=""))
        self.assertFalse((self.root / "config.json").exists())

    def test_invalid_token_never_printed_in_error(self):
        with self.assertRaises(ValueError) as error:
            configure(dict(self.env, BOT_TOKEN="invalid-secret"))
        self.assertNotIn("invalid-secret", str(error.exception))

    def test_relative_storage_is_rejected(self):
        with self.assertRaises(ValueError):
            configure(dict(self.env, SMM_DATA_DIR="relative"))

    def test_saved_owner_and_token_need_no_reentry(self):
        configure(self.env)
        _, store = configure({"SMM_DATA_DIR": str(self.root)})
        self.assertEqual(store.get("owner"), 42)

    def test_oversized_owner_rejected(self):
        with self.assertRaises(ValueError):
            configure(dict(self.env, OWNER_ID=str(2**63)))
