"""terrainfix.14: base stage rebuilds automatically when resume data came from an older build."""
from __future__ import annotations
import ast
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SOURCE = APP / "xg_gamedata/terrain/terrain_build.py"


def load(run_stream, logs):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"_abs", "remove_tree", "_base_resume_stale", "base_stage", "read_json"}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    consts = [n for n in tree.body if isinstance(n, ast.Assign)
              and any(getattr(t, "id", None) == "BASE_STALE_RESUME_CODES" for t in n.targets)]
    assert len(nodes) == len(names) and len(consts) == 1

    class Paths:
        @staticmethod
        def require_node_exe(node):
            return node

    def read_json(path):
        return json.loads(Path(path).read_text(encoding="utf-8")), None

    ns = {"os": os, "Path": Path, "sys": sys, "json": json, "shutil": shutil, "log": logs.append,
          "_long_path": lambda p, force=False: os.fspath(p), "xg_paths": Paths,
          "TOOLS": Path("tools"), "SRC": Path("src"), "run_stream": run_stream, "save": lambda s: None}
    exec(compile(ast.Module(body=consts + nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
    ns["read_json"] = read_json
    return ns


class Progress:
    total = 1

    def update(self, *a, **k):
        pass


class StaleResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "assets.work"
        self.out = self.work / "all-scenes-v1"
        self.out.mkdir(parents=True)
        (self.out / "catalog.json").write_text(json.dumps({"implementation_sha256": "old"}), encoding="utf-8")
        (self.out / "stale.bin").write_bytes(b"x")
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_run(self, first_error):
        def run_stream(cmd, log_path, on_line=None, env=None):
            self.calls.append([str(c) for c in cmd])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as h:
                if len(self.calls) == 1 and first_error:
                    h.write("ValueError: %s\n" % first_error)
                    return 1, 0.5
                h.write("ok\n")
            catalog = {"entries": [{"scene_name": "Dawn", "read_status": "ready"}]}
            (self.out / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
            return 0, 1.0
        return run_stream

    def run_base(self, first_error):
        logs = []
        ns = load(self.fake_run(first_error), logs)
        summary = {"stages": {}, "maps": {}}
        ns["base_stage"](Path("x.apk"), self.work, ["Dawn"], ["Dawn"], "node", True, 60, summary, Progress())
        return summary, logs

    def test_stale_resume_source_rebuilds_without_resume(self):
        summary, logs = self.run_base("resume_source_changed_use_new_output")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("--resume", self.calls[0])
        self.assertNotIn("--resume", self.calls[1])
        self.assertFalse((self.out / "stale.bin").exists())
        self.assertTrue(summary["stages"]["base_rebuilt_stale_resume"])
        self.assertEqual(summary["stages"]["base"]["exit_code"], 0)
        self.assertTrue(any("自动清理" in line for line in logs))

    def test_other_failure_is_not_retried(self):
        with self.assertRaises(RuntimeError):
            self.run_base("some_other_error")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.out / "stale.bin").exists())

    def test_stale_code_from_earlier_run_in_log_is_ignored(self):
        log_path = self.work / "logs" / "base.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("ValueError: resume_source_changed_use_new_output\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.run_base("unrelated_failure")
        self.assertEqual(len(self.calls), 1)

    def test_success_needs_no_retry(self):
        summary, _ = self.run_base(None)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("base_rebuilt_stale_resume", summary["stages"])


if __name__ == "__main__":
    unittest.main()
