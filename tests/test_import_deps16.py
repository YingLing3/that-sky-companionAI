"""terrainfix.16: overlay import order, Node engine dependency precheck, import-failure codes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
import xg_paths  # noqa: E402
from xg_gamedata import catalog_state as CS  # noqa: E402


def make_engine(tmp, installed):
    eng = Path(tmp) / "engine"
    eng.mkdir()
    (eng / "package.json").write_text(json.dumps({"dependencies": {
        "@dimforge/rapier3d-compat": "0.21.0", "meshoptimizer": "1.3.0", "recast-navigation": "0.44.0"}}),
        encoding="utf-8")
    for name, files in installed.items():
        d = eng / "node_modules" / name
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_text("x", encoding="utf-8")
    return eng


class EngineDepsTests(unittest.TestCase):
    def test_all_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make_engine(tmp, {"@dimforge/rapier3d-compat": ["package.json"],
                                    "meshoptimizer": ["package.json", "meshopt_decoder.mjs"],
                                    "recast-navigation": ["package.json"]})
            self.assertEqual(xg_paths.engine_deps_missing(eng), [])
            xg_paths.require_engine_deps(eng, "测试")

    def test_missing_and_half_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = make_engine(tmp, {"meshoptimizer": ["package.json"]})
            self.assertEqual(xg_paths.engine_deps_missing(eng),
                             ["@dimforge/rapier3d-compat", "meshoptimizer", "recast-navigation"])
            with self.assertRaises(xg_paths.NodeResolutionError) as cm:
                xg_paths.require_engine_deps(eng, "走路引擎")
            self.assertEqual(cm.exception.code, "engine_deps_missing")
            self.assertIn("npm ci --prefix", cm.exception.message)
            self.assertIn("走路引擎", cm.exception.message)

    def test_no_manifest_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(xg_paths.engine_deps_missing(Path(tmp)), [])

    def test_shipped_package_is_complete(self):
        for eng in (APP / "engine", APP / "xg_gamedata/terrain/pipeline/engine"):
            if (eng / "node_modules").is_dir():
                self.assertEqual(xg_paths.engine_deps_missing(eng), [], eng)


class ImportFailureTests(unittest.TestCase):
    def test_import_error_is_not_reported_as_rebuild(self):
        row = CS._failure({"account": "a"}, ImportError("cannot import name 'x'", name="qfsky.map_long0167"))
        self.assertEqual(row["code"], "catalog_import_failed")
        self.assertNotEqual(row["message"], CS.REBUILD_MESSAGE)

    def test_child_code_maps_to_import_message(self):
        row = CS._failure({"account": "a"}, ValueError("catalog_import_failed"))
        self.assertEqual(row["code"], "catalog_import_failed")
        self.assertNotEqual(row["message"], CS.REBUILD_MESSAGE)


class OverlayPathTests(unittest.TestCase):
    def test_live_package_gets_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "overlay"
            (overlay / "qfsky/map_long0167").mkdir(parents=True)
            xg = types.SimpleNamespace(OVERLAY=overlay, QFSKY_CODE=Path(tmp) / "app/qfsky",
                                       BOT_CODE=Path(tmp) / "app/bot", SRC=Path(tmp) / "nosrc")
            fake = types.ModuleType("qfsky")
            fake.__path__ = [str(Path(tmp) / "app/qfsky")]
            saved_mod, saved_path = sys.modules.get("qfsky"), list(sys.path)
            sys.modules["qfsky"] = fake
            try:
                CS._ensure_qfsky_paths(xg)
                self.assertIn(str(overlay / "qfsky"), fake.__path__)
                self.assertIn(str(overlay), sys.path)
            finally:
                sys.path[:] = saved_path
                if saved_mod is None:
                    sys.modules.pop("qfsky", None)
                else:
                    sys.modules["qfsky"] = saved_mod

    def test_qfsky_imported_first_still_finds_overlay_module(self):
        code = ("import sys; sys.path[:0]=[%r]; import qfsky.map_long0167; "
                "from xg_gamedata import catalog_state as cs; cs._ensure_qfsky_paths(); "
                "from qfsky.map_long0167 import asset_catalog_v32c, catalog_account_pins; print('ok')") % str(APP)
        env = dict(os.environ, PYTHONPATH="", XG_DATA_DIR=tempfile.mkdtemp(), SKY_AI_ROOT=str(APP))
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env, cwd=tempfile.gettempdir(), timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertIn("ok", out.stdout)

    def test_control_api_paths_include_overlay_and_src(self):
        text = (APP / "bot/control_api.py").read_text(encoding="utf-8")
        head = text.split("import map_catalog", 1)[0]
        self.assertIn('"escort-v31-server"', head)
        self.assertIn('"src"', head)


class ResumeRetryTests(unittest.TestCase):
    def test_base_resume_retries_failed(self):
        text = (APP / "xg_gamedata/terrain/terrain_build.py").read_text(encoding="utf-8")
        self.assertIn('cmd += ["--resume", "--retry-failed"]', text)
        self.assertIn('require_engine_deps(PIPELINE / "engine"', text)


if __name__ == "__main__":
    unittest.main()
