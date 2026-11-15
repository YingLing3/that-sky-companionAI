"""terrainfix.15: capture registration with duplicate local records of the same game account."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from xg_gamedata import captures as C  # noqa: E402
from xg_gamedata.common import GameDataError  # noqa: E402

UUID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
RECORD = {"account_binding": {"value": C._account_binding(UUID)}}


def row(i, uuid=UUID):
    return {"id": i, "user_uuid": uuid, "alias": "x"}


class MatchTests(unittest.TestCase):
    def test_single_match(self):
        self.assertEqual(C._match_account(RECORD, [row("a"), row("b", OTHER)])["id"], "a")

    def test_duplicate_prefers_active(self):
        self.assertEqual(C._match_account(RECORD, [row("old"), row("new")], "new")["id"], "new")

    def test_duplicate_without_active_is_still_refused(self):
        with self.assertRaises(GameDataError) as cm:
            C._match_account(RECORD, [row("old"), row("new")], "someone-else")
        self.assertEqual(cm.exception.code if hasattr(cm.exception, "code") else str(cm.exception),
                         getattr(cm.exception, "code", str(cm.exception)))
        self.assertIn("当前选中", str(cm.exception))

    def test_unknown_account(self):
        with self.assertRaises(GameDataError):
            C._match_account(RECORD, [row("b", OTHER)], "b")


class ReadAccountsTests(unittest.TestCase):
    def test_tombstoned_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "accounts.json"
            p.write_text(json.dumps({"accounts": [row("old"), row("new")], "deleted_account_ids": ["old"]}),
                         encoding="utf-8")
            rows = C._read_accounts(p)
            self.assertEqual([r["id"] for r in rows], ["new"])
            self.assertEqual(C._match_account(RECORD, rows)["id"], "new")

    def test_missing_tombstone_list_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "accounts.json"
            p.write_text(json.dumps({"accounts": [row("a")]}), encoding="utf-8")
            self.assertEqual(len(C._read_accounts(p)), 1)


if __name__ == "__main__":
    unittest.main()
