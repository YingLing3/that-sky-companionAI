"""Synthetic catalogue/UI status tests; no live accounts, captures or APK reads."""
from __future__ import annotations
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from test_catalog_async14 import settled, wait_idle

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path[:0] = [str(APP / "escort-v31-server"), str(APP)]
from qfsky.version_profile import CURRENT_BUILD, profile_for_build
from xg_gamedata import catalog, catalog_state
from xg_gamedata.common import GameDataError
import qfsky
import qfsky.map_long0167
for package, path in ((qfsky, APP / "escort-v31-server/qfsky"),
                      (qfsky.map_long0167, APP / "escort-v31-server/qfsky/map_long0167")):
    if str(path) not in package.__path__:
        package.__path__.append(str(path))
from qfsky.map_long0167 import asset_catalog_v32c as C

PROFILE = profile_for_build(CURRENT_BUILD)
ACCOUNT = "synthetic-spawn-status"
UUID = "11111111-1111-4111-8111-111111111111"
CODE = "catalog_qianniaocheng_stale"
MESSAGE = "千鸟城落点已更新，需要重新构建地图目录"

def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class StatusTests(unittest.TestCase):
    def setUp(self):
        wait_idle()
        self.temp = tempfile.TemporaryDirectory(prefix="spawn-status13-", dir=ROOT / "tests")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.xg = catalog_state._paths(self.data)
        self.path = self.xg.ASSET_RELEASE / "asset-catalog-acct" / ACCOUNT / "manifest.json"
        write_json(self.path, {"apk_build": PROFILE["build"], "native_sha256": PROFILE["native_sha256"]})
        catalog_state._CACHE.clear()
        self.addCleanup(catalog_state._CACHE.clear)

    def certify(self):
        return settled(self.data, ACCOUNT, UUID)

    def test_new_verified_directory_is_ready(self):
        with mock.patch.object(catalog_state, "_runtime_check", return_value={"maps": 115, "variants": 1}) as check:
            row = self.certify()
        self.assertTrue(row["certified"])
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["maps"], 115)
        self.assertEqual(row["code"], "")
        check.assert_called_once()
        self.assertNotIn("千鸟城", row["message"])

    def test_exact_runtime_stale_code_has_specific_rebuild_message(self):
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=ValueError(CODE)):
            row = self.certify()
        self.assertFalse(row["certified"])
        self.assertEqual(row["state"], "stale")
        self.assertEqual(row["code"], CODE)
        self.assertEqual(row["message"], MESSAGE)

    def test_business_exception_code_is_preserved(self):
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=GameDataError("safe fixture text", CODE)):
            row = self.certify()
        self.assertEqual(row["code"], CODE)
        self.assertEqual(row["message"], MESSAGE)

    def test_other_runtime_failures_are_not_relabelled(self):
        for code in ("capture_version_stale", "catalog_account_mismatch", "catalog_self_check",
                     "catalog_source_capture_changed", "catalog_incomplete"):
            # Each failure case represents changed input; unchanged failures are now cached.
            write_json(self.path, {"apk_build": PROFILE["build"], "native_sha256": PROFILE["native_sha256"], "fixture_case": code})
            with self.subTest(code=code), mock.patch.object(catalog_state, "_runtime_check", side_effect=ValueError(code)):
                row = self.certify()
            self.assertFalse(row["certified"])
            self.assertEqual(row["code"], code)
            self.assertEqual(row["message"], catalog_state.REBUILD_MESSAGE)
            self.assertNotEqual(row["message"], MESSAGE)

    def test_missing_account_precedes_runtime_stale_check(self):
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=AssertionError("must not run")):
            row = catalog_state.certify(self.data, "", "", PROFILE)
        self.assertEqual(row["code"], "account_required")
        self.assertEqual(row["message"], "先选择当前游戏账号")

    def test_missing_manifest_is_not_reported_as_stale_placement(self):
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=AssertionError("must not run")):
            row = catalog_state.certify(self.data, "another-fixture", UUID, PROFILE)
        self.assertEqual(row["code"], "catalog_missing")

    def test_old_build_precedes_placement_validation(self):
        write_json(self.path, {"apk_build": 421589, "native_sha256": "0" * 64})
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=AssertionError("must not run")):
            row = self.certify()
        self.assertEqual(row["code"], "catalog_version_stale")
        self.assertEqual(row["message"], catalog_state.REBUILD_MESSAGE)

    def test_error_message_substring_does_not_trigger_specific_notice(self):
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=ValueError("other:" + CODE)):
            row = self.certify()
        self.assertNotEqual(row["message"], MESSAGE)

    def test_malformed_error_metadata_remains_safe(self):
        exc = ValueError("private-fixture-value")
        exc.code = {"not": "a string"}
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=exc):
            row = self.certify()
        self.assertEqual(row["code"], "catalog_runtime_invalid")
        self.assertNotIn("private-fixture-value", row["message"])

    def test_runtime_child_json_preserves_specific_code(self):
        result = types.SimpleNamespace(returncode=0, stdout=json.dumps({"ok": False, "code": CODE}))
        with mock.patch.object(catalog_state.xg_exec, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "^" + CODE + "$"):
                catalog_state._runtime_check({"data": str(self.data)})

    def test_rebuild_requires_fresh_success_not_stale_failure_cache(self):
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=ValueError(CODE)):
            self.assertFalse(self.certify()["certified"])
        # A real rebuild changes its output fingerprint before a cached failure may retry.
        write_json(self.path, {"apk_build": PROFILE["build"], "native_sha256": PROFILE["native_sha256"], "fixture_rebuild": 1})
        with mock.patch.object(catalog_state, "_runtime_check", return_value={"maps": 115, "variants": 1}) as check:
            self.assertTrue(self.certify()["certified"])
        check.assert_called_once()

    def wizard(self, inventory):
        source = (APP / "launcher/wizard.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "catalog_status")
        namespace = {"active_account": lambda: ACCOUNT, "_current_game_profile": lambda: PROFILE,
                     "_reset_doc": lambda: {}, "_reset_steps": lambda reset, account: [],
                     "tasks": types.SimpleNamespace(current=lambda: {}),
                     "_capture_inventory": lambda profile: inventory,
                     "_accounts": lambda: [{"id": ACCOUNT, "user_uuid": UUID}],
                     "xg_paths": types.SimpleNamespace(DATA=self.data)}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<wizard-catalog-status>", "exec"), namespace)
        return namespace["catalog_status"]

    def test_launcher_keeps_old_capture_gate_before_directory_check(self):
        with mock.patch.object(catalog_state, "certify", side_effect=AssertionError("must not run")):
            row = self.wizard({"stale": [{"version": "0.16.7"}], "current": []})()
        self.assertEqual(row["code"], "capture_stale")
        self.assertNotEqual(row["message"], MESSAGE)

    def test_launcher_passes_through_exact_directory_notice(self):
        expected = {"certified": False, "state": "stale", "code": CODE, "message": MESSAGE}
        with mock.patch.object(catalog_state, "certify", return_value=expected) as check:
            row = self.wizard({"stale": [], "current": [{"scene": "CandleSpace"}]})()
        self.assertEqual(row, expected)
        check.assert_called_once_with(self.data, ACCOUNT, UUID, PROFILE)

    def runtime_fixture(self, stale=False, other_error=False):
        # Actual _entry_for and _verify_all are used. Synthetic profile bytes
        # replace the game payload; this tests propagation, not a live capture.
        mid = next(key for key, rows in C.EXPECTED_VARIANTS.items() if "qianniaocheng" in rows)
        raw = b"synthetic-catalog-runtime-status"
        digest = hashlib.sha256(raw).hexdigest()
        entry = copy.deepcopy(C.EXPECTED_ENTRIES[mid])
        entry.update(actor_sha256=digest, cache_sha256=digest, scene_sha256=digest)
        variant = copy.deepcopy(C.EXPECTED_VARIANTS[mid]["qianniaocheng"])
        variant["actor_sha256"] = digest
        if stale:
            variant["placement_sha256"] = "0" * 64
            variant["annotation_sha256"] = "0" * 64
        manifest = {"entries": {mid: entry}, "variants": {mid: {"qianniaocheng": variant}}}
        if other_error:
            manifest["entries"]["1"] = {"scene": "SyntheticOther"}
        seen = []
        def loader(uuid, map_id, variant=None):
            seen.append((map_id, variant))
            if map_id == 1:
                raise ValueError("asset_catalog_other_failure")
            row = C._entry_for(map_id, variant, entries=manifest["entries"], variants=manifest["variants"])
            profile = types.SimpleNamespace(binding="synthetic-binding", record={"actor_sha256": digest}, cache=raw, blob=raw)
            return None, profile, row
        def check(request):
            with mock.patch.dict(sys.modules, {"qfsky.asset_catalog_runtime_v32c": types.SimpleNamespace(load_catalog_trial=loader)}):
                return catalog._verify_all(self.xg, UUID, "synthetic-binding", manifest, self.path,
                                           PROFILE["resource_revision"], None)
        return mid, entry, manifest, seen, check

    def test_actual_entry_guard_and_aggregate_new_directory_ready(self):
        mid, entry, manifest, seen, check = self.runtime_fixture()
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=check):
            row = self.certify()
        self.assertTrue(row["certified"])
        self.assertEqual(row["maps"], 1)
        self.assertEqual(row["variants"], 1)
        self.assertEqual(seen, [(int(mid), None), (int(mid), "qianniaocheng")])

    def test_actual_entry_guard_and_aggregate_old_directory_specific_notice(self):
        mid, entry, manifest, seen, check = self.runtime_fixture(stale=True)
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=check):
            row = self.certify()
        self.assertEqual(row["code"], CODE)
        self.assertEqual(row["message"], MESSAGE)
        self.assertFalse(row["certified"])
        self.assertIn((int(mid), None), seen)
        self.assertIn((int(mid), "qianniaocheng"), seen)

    def test_old_variant_never_changes_main_spawn_entry(self):
        mid, entry, manifest, seen, check = self.runtime_fixture(stale=True)
        self.assertEqual(C._entry_for(int(mid), None, entries=manifest["entries"],
                                      variants=manifest["variants"]), entry)

    def test_actual_aggregate_other_failure_is_not_hidden_by_old_variant(self):
        mid, entry, manifest, seen, check = self.runtime_fixture(stale=True, other_error=True)
        with mock.patch.object(catalog_state, "_runtime_check", side_effect=check):
            row = self.certify()
        self.assertEqual(row["code"], "catalog_self_check")
        self.assertNotEqual(row["message"], MESSAGE)
        self.assertIn((1, None), seen)
        self.assertIn((int(mid), "qianniaocheng"), seen)


