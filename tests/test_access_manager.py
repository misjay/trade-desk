"""
tests/test_access_manager.py — Unit tests for Access Code authorization engine.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import access_manager
import user_store


class TestAccessManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp()
        cls.orig_access_file = access_manager._ACCESS_FILE
        access_manager._ACCESS_FILE = Path(cls.temp_dir) / "test_access_codes.json"
        cls.orig_users_file = user_store._USERS_FILE
        user_store._USERS_FILE = Path(cls.temp_dir) / "test_users_state.json"

    @classmethod
    def tearDownClass(cls):
        access_manager._ACCESS_FILE = cls.orig_access_file
        user_store._USERS_FILE = cls.orig_users_file
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        if access_manager._ACCESS_FILE.exists():
            access_manager._ACCESS_FILE.unlink()
        if user_store._USERS_FILE.exists():
            user_store._USERS_FILE.unlink()

    def test_default_seed_code_and_admin_bypass(self):
        # Admin is automatically authorized without entering any code
        admin_id = "6724880386"
        self.assertTrue(access_manager.is_user_authorized(admin_id))

        # Stranger is unauthorized initially
        stranger_id = "11223344"
        self.assertFalse(access_manager.is_user_authorized(stranger_id))

        # Default code WINZ2026 exists
        codes = access_manager.list_access_codes()
        self.assertIn("WINZ2026", codes)
        self.assertTrue(codes["WINZ2026"]["is_active"])

    def test_redeem_valid_access_code(self):
        user_id = "user_alpha_1"
        self.assertFalse(access_manager.is_user_authorized(user_id))

        ok, msg = access_manager.redeem_access_code(user_id, "winz2026")
        self.assertTrue(ok)
        self.assertIn("activated successfully", msg)

        # Now authorized
        self.assertTrue(access_manager.is_user_authorized(user_id))
        self.assertTrue(user_store.is_user_has_access(user_id))

        # Re-redeeming informs user
        ok2, msg2 = access_manager.redeem_access_code(user_id, "winz2026")
        self.assertTrue(ok2)
        self.assertIn("already have active", msg2)

    def test_redeem_invalid_code(self):
        user_id = "user_beta_2"
        ok, msg = access_manager.redeem_access_code(user_id, "FAKE-CODE-999")
        self.assertFalse(ok)
        self.assertIn("Invalid access code", msg)
        self.assertFalse(access_manager.is_user_authorized(user_id))

    def test_create_code_with_max_uses(self):
        ok, msg, rec = access_manager.create_access_code(
            code="LIMITED-2",
            max_uses=2,
            label="Two User Invite",
            created_by="admin_test",
        )
        self.assertTrue(ok)
        self.assertEqual(rec["code"], "LIMITED-2")
        self.assertEqual(rec["max_uses"], 2)

        # First redemption
        ok1, _ = access_manager.redeem_access_code("user_1", "LIMITED-2")
        self.assertTrue(ok1)

        # Second redemption
        ok2, _ = access_manager.redeem_access_code("user_2", "LIMITED-2")
        self.assertTrue(ok2)

        # Third redemption should fail (max limit reached)
        ok3, msg3 = access_manager.redeem_access_code("user_3", "LIMITED-2")
        self.assertFalse(ok3)
        self.assertIn("maximum redemption limit", msg3)
        self.assertFalse(access_manager.is_user_authorized("user_3"))

    def test_revoke_access_code(self):
        ok, _, rec = access_manager.create_access_code(code="REVOKE-ME", max_uses=0)
        self.assertTrue(ok)

        # Revoke code
        rev_ok, rev_msg = access_manager.revoke_access_code("REVOKE-ME")
        self.assertTrue(rev_ok)

        # Attempt to redeem revoked code
        ok_red, msg_red = access_manager.redeem_access_code("user_victim", "REVOKE-ME")
        self.assertFalse(ok_red)
        self.assertIn("deactivated or revoked", msg_red)
        self.assertFalse(access_manager.is_user_authorized("user_victim"))

    def test_admin_direct_grant_and_revoke_user(self):
        target = "user_direct_99"
        self.assertFalse(access_manager.is_user_authorized(target))

        # Admin grants access directly
        self.assertTrue(access_manager.grant_user_access(target, label="VIP Partner"))
        self.assertTrue(access_manager.is_user_authorized(target))

        # Admin revokes user access
        self.assertTrue(access_manager.revoke_user_access(target))
        self.assertFalse(access_manager.is_user_authorized(target))

    def test_generate_random_access_code(self):
        ok, _, rec = access_manager.create_access_code(code=None, max_uses=1, label="Single Pass")
        self.assertTrue(ok)
        self.assertTrue(rec["code"].startswith("WINZ-"))
        self.assertEqual(rec["max_uses"], 1)
