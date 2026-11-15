"""terrainfix.14: body height / animation rate / one-shot emote fixes (synthetic fixture only)."""
from __future__ import annotations
import json
from pathlib import Path
import struct
import sys
import unittest
import warnings

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "app/escort-v31-server", ROOT / "app", ROOT / "app/bot"):
    sys.path.insert(0, str(p))
warnings.simplefilter("ignore")

import anim_pose  # noqa: E402
import candle_pose  # noqa: E402
import social_pose  # noqa: E402
import air_follow  # noqa: E402

FIXTURE = (ROOT / "tests/fixtures/actor_0168_candle.bin").read_bytes()
REF = json.loads((ROOT / "app/bot/stand_pose.json").read_text(encoding="utf-8"))[0]["values"]


def body(feet_y, torso_y):
    values = {}
    for k, v in REF.items():
        label = k.split("|")[0]
        v = list(v)
        if label in social_pose.FEET:
            v[1] = feet_y
        elif label == social_pose.BODY:
            v[1] = torso_y
        values[k] = v
    return anim_pose.write_pose_fields(FIXTURE, values)


def field(b, label, occurrence=1):
    seen = 0
    for t, l in zip(candle_pose.tokens(b), candle_pose.labels(b)):
        if l == label:
            seen += 1
            if seen == occurrence:
                return bytes(t.value) if t.kind == "raw" else t.value
    return None


def set_rate(b, value):
    toks, labs = candle_pose.tokens(b), candle_pose.labels(b)
    occ = {}
    raw = struct.pack("<f", value)
    for i, l in enumerate(labs):
        occ[l] = occ.get(l, 0) + 1
        if l == "0x21ddc20" or (l == "0x1716a94" and occ[l] == 6):
            toks[i] = type(toks[i])("raw", 4, raw)
    return candle_pose.write(toks)


class StandingHeightTests(unittest.TestCase):
    def test_every_height_counts_as_standing(self):
        for feet, torso in ((114, 159), (98, 147), (85, 143), (40, 120), (200, 240)):
            self.assertTrue(social_pose._feet_down(body(feet, torso)), (feet, torso))

    def test_uneven_feet_or_torso_below_feet_is_not_standing(self):
        b = body(98, 147)
        values = {k: (list(v[:1]) + [60] + list(v[2:]) if k.startswith("0x171666c") else v) for k, v in REF.items()}
        self.assertFalse(social_pose._feet_down(anim_pose.write_pose_fields(b, values)))
        self.assertFalse(social_pose._feet_down(body(150, 140)))

    def test_origin_offset_matches_measured_heights(self):
        # 实测（遇境平地）：游戏最矮 118→0.160、矮 114→0.226、普通 98→0.496、高 85→0.707、游戏最高 62→1.101
        for feet, torso, expected in ((118, 136, 0.160), (114, 159, 0.226), (98, 147, 0.496),
                                      (85, 143, 0.707), (62, 131, 1.101)):
            self.assertAlmostEqual(social_pose.body_origin_offset(body(feet, torso)), expected, delta=0.007)

    def test_origin_offset_out_of_range_or_not_standing_is_none(self):
        self.assertIsNone(social_pose.body_origin_offset(body(250, 255)))
        self.assertIsNone(social_pose.body_origin_offset(b""))


class AnimRateTests(unittest.TestCase):
    def test_slow_recorded_rate_is_restored_to_one(self):
        slow = set_rate(body(85, 143), 0.25)
        self.assertEqual(field(slow, "0x21ddc20"), struct.pack("<f", 0.25))
        out = social_pose.stand(slow)
        self.assertEqual(field(out, "0x21ddc20"), struct.pack("<f", 1.0))
        self.assertEqual(field(out, "0x1716a94", 6), struct.pack("<f", 1.0))
        self.assertEqual(len(candle_pose.tokens(out)), len(candle_pose.tokens(slow)))

    def test_normal_rate_body_unchanged(self):
        b = set_rate(body(98, 147), 1.0)
        self.assertEqual(social_pose.normal_anim_rate(b), b)


class EmoteOnceTests(unittest.TestCase):
    def test_real_client_sequence(self):
        b = body(98, 147)
        ev = anim_pose.EmoteOnceEvent(anim_pose.anim_key("WaveEmote_1"), anim_pose.read(b), 100.0, 2.5)
        ev.post(b, 100.0)
        expect = {0.0: (7, 7, 1), 0.15: (3, 4, 1), 1.0: (0, 1, 1), 2.2: (1, 2, 1), 2.3: (4, 5, 1),
                  2.4: (7, 7, 1), 2.6: (7, 7, 0)}
        for t, (c90, f, hold) in expect.items():
            r = anim_pose.read(ev.post(b, 100.0 + t))
            self.assertEqual((r["mode"], r["code90"], r["code_f48"], r["code_f70"], r["emote_hold"]),
                             (0, c90, f, f, hold), t)
            self.assertEqual(r["key"], anim_pose.anim_key("WaveEmote_1"))
        self.assertEqual(ev.stamp()["key"], ev.key)


class OwnerHeightTests(unittest.TestCase):
    class Terrain:
        def __init__(self, offset):
            self.offset = (0.0, offset, 0.0)

        def hits(self, x, z):
            return [(1, 0.0 + self.offset[1], 1.0)]

    def test_tall_bot_does_not_filter_owner_ground(self):
        owner_feet_origin = (0.0, 0.25, 0.0)          # 矮个子主人站在地上
        self.assertIsNotNone(air_follow.support_below(self.Terrain(0.707), owner_feet_origin))

    def test_owner_height_uses_normal_body_regardless_of_bot_height(self):
        for bot in (0.226, 0.5, 0.707, 1.4):
            tr = air_follow.TargetTracker()
            obs = tr.observe(1.0, (0.0, 0.5, 0.0), self.Terrain(bot), binding="b", observed_at=1.0)
            self.assertAlmostEqual(obs["height_above"], 0.0, places=6, msg=str(bot))
            self.assertNotEqual(obs["mode"], "airborne")

    def test_tallest_owner_standing_is_not_airborne(self):
        # 游戏最高的主人站在地上：身体原点离地 1.101 m。带上他自己的身高就是 0 m 离地。
        for bot in (0.16, 0.5, 1.1):
            tr = air_follow.TargetTracker()
            obs = None
            for i in range(30):
                obs = tr.observe(1.0 + i * .1, (0.0, 1.101, 0.0), self.Terrain(bot), binding="b",
                                 observed_at=1.0 + i * .1, target_origin=1.101)
            self.assertAlmostEqual(obs["height_above"], 0.0, places=6, msg=str(bot))
            self.assertNotEqual(obs["mode"], "airborne")
        # 不带身高（旧行为）会把他当成离地 0.6 m —— 这正是要修的情况。
        tr = air_follow.TargetTracker()
        obs = tr.observe(1.0, (0.0, 1.101, 0.0), self.Terrain(0.5), binding="b", observed_at=1.0)
        self.assertAlmostEqual(obs["height_above"], 0.601, places=3)


if __name__ == "__main__":
    unittest.main()