class ResourcePageTests(unittest.TestCase):
    def view(self, summary, task=None, error=""):
        node = ROOT / "runtime/node/node.exe"
        self.assertTrue(node.is_file())
        code = r"""
const fs=require('fs'),vm=require('vm');
const s=fs.readFileSync(process.argv[1],'utf8');
const begin=s.indexOf('// RESOURCE_WORKFLOW_MODEL_BEGIN'),end=s.indexOf('// RESOURCE_WORKFLOW_MODEL_END');
if(begin<0||end<=begin)throw new Error('model markers missing');
const ctx={};vm.createContext(ctx);
vm.runInContext(s.slice(begin,end)+';globalThis.view=resourceStatus.catalogView;',ctx);
const input=JSON.parse(process.argv[2]);
console.log(JSON.stringify(ctx.view(input.summary,input.task,input.error)));
"""
        result = subprocess.run([str(node), "-e", code, str(APP / "web/desktop.js"),
                                 json.dumps({"summary": summary, "task": task, "error": error})],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def summary(self, **changes):
        value = {"account": ACCOUNT, "catalog": {"account": ACCOUNT, "certified": False,
                 "code": CODE, "message": MESSAGE}, "capture": {"stale": [], "recorded": ["CandleSpace"]}}
        value.update(changes)
        return value

    def test_old_directory_notice_visible(self):
        row = self.view(self.summary())
        self.assertEqual(row["state"], "waiting")
        self.assertEqual(row["message"], MESSAGE)
        self.assertIn("主出生点保持原义", row["detail"])
        self.assertIsNone(row["progress"])

    def test_new_directory_green_requires_real_certificate(self):
        row = self.view(self.summary(catalog={"account": ACCOUNT, "certified": True, "code": "", "maps": 115, "variants": 1}))
        self.assertEqual(row["state"], "success")
        self.assertEqual(row["progress"]["value"], 100)

    def test_historical_task_success_cannot_hide_old_directory(self):
        row = self.view(self.summary(), {"task": {"kind": "catalog", "ok": True, "running": False},
                                        "meta": {"account": ACCOUNT}})
        self.assertEqual(row["message"], MESSAGE)
        self.assertNotEqual(row["state"], "success")

    def test_running_task_keeps_progress(self):
        row = self.view(self.summary(), {"task": {"kind": "catalog", "running": True, "done": 1, "total": 5}})
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["progress"]["value"], 20)

    def test_task_failure_is_not_overwritten(self):
        row = self.view(self.summary(), {"task": {"kind": "catalog", "running": False, "ok": False, "error": "synthetic capture failure"}})
        self.assertEqual(row["state"], "error")
        self.assertEqual(row["message"], "synthetic capture failure")

    def test_old_capture_notice_precedes_placement_notice(self):
        row = self.view(self.summary(capture={"stale": [{"version": "0.16.7"}], "recorded": [], "current_version": "0.16.8"}))
        self.assertIn("重新录", row["message"])
        self.assertNotEqual(row["message"], MESSAGE)

    def test_other_account_or_missing_account_never_shows_placement_notice(self):
        for account in ("", "other-account"):
            with self.subTest(account=account):
                row = self.view(self.summary(account=account))
                self.assertNotEqual(row["message"], MESSAGE)

    def test_transport_error_remains_visible(self):
        row = self.view(self.summary(), error="synthetic status request error")
        self.assertEqual(row["state"], "error")
        self.assertEqual(row["message"], "synthetic status request error")


if __name__ == "__main__":
    unittest.main()
