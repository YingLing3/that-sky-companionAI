"""Real template resolver with synthetic local records, no account/config discovery."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
# Module initialization only installs import paths. Restore those paths so this
# regression never changes the following test modules' overlay precedence.
_saved_path = list(sys.path)
try:
    _spec = importlib.util.spec_from_file_location("catalog_template13_target", APP/"bot/account_catalog.py")
    C = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(C)
finally:
    sys.path[:] = _saved_path


def digest(body):
    return hashlib.sha256(body).hexdigest()


class TemplateRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="catalog-template13-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        self.sources = self.root / "private/catalog_sources.json"
        self.sources.parent.mkdir()
        for target, value in (("PROFILES", self.profiles), ("SOURCES", self.sources)):
            patcher = patch.object(C, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # These are mathematical fixture identities, not user account IDs.
        self.binding = digest(b"synthetic-template-owner")
        self.record = {"account_binding":{"value":self.binding},
                       "scene":"CandleSpace", "level_id":C.HOME,
                       "captured_utc":"2026-01-01T00:00:00Z"}
        self.actor = b"synthetic actor bytes\x00\x01\x02"
        self.manifest = self.make_record("template_old")
        self.doc = {"maps":{str(C.HOME):{"current_capture":{}}}}

    def make_record(self, name, record=None, actor=None):
        folder = self.profiles/name
        folder.mkdir(exist_ok=True)
        raw = json.dumps(self.record if record is None else record,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
        body = self.actor if actor is None else actor
        (folder/"record.json").write_bytes(raw)
        (folder/"actor.bin").write_bytes(body)
        return {"source_capture_record_sha256":digest(raw),
                "source_actor_sha256":digest(body),
                "source_account_binding_hash":self.binding}

    def write_sources(self, doc):
        self.sources.write_text(json.dumps(doc), encoding="utf-8")

    def current(self, name):
        self.doc["maps"][str(C.HOME)]["current_capture"] = {"record_dir":name}

    def resolve(self, manifest=None):
        return C._template_record_dir(self.manifest if manifest is None else manifest, self.doc)

    def rejected(self, manifest=None):
        with self.assertRaisesRegex(C.CatalogError, "^default_source_capture_not_found$"):
            self.resolve(manifest)

    def test_old_template_found_only_in_sources_after_new_capture(self):
        self.make_record("capture_new", actor=b"different newly recorded synthetic actor")
        self.current("capture_new")
        self.write_sources({"synthetic_owner":{"record_dir":"template_old"}})
        self.assertEqual(self.resolve(),"template_old")

    def test_record_fingerprint_mismatch_rejects(self):
        self.write_sources({"owner":{"record_dir":"template_old"}})
        (self.profiles/"template_old/record.json").write_bytes(b'{"account_binding":{"value":"changed"}}')
        self.rejected()

    def test_actor_fingerprint_mismatch_rejects(self):
        self.write_sources({"owner":{"record_dir":"template_old"}})
        (self.profiles/"template_old/actor.bin").write_bytes(b"changed synthetic actor")
        self.rejected()

    def test_binding_fingerprint_mismatch_rejects(self):
        self.write_sources({"owner":{"record_dir":"template_old"}})
        bad=dict(self.manifest,source_account_binding_hash=digest(b"different synthetic owner"))
        self.rejected(bad)

    def test_current_capture_wins_when_both_match(self):
        self.make_record("current_equal")
        self.current("current_equal")
        self.write_sources({"owner":{"record_dir":"template_old"}})
        self.assertEqual(self.resolve(),"current_equal")

    def test_current_by_account_order_stays_before_direct_current(self):
        self.make_record("by_account_equal")
        self.make_record("direct_equal")
        self.doc["maps"][str(C.HOME)]["current_capture"] = {
            "by_account":{"synthetic":{"record_dir":"by_account_equal"}},
            "record_dir":"direct_equal"}
        self.write_sources({"owner":{"record_dir":"template_old"}})
        self.assertEqual(self.resolve(),"by_account_equal")

    def test_repeated_candidates_are_checked_once_in_first_seen_order(self):
        self.make_record("other")
        self.doc["maps"][str(C.HOME)]["current_capture"] = {
            "by_account":{"a":{"record_dir":"template_old"},"b":{"record_dir":"template_old"}},
            "record_dir":"template_old"}
        self.write_sources({"a":{"record_dir":"template_old"},"b":{"record_dir":"other"},"c":{"record_dir":"other"}})
        with patch.object(C,"_candidate_record",wraps=C._candidate_record) as candidate:
            self.rejected(dict(self.manifest,source_actor_sha256="0"*64))
        self.assertEqual([call.args[1] for call in candidate.call_args_list],["template_old","other"])

    def test_missing_sources_current_still_works(self):
        self.current("template_old")
        self.assertFalse(self.sources.exists())
        self.assertEqual(self.resolve(),"template_old")

    def test_missing_sources_without_match_is_explicit_not_found(self):
        self.rejected()

    def test_bad_sources_json_current_still_works(self):
        self.current("template_old")
        self.sources.write_text('{broken',encoding="utf-8")
        self.assertEqual(self.resolve(),"template_old")

    def test_bad_sources_json_without_match_is_not_found(self):
        self.sources.write_text('{broken',encoding="utf-8")
        self.rejected()

    def test_invalid_sources_utf8_is_tolerated(self):
        self.sources.write_bytes(b"\xff\xfe\x80")
        self.current("template_old")
        self.assertEqual(self.resolve(),"template_old")

    def test_sources_non_dict_is_tolerated(self):
        for value in ([],[{}],None,True,17,"text"):
            with self.subTest(value=value):
                self.write_sources(value)
                self.current("template_old")
                self.assertEqual(self.resolve(),"template_old")
                self.current("absent")
                self.rejected()

    def test_sources_bad_rows_skip_to_valid_row(self):
        for value in ([],None,True,17,"text"):
            with self.subTest(value=value):
                self.write_sources({"bad":value,"good":{"record_dir":"template_old"}})
                self.assertEqual(self.resolve(),"template_old")

    def test_sources_non_string_record_dir_skip_to_valid_row(self):
        for value in ([],{},None,True,17):
            with self.subTest(value=value):
                self.write_sources({"bad":{"record_dir":value},"good":{"record_dir":"template_old"}})
                self.assertEqual(self.resolve(),"template_old")

    def test_sources_empty_or_missing_record_dir_skip(self):
        self.write_sources({"empty":{"record_dir":""},"absent":{},"good":{"record_dir":"template_old"}})
        with patch.object(C,"_candidate_record",wraps=C._candidate_record) as candidate:
            self.assertEqual(self.resolve(),"template_old")
        self.assertEqual([call.args[1] for call in candidate.call_args_list],["template_old"])

    def test_sources_uses_long_path_resolver_for_read(self):
        self.write_sources({"owner":{"record_dir":"template_old"}})
        with patch.object(C.xg_paths,"long_path",wraps=C.xg_paths.long_path) as long_path:
            self.assertEqual(self.resolve(),"template_old")
        self.assertTrue(any(Path(call.args[0])==self.sources for call in long_path.call_args_list))

    def test_sources_read_uses_resolved_path_not_original(self):
        redirected=self.root/"resolved_sources.json"
        redirected.write_text(json.dumps({"owner":{"record_dir":"template_old"}}),encoding="utf-8")
        real=C.xg_paths.long_path
        def resolver(path):
            return str(redirected) if Path(path)==self.sources else real(path)
        self.assertFalse(self.sources.exists())
        with patch.object(C.xg_paths,"long_path",side_effect=resolver):
            self.assertEqual(self.resolve(),"template_old")

    def test_real_sources_path_longer_than_windows_max_path(self):
        deep = self.root / "long_sources"
        for index in range(5):
            deep = deep / ("layer_%d_" % index + "x" * 55)
        source = deep / "catalog_sources.json"
        self.assertGreater(len(str(source.resolve())), 260)
        self.assertTrue(source.resolve().is_relative_to(self.root.resolve()))
        physical = Path(C.xg_paths.long_path(source))
        try:
            Path(C.xg_paths.long_path(deep)).mkdir(parents=True, exist_ok=True)
            physical.write_text(json.dumps({"synthetic":{"record_dir":"template_old"}}), encoding="utf-8")
            with patch.object(C, "SOURCES", source):
                self.assertEqual(self.resolve(), "template_old")
        finally:
            if physical.exists():
                physical.unlink()
            parent = deep
            while parent != self.root:
                self.assertTrue(parent.resolve().is_relative_to(self.root.resolve()))
                directory = Path(C.xg_paths.long_path(parent))
                if directory.exists():
                    directory.rmdir()
                parent = parent.parent

    def test_path_name_checks_reject_traversal_absolute_and_separator(self):
        bad_names=("../template_old","..\\template_old","/template_old",str((self.profiles/"template_old").resolve()),
                   "a/b","a\\b",".","..","template_old ","a:b")
        for name in bad_names:
            with self.subTest(name=name):
                self.write_sources({"owner":{"record_dir":name}})
                with patch.object(C,"_read_json",wraps=C._read_json) as reader:
                    self.rejected()
                self.assertEqual(reader.call_count,0)

    def test_invalid_path_candidate_does_not_hide_later_valid_source(self):
        self.write_sources({"bad":{"record_dir":"../forbidden"},"good":{"record_dir":"template_old"}})
        self.assertEqual(self.resolve(),"template_old")

    def test_missing_directory_candidate_does_not_hide_later_valid_source(self):
        self.write_sources({"bad":{"record_dir":"absent"},"good":{"record_dir":"template_old"}})
        self.assertEqual(self.resolve(),"template_old")

    def test_missing_actor_rejected(self):
        self.write_sources({"owner":{"record_dir":"template_old"}})
        (self.profiles/"template_old/actor.bin").unlink()
        self.rejected()

    def test_missing_record_rejected(self):
        self.write_sources({"owner":{"record_dir":"template_old"}})
        (self.profiles/"template_old/record.json").unlink()
        self.rejected()

    def test_record_symlink_gate_preserved_without_creating_symlink(self):
        self.write_sources({"owner":{"record_dir":"template_old"}})
        original=Path.is_symlink
        target=self.profiles/"template_old"
        with patch.object(Path,"is_symlink",lambda p: p==target or original(p)):
            self.rejected()

if __name__=="__main__":
    unittest.main()
