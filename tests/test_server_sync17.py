"""terrainfix.17: 2026-11-18 server fixes synced into the client (hold flags, chibi masks, air-follow clock gap)."""
import ast
from pathlib import Path
import unittest

BOT = Path(__file__).resolve().parents[1] / "app" / "bot"


def _src(name):
    return (BOT / name).read_text(encoding="utf-8")


class ServerSync17Tests(unittest.TestCase):
    def test_hold_follower_flags_match_real_follower(self):
        src = _src("room_pose.py")
        tree = ast.parse(src)
        consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name) and t.id in ("HOLD_MOVE_FORCE", "HOLD_STILL_FORCE")}
        self.assertEqual(consts["HOLD_MOVE_FORCE"]["0x21dd40c"], 0)      # 真机被牵方不发“自己在走”
        self.assertEqual(consts["HOLD_MOVE_FORCE"]["0x21ddcc8"], 31)
        self.assertEqual(consts["HOLD_STILL_FORCE"]["0x21ddcc8"], 0)
        for force in consts.values():
            self.assertEqual((force["0x1715ed0"], force["0x1715f48"], force["0x1715f70"]), (0, 0, 0))
        self.assertIn("CLAUDEWORK_HOLD_FOLLOWER_FLAGS", src)

    def test_both_chibi_masks(self):
        import social_pose
        self.assertEqual(social_pose.CHIBI_MASK_IDS, frozenset((2954671753, 1105594441)))
        self.assertAlmostEqual(social_pose.CHIBI_SCALE, 0.36)
        src = _src("room_pose.py")
        self.assertIn("social_pose.CHIBI_MASK_IDS", src)
        self.assertNotIn("== social_pose.CHIBI_MASK_ID", src)

    def test_air_follow_clock_gap_not_latched(self):
        src = _src("air_follow_motion.py")
        self.assertNotIn("return self._hold(now,'air_follow_clock_gap')", src)


if __name__ == "__main__":
    unittest.main()
