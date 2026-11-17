"""terrainfix.16: 八人门 rename + DayHubCave named spawns (彩虹日 / 奥利奥) synced from the server."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for p in (APP / "escort-v31-server", APP):
    sys.path.insert(0, str(p))
from qfsky.map_long0167 import asset_catalog_v32c as C  # noqa: E402
from xg_gamedata import catalog_state as CS  # noqa: E402
from xg_gamedata import name_data  # noqa: E402

MAP = "2394719185"
NAMES = {"rainbowday": "彩虹日", "oreo": "奥利奥"}


def sha(b):
    return hashlib.sha256(b).hexdigest()


class BairenmenTests(unittest.TestCase):
    def setUp(self):
        self.pack = json.loads((APP / "xg_gamedata/spawn_pack_0168.json").read_text(encoding="utf-8"))

    def test_pack_variants_match_policy(self):
        rows = self.pack["variants"]["DayHubCave"]
        self.assertEqual(set(rows), set(NAMES))
        table = json.loads(C._EXPECTED_VARIANTS_JSON)
        self.assertEqual(sha(C.canonical_json(table)), C._EXPECTED_VARIANTS_SHA)
        self.assertEqual(set(table[MAP]), set(NAMES))
        for name, label in NAMES.items():
            pl = rows[name]
            policy = table[MAP][name]
            self.assertNotIn("actor_sha256", policy)       # client actors are per-account
            self.assertEqual(policy["label"], label)
            self.assertEqual(policy["placement_sha256"], sha(C.canonical_json(pl)))
            ann = pl["provenance"]["annotation"]
            self.assertEqual(policy["annotation_sha256"], sha(C.canonical_json(ann)))
            self.assertEqual(ann["note"], label)
            self.assertEqual(ann["name_zh"], "八人门")
            self.assertGreaterEqual(pl["provenance"]["portal_clearance"]["minimum_body_aabb_separation"], 0.5)
        self.assertIn("qianniaocheng", table["507487826"])     # 千鸟城 unchanged

    def test_names_and_destinations(self):
        scenes = next(v for v in name_data.DATA.values() if isinstance(v, dict) and "DayHubCave" in v
                      and isinstance(v["DayHubCave"], dict) and "displayName" in v["DayHubCave"])
        self.assertEqual(scenes["DayHubCave"]["displayName"], "八人门")
        self.assertIn("中央神坛", scenes["DayHubCave"]["aliases"])           # 旧名仍能叫到
        landmarks = {r["id"]: r for r in name_data.DATA["DEST_LANDMARKS"]}
        dests = {d["id"]: d for d in json.loads((APP / "config/spawn_points.json").read_text(encoding="utf-8"))["destinations"]}
        self.assertIn("八人门", dests["scene:DayHubCave"]["aliases"])
        for name, label in NAMES.items():
            rid = "landmark:" + name
            self.assertEqual(landmarks[rid]["spawnVariant"], name)
            self.assertEqual(landmarks[rid]["displayName"], label)
            self.assertEqual((dests[rid]["variant"], dests[rid]["map_id"]), (name, int(MAP)))
            self.assertIn(label, dests[rid]["aliases"])

    def test_old_catalog_asks_rebuild_not_recapture(self):
        row = CS._failure({"account": "a"}, ValueError("catalog_variants_incomplete"))
        self.assertEqual(row["state"], "stale")
        self.assertNotEqual(row["message"], CS.REBUILD_MESSAGE)
        self.assertIn("重新构建地图目录", row["message"])


if __name__ == "__main__":
    unittest.main()
