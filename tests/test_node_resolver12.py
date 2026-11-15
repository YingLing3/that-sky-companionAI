"""Offline resolver and real bundled-child tests; no user configuration reads."""
from __future__ import annotations
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
import xg_paths


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="node-resolver-", dir=ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name, value in (("INSTALL_ROOT", self.folder / "install"),
                            ("ROOT", self.folder / "server"),
                            ("RELEASE", self.folder / "release")):
            patch = mock.patch.object(xg_paths, name, value)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ, {"XG_NODE": "", "PATH": ""})
        patch.start(); self.addCleanup(patch.stop)
        self.which = mock.patch.object(xg_paths.shutil, "which", return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        self.probe = mock.patch.object(xg_paths, "_probe_node_version", return_value=(22, "v22.0.0")).start()

    def node(self, rel):
        path = self.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic executable placeholder")
        return path

    def bundle(self):
        return self.node("install/runtime/node/node.exe")

    def assert_code(self, code, call):
        with self.assertRaises(xg_paths.NodeResolutionError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(str(ctx.exception), ctx.exception.message)

    def test_bundle_precedes_path(self):
        bundled = self.bundle(); old = self.node("old/node.exe")
        self.which.return_value = str(old)
        self.assertEqual(xg_paths.require_node_exe(), str(bundled.resolve()))
        self.which.assert_not_called()

    def test_explicit_environment_precedes_bundle(self):
        self.bundle(); custom = self.node("custom/node.exe")
        with mock.patch.dict(os.environ, {"XG_NODE": str(custom)}):
            self.assertEqual(xg_paths.require_node_exe(), str(custom.resolve()))

    def test_concrete_argument_precedes_environment(self):
        custom = self.node("custom/node.exe")
        with mock.patch.dict(os.environ, {"XG_NODE": "missing"}):
            self.assertEqual(xg_paths.require_node_exe(custom), str(custom.resolve()))

    def test_invalid_explicit_environment_never_falls_back(self):
        self.bundle()
        with mock.patch.dict(os.environ, {"XG_NODE": str(self.folder / "missing.exe")}):
            self.assert_code("node_invalid_override", xg_paths.require_node_exe)
            self.assertEqual(xg_paths.node_exe(), "")

    def test_missing_launcher_managed_override_reports_incomplete_package(self):
        managed = xg_paths.INSTALL_ROOT / "runtime/node/node.exe"
        with mock.patch.dict(os.environ, {"XG_NODE": str(managed)}):
            with self.assertRaises(xg_paths.NodeResolutionError) as ctx:
                xg_paths.require_node_exe()
            self.assertEqual(ctx.exception.code, "node_missing")
            self.assertIn("安装包不完整", ctx.exception.message)
            self.assertIn("重新解压", ctx.exception.message)
            self.assertEqual(xg_paths.node_exe(), "")

    def test_missing_managed_argument_does_not_become_custom_override_error(self):
        managed = xg_paths.INSTALL_ROOT / "runtime/node/node.exe"
        self.assert_code("node_missing", lambda: xg_paths.require_node_exe(managed))

    def test_invalid_concrete_argument_never_falls_back(self):
        self.bundle()
        self.assert_code("node_invalid_override", lambda: xg_paths.require_node_exe(self.folder / "missing.exe"))

    def test_directory_is_not_an_executable_override(self):
        self.assert_code("node_invalid_override", lambda: xg_paths.require_node_exe(self.folder))

    def test_placeholders_use_central_order(self):
        path = self.bundle()
        for value in (None, "", "node", "node.exe", "NODE.EXE"):
            with self.subTest(value=value):
                self.assertEqual(xg_paths.require_node_exe(value), str(path.resolve()))

    def test_quoted_explicit_path(self):
        path = self.node("custom folder/node.exe")
        self.assertEqual(xg_paths.require_node_exe('"' + str(path) + '"'), str(path.resolve()))

    def test_relative_override_becomes_absolute(self):
        path = self.node("custom/node.exe")
        relative = os.path.relpath(path, Path.cwd())
        self.assertEqual(xg_paths.require_node_exe(relative), str(path.resolve()))

    def test_path_fallback_when_bundle_absent(self):
        path = self.node("path/node.exe")
        self.which.return_value = str(path)
        self.assertEqual(xg_paths.require_node_exe(), str(path.resolve()))
        self.which.assert_called_once_with("node", path="")

    def test_legacy_server_fallback_after_path(self):
        path = self.node("server/runtime/node/bin/node")
        self.assertEqual(xg_paths.require_node_exe(), str(path.resolve()))

    def test_path_precedes_legacy_server(self):
        self.node("server/runtime/node/node.exe")
        path = self.node("path/node.exe"); self.which.return_value = str(path)
        self.assertEqual(xg_paths.require_node_exe(), str(path.resolve()))

    def test_missing_has_stable_diagnostics_and_compatibility_empty(self):
        self.assert_code("node_missing", xg_paths.require_node_exe)
        self.assertEqual(xg_paths.node_exe(), "")

    def test_old_path_version_is_rejected(self):
        path = self.node("path/node.exe"); self.which.return_value = str(path)
        self.probe.return_value = (18, "v18.20.0")
        self.assert_code("node_version_unsupported", xg_paths.require_node_exe)

    def test_unusable_bundle_does_not_silently_select_other_node(self):
        self.bundle()
        self.probe.side_effect = xg_paths.NodeResolutionError("node_unusable", "safe diagnostic")
        self.assert_code("node_unusable", xg_paths.require_node_exe)
        self.which.assert_not_called()

    def test_child_env_uses_its_mapping_not_ambient_override(self):
        path = self.node("provided/node.exe")
        with mock.patch.dict(os.environ, {"XG_NODE": "ambient-missing", "PATH": "ambient-path"}):
            result = xg_paths.node_child_env({"PATH": "child-path", "XG_NODE": str(path), "OTHER": "preserved"})
        self.assertEqual(result["XG_NODE"], str(path.resolve()))
        self.assertEqual(result["OTHER"], "preserved")

    def test_child_env_path_lookup_uses_mapping(self):
        path = self.node("provided/node.exe"); self.which.return_value = str(path)
        result = xg_paths.node_child_env({"PATH": "exact-child-path"})
        self.which.assert_called_once_with("node", path="exact-child-path")
        self.assertEqual(result["XG_NODE"], str(path.resolve()))

    def test_child_env_missing_preserves_input_without_guess(self):
        original = {"PATH": "", "OTHER": "same"}
        self.assertEqual(xg_paths.node_child_env(original), original)
        self.assertNotIn("XG_NODE", original)

    def test_child_env_invalid_override_stays_visible(self):
        self.bundle()
        original = {"PATH": "", "XG_NODE": str(self.folder / "missing.exe")}
        self.assertEqual(xg_paths.node_child_env(original), original)

    def test_physical_bundle_ignores_data_and_server_root(self):
        path = self.bundle()
        with mock.patch.dict(os.environ, {"SKY_AI_ROOT": str(self.folder / "other"),
                                         "XG_DATA_DIR": str(self.folder / "data")}):
            self.assertEqual(xg_paths.require_node_exe(), str(path.resolve()))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="node-probe-", dir=ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "node.exe"
        self.path.write_bytes(b"synthetic executable")
        xg_paths._NODE_VERSION_CACHE.clear()
        self.addCleanup(xg_paths._NODE_VERSION_CACHE.clear)

    def test_probe_is_cached_by_file_identity(self):
        with mock.patch.object(xg_paths.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="v22.1.2\n")) as run:
            self.assertEqual(xg_paths._probe_node_version(self.path), (22, "v22.1.2"))
            self.assertEqual(xg_paths._probe_node_version(self.path), (22, "v22.1.2"))
            run.assert_called_once()
            self.path.write_bytes(b"changed size synthetic executable")
            xg_paths._probe_node_version(self.path)
            self.assertEqual(run.call_count, 2)

    def test_nonzero_and_malformed_output_are_rejected(self):
        for result in (types.SimpleNamespace(returncode=1, stdout="v22.0.0"),
                       types.SimpleNamespace(returncode=0, stdout="this is not Node")):
            xg_paths._NODE_VERSION_CACHE.clear()
            with mock.patch.object(xg_paths.subprocess, "run", return_value=result):
                with self.assertRaises(xg_paths.NodeResolutionError) as ctx:
                    xg_paths._probe_node_version(self.path)
                self.assertEqual(ctx.exception.code, "node_unusable")

    def test_timeout_and_oserror_are_safe_and_bounded(self):
        for error in (OSError("private-path-marker"), subprocess.TimeoutExpired("synthetic", 3)):
            xg_paths._NODE_VERSION_CACHE.clear()
            with mock.patch.object(xg_paths.subprocess, "run", side_effect=error) as run:
                with self.assertRaises(xg_paths.NodeResolutionError) as ctx:
                    xg_paths._probe_node_version(self.path)
                self.assertEqual(ctx.exception.code, "node_unusable")
                self.assertNotIn("private-path-marker", ctx.exception.message)
                self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_probe_failure_is_briefly_cached(self):
        with mock.patch.object(xg_paths.subprocess, "run", side_effect=OSError("fixture")) as run:
            for _ in range(2):
                with self.assertRaises(xg_paths.NodeResolutionError):
                    xg_paths._probe_node_version(self.path)
            self.assertEqual(run.call_count, 1)


class ActualBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="node-child-", dir=ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC") if key in os.environ}
        self.env.update(PATH="", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                        PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                        SKY_AI_ROOT=str(APP), XG_DATA_DIR=str(self.folder / "isolated-data"),
                        HOME=str(self.folder), USERPROFILE=str(self.folder), LOCALAPPDATA=str(self.folder))
        self.bundle = ROOT / "runtime/node/node.exe"
        self.assertTrue(self.bundle.is_file())

    def child(self, code, *args):
        proc = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, *map(str, args)],
                              cwd=self.folder, env=self.env, text=True, encoding="utf-8",
                              errors="replace", capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]

    def test_real_empty_path_nonapp_cwd_resolves_physical_bundle(self):
        code = """import sys,json,subprocess;sys.path.insert(0,sys.argv[1]);import xg_paths
p=xg_paths.require_node_exe();r=subprocess.run([p,'--version'],capture_output=True,text=True,check=True)
print(json.dumps({'node':p,'version':r.stdout.strip(),'root':str(xg_paths.ROOT),'install':str(xg_paths.INSTALL_ROOT)}))"""
        doc = self.child(code, APP)[-1]
        self.assertEqual(Path(doc["node"]), self.bundle.resolve())
        self.assertGreaterEqual(int(doc["version"].lstrip("v").split(".")[0]), 20)
        self.assertEqual(Path(doc["root"]), APP)
        self.assertEqual(Path(doc["install"]), ROOT)

    @unittest.skipUnless(os.name == "nt", "Windows detached-process behavior")
    def test_real_detached_python_cold_probe_empty_path(self):
        result = self.folder / "detached-result.json"
        log_path = self.folder / "detached.log"
        code = """import sys,json,subprocess;sys.path.insert(0,sys.argv[1]);import xg_paths
from pathlib import Path
p=xg_paths.require_node_exe()
r=xg_paths._node_exec().run([p,'--version'],capture_output=True,text=True,check=True)
Path(sys.argv[2]).write_text(json.dumps({'node':p,'version':r.stdout.strip()}),encoding='utf-8')"""
        with log_path.open("wb") as log:
            child = subprocess.Popen(
                [sys.executable, "-B", "-X", "utf8", "-c", code, str(APP), str(result)],
                cwd=self.folder, env=self.env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                exit_code = child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
                raise
        self.assertEqual(exit_code, 0, log_path.read_text(encoding="utf-8", errors="replace"))
        doc = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(Path(doc["node"]), self.bundle.resolve())
        self.assertGreaterEqual(int(doc["version"].lstrip("v").split(".")[0]), 20)

    def test_real_shared_env_pins_node_for_child_from_nonapp_cwd(self):
        code = """import sys,json,subprocess;sys.path.insert(0,sys.argv[1]);import xg_paths
xg_paths.config=lambda:{};from capture import gamedata
gamedata._extra_paths=lambda:[]
e=gamedata.env()
r=subprocess.run([sys.executable,'-B','-c',"import os,json,subprocess; p=os.environ['XG_NODE'];print(json.dumps({'node':p,'version':subprocess.check_output([p,'--version'],text=True).strip()}))"],env=e,capture_output=True,text=True,check=True)
print(r.stdout.strip())"""
        doc = self.child(code, APP)[-1]
        self.assertEqual(Path(doc["node"]), self.bundle.resolve())
        self.assertGreaterEqual(int(doc["version"].lstrip("v").split(".")[0]), 20)

    def test_real_chain_child_receives_absolute_node(self):
        fixture = self.folder / "fixture"
        package = fixture / "xg_gamedata"; package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(
            "import json,os,subprocess\n"
            "def main(args):\n"
            " p=os.environ['XG_NODE']\n"
            " print(json.dumps({'ok':True,'node':p,'version':subprocess.check_output([p,'--version'],text=True).strip(),'args':args}))\n"
            " return 0\n", encoding="utf-8")
        code = """import sys,os;sys.path[:0]=[sys.argv[1],sys.argv[1]+'/launcher'];import xg_paths
xg_paths.config=lambda:{};from capture import gamedata
gamedata._extra_paths=lambda:[sys.argv[2]]
os.environ['XG_GAMEDATA_PYTHONPATH']=sys.argv[2]
os.environ['XG_PYTHON']=sys.executable
import gamedata_chain
raise SystemExit(gamedata_chain.run_plan([['node-fixture']], 'data'))"""
        doc = self.child(code, APP, fixture)[-1]
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["args"], ["node-fixture"])
        self.assertEqual(Path(doc["node"]), self.bundle.resolve())

    def test_shipped_launcher_pins_physical_bundle_absolute_path(self):
        # The delivered EXE embeds this baseline desktop entry. It intentionally
        # sets its own bundle path; only app/CLI entrypoints honor external XG_NODE.
        tree = ast.parse((ROOT / "desktop.py").read_text(encoding="utf-8"))
        self.assertFalse(any(isinstance(n, ast.FunctionDef) and n.name == "_node_child_environment"
                             for n in tree.body))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Backend")
        start = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "start")
        node_values = [kw.value for call in ast.walk(start) if isinstance(call, ast.Call)
                       and isinstance(call.func, ast.Attribute) and call.func.attr == "update"
                       for kw in call.keywords if kw.arg == "XG_NODE"]
        self.assertEqual(len(node_values), 1)
        value = eval(compile(ast.Expression(body=node_values[0]), "<launcher-node-env>", "eval"),
                     {"str": str, "self": types.SimpleNamespace(root=ROOT)})
        self.assertEqual(Path(value), self.bundle.resolve())

    def test_tasks_and_chain_both_use_shared_environment(self):
        for filename, name in (("tasks.py", "start"), ("gamedata_chain.py", "run_plan")):
            source = (APP / "launcher" / filename).read_text(encoding="utf-8")
            fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and isinstance(n.func.value, ast.Name) and n.func.value.id == "gamedata" and n.func.attr == "env"]
            self.assertEqual(len(calls), 1, filename)

    def test_import_does_not_probe_node(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("import-time subprocess")):
            spec = importlib.util.spec_from_file_location("_node_import_fixture", APP / "xg_paths.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertEqual(module.INSTALL_ROOT, ROOT)


if __name__ == "__main__":
    unittest.main()
