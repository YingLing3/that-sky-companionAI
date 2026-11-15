"""Offline synthetic-account generation: real Actor codec, filesystem and pin registry.
APK extraction/capture discovery use explicit synthetic adapters; no native scene proof claim.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path[:0] = [str(APP / "escort-v31-server"), str(APP)]
from xg_gamedata import actor, catalog, spawn_loader
from xg_gamedata.common import GameDataError
from xg_gamedata.apk_identity import SCHEMA
import qfsky
import qfsky.map_long0167
for pkg, folder in ((qfsky, APP / "escort-v31-server/qfsky"),
                    (qfsky.map_long0167, APP / "escort-v31-server/qfsky/map_long0167")):
    if str(folder) not in pkg.__path__:
        pkg.__path__.append(str(folder))
from qfsky.map_long0167 import asset_catalog_v32c as C, catalog_account_pins as P
from qfsky.version_profile import PROFILES, CURRENT_NATIVE_SHA256 as NATIVE
from qfsky.join0167_profile import account_binding
from qfsky.long0167.actor_codec.bit_stream import BitWriter

MAP = 507487826
SCENE = "SunsetEnd2"
VARIANT = "qianniaocheng"
NEW_SHA = "353ba870afe64b424caf3b6508d050fefdaa3fede8032a9ed6be9e6b0dd8f73a"
OLD_PLACEMENT_SHA = "1cb24031f4d5132f5fe29913feadafda743527de5e628e13c76621705a6e1473"
OLD_ANNOTATION_SHA = "db3539593747d63bac3aba5199f9092c549791aad51a55412ebeddf5bc018d73"

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def new_placement():
    return copy.deepcopy(spawn_loader.load(build=421629)["variants"][SCENE][VARIANT])

def synthetic_actor(mark=b"A"):
    """Zero layout plus ten synthetic strings; never reads a captured Actor."""
    aw = actor._codec()
    walk = aw._Walk(bytes(4096), True, False)
    walk.main()
    tokens = list(walk.tokens)
    empty = [i for i, t in enumerate(tokens) if t.kind == "raw" and t.extent == 0]
    assert len(empty) == 10
    for index in empty:
        tokens[index - 1] = replace(tokens[index - 1], value=struct.pack("<I", 2))
        tokens[index] = replace(tokens[index], extent=2, value=mark + b"\0")
    writer = BitWriter(max_bytes=4096)
    for token in tokens:
        if token.kind == "raw":
            writer.write_raw(token.value)
        else:
            writer.write_bits(token.value, token.extent)
    body = writer.to_bytes()
    observed = aw.inspect_actor_wire(body, verified_profile_sha256=NATIVE,
                                    check_zero_padding=True, require_canonical_writer=True)
    assert aw.reencode_actor_wire(observed, verified_profile_sha256=NATIVE) == body
    body, _ = actor.project(body, catalog.HOME, [0, .5, 0], 63789, NATIVE)
    return body

def observe(body):
    return actor._codec().inspect_actor_wire(body, verified_profile_sha256=NATIVE,
                    check_zero_padding=True, require_canonical_writer=True)

def positions(body):
    obs = observe(body)
    origin = actor._index_for(obs, actor.BOUNDARY_ORIGIN, "raw", 4)
    history = actor._index_for(obs, actor.BOUNDARY_HISTORY, "raw", 1) + 2
    return tuple(tuple(struct.unpack("<f", obs._tokens[start+k].value)[0] for k in range(3))
                 for start in (origin, history))

class SyntheticBuild:
    """Run actual catalog.build; only external-input and native-scene boundary adapters."""
    def __init__(self, root):
        self.root = Path(root)
        self.profile = PROFILES[421629]
        self.pack = copy.deepcopy(spawn_loader.load(build=421629))
        self.pack["placements"] = {SCENE: self.pack["placements"][SCENE]}
        self.pack["variants"] = {SCENE: self.pack["variants"][SCENE]}
        self.xg = SimpleNamespace(PROFILES_JSON=self.root/"profiles.json",
            ASSET_RELEASE=self.root/"release", RUNTIME_PRIVATE=self.root/"private",
            RUNTIME_DATA=self.root/"runtime")
        self.registry = self.root/"private/pins.json"
        self.xg.PROFILES_JSON.write_text(json.dumps({"maps": {}, "apk_build":421629,
            "resource_revision":self.profile["resource_revision"]}), encoding="utf-8")
        self.calls = []
        self.approved = []
        self.progress = SimpleNamespace(emit=lambda *a,**k:None, finish=lambda *a,**k:None)
        self.identity = {"build":421629,"native_sha256":NATIVE,
            "sha256":"0"*64, "resource_identity_schema":SCHEMA,
            "consumer_sha256":{"catalog":self.profile["resource_identity_sha256"]["catalog"]},
            "apk_archive_directory_sha256":self.profile["apk_directory_sha256"],
            "reference_apk_archive_directory_sha256":self.profile["apk_directory_sha256"]}
        self.proof = {"map_id":MAP,"scene":SCENE,"label":"Synthetic scene adapter",
            "netversion_u16":[63789,49307,23536,0,0,25536],
            "resource_revision":self.profile["resource_revision"],
            "provenance":{"scene_member_sha256":self.pack["placements"][SCENE]["provenance"]["source_member_sha256"]}}

    def run(self, number=1, fail=False):
        account_id = "synthetic_%d" % number
        uid = str(uuid.UUID(int=number))
        binding = account_binding(uid)
        donor_actor = synthetic_actor(bytes([64+number]))
        donor = {"actor":donor_actor,"actor_sha256":sha(donor_actor),
            "record_sha256":"1"*64,"record_dir":"synthetic_record_%d"%number,
            "netversion_u16":self.proof["netversion_u16"],
            "record":{"scene":"SyntheticDonor"},"folder":self.root/"unused_capture"}
        def load_trial(owner, map_id, variant=None):
            self.calls.append((number, map_id, variant))
            folder = self.xg.ASSET_RELEASE/"asset-catalog-acct"/account_id
            manifest = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
            pins = P.load_registry(self.registry)[binding]
            self.approved.append(C._validate_variants(manifest,manifest["entries"],pins))
            entry = C._entry_for(map_id,variant,manifest["entries"],manifest["variants"])
            asset = folder/SCENE
            if variant:
                asset = asset/"variants"/variant
            placement = json.loads((asset/"placement.json").read_text(encoding="utf-8"))
            C._placement(placement,entry)
            actual = (asset/"actor.bin").read_bytes()
            if fail and variant:
                raise ValueError("synthetic_failure_after_publish")
            assert sha(actual) == entry["actor_sha256"]
            assert positions(actual)[0] == positions(actual)[1]
            profile = SimpleNamespace(binding=binding,record={"actor_sha256":sha(actual)},
                cache=(folder/SCENE/"cache.bin").read_bytes(), blob=(folder/SCENE/"scene.bin").read_bytes())
            return None, profile, entry
        with ExitStack() as stack:
            for obj, name, value in [(catalog,"_bind",lambda data:self.xg),
                (catalog,"_account",lambda xg,acct:{"id":account_id,"uuid":uid,"binding":binding}),
                (catalog,"_donor",lambda *a:donor),
                (catalog,"_scene_inputs",lambda *a:{SCENE:{"proof":copy.deepcopy(self.proof),"wire":b"synthetic-scene","cache":b"synthetic-cache"}}),
                (spawn_loader,"load",lambda **k:self.pack),
                (P,"registry_path",lambda:self.registry)]:
                stack.enter_context(patch.object(obj,name,value))
            stack.enter_context(patch("xg_gamedata.apk.inspect_apk",return_value=self.identity))
            stack.enter_context(patch("xg_gamedata.scene_inputs_build.ensure_scene_inputs",return_value={"generated":0,"reused":1}))
            stack.enter_context(patch("qfsky.asset_catalog_runtime_v32c.load_catalog_trial",side_effect=load_trial))
            result = catalog.build(self.root/"synthetic-not-opened.apk",account_id,self.root,None,None,self.progress)
        return result

class ActorProjectionTests(unittest.TestCase):
    def test_two_positions_float32_wire_not_geometry(self):
        p = new_placement()
        body,_ = actor.project(synthetic_actor(),MAP,p["wire_position"],63789,NATIVE)
        self.assertEqual(positions(body), (tuple(p["wire_position_float32"]),)*2)
        self.assertNotEqual(positions(body)[0][1],p["feet_position"][1])
        self.assertEqual(p["wire_position"][1]-p["feet_position"][1],.5)

    def test_accounts_keep_distinct_actor_content(self):
        a,_=actor.project(synthetic_actor(b"A"),MAP,new_placement()["wire_position"],63789,NATIVE)
        b,_=actor.project(synthetic_actor(b"B"),MAP,new_placement()["wire_position"],63789,NATIVE)
        self.assertNotEqual(sha(a),sha(b))
        self.assertEqual(positions(a),positions(b))

    def test_only_documented_projection_tokens_change(self):
        before=synthetic_actor();after,_=actor.project(before,MAP,new_placement()["wire_position"],63789,NATIVE)
        old,new=observe(before),observe(after)
        origin=actor._index_for(old,actor.BOUNDARY_ORIGIN,"raw",4)
        hist=actor._index_for(old,actor.BOUNDARY_HISTORY,"raw",1)
        main=actor._index_for(old,actor.BOUNDARY_MAIN_MAP,"raw",4)
        vel=actor._index_for(old,actor.BOUNDARY_VELOCITY,"bits",8)
        state=actor._index_for(old,actor.BOUNDARY_LEVEL_STATE,"raw",2)
        allowed={main,hist+1,*range(origin,origin+3),*range(hist+2,hist+5),*range(vel,vel+3),*range(state,state+3)}
        self.assertEqual(len(old._tokens),len(new._tokens))
        for i,(a,b) in enumerate(zip(old._tokens,new._tokens)):
            if i not in allowed:self.assertEqual(a,b)
        self.assertEqual(len(before),len(after))
        self.assertTrue(new.matches_prefix_u32(MAP))
        self.assertEqual(actor.level_state(after,NATIVE)["resource_tag"],63789)

    def test_bad_native_profile_still_rejected(self):
        with self.assertRaises(GameDataError):
            actor.project(synthetic_actor(),MAP,new_placement()["wire_position"],63789,"0"*64)

class VariantPolicyTests(unittest.TestCase):
    def row(self):
        row=copy.deepcopy(C.EXPECTED_VARIANTS[str(MAP)][VARIANT]);row["actor_sha256"]="a"*64
        return row
    def test_policy_has_no_copied_account_actor(self):
        self.assertNotIn("actor_sha256",C.EXPECTED_VARIANTS[str(MAP)][VARIANT])
        self.assertEqual(C._legacy_pins()["variants"],{})
    def test_no_account_variant_pins_fail_closed(self):
        with self.assertRaisesRegex(ValueError,"^asset_catalog_variant_account_pins_required$"):
            C._entry_for(MAP,VARIANT)
    def test_current_account_variant_pin_used(self):
        row=self.row();entry=C._entry_for(MAP,VARIANT,variants={str(MAP):{VARIANT:row}})
        self.assertEqual(entry["actor_sha256"],row["actor_sha256"])
        self.assertEqual(entry["placement_sha256"],NEW_SHA)
    def test_old_variant_placement_rejected(self):
        row=self.row();row["placement_sha256"]=OLD_PLACEMENT_SHA
        with self.assertRaisesRegex(ValueError,"^asset_catalog_qianniaocheng_stale$"):
            C._entry_for(MAP,VARIANT,variants={str(MAP):{VARIANT:row}})
    def test_old_variant_annotation_rejected(self):
        row=self.row();row["annotation_sha256"]=OLD_ANNOTATION_SHA
        with self.assertRaisesRegex(ValueError,"^asset_catalog_qianniaocheng_stale$"):
            C._entry_for(MAP,VARIANT,variants={str(MAP):{VARIANT:row}})
    def test_mutable_convenience_policy_cannot_allow_stale_point(self):
        row=self.row();row["placement_sha256"]=OLD_PLACEMENT_SHA
        with patch.dict(C.EXPECTED_VARIANTS,{},clear=True):
            with self.assertRaisesRegex(ValueError,"^asset_catalog_qianniaocheng_stale$"):
                C._entry_for(MAP,VARIANT,variants={str(MAP):{VARIANT:row}})
    def test_policy_source_tamper_fails_closed(self):
        row=self.row()
        with patch.object(C,"_EXPECTED_VARIANTS_JSON","{}"):
            with self.assertRaisesRegex(ValueError,"^asset_catalog_variant_policy_pin$"):
                C._entry_for(MAP,VARIANT,variants={str(MAP):{VARIANT:row}})
    def test_missing_policy_fails_even_if_pin_matches_empty_table(self):
        row=self.row()
        with patch.object(C,"_EXPECTED_VARIANTS_JSON","{}"), patch.object(C,"_EXPECTED_VARIANTS_SHA",sha(C.canonical_json({}))):
            with self.assertRaisesRegex(ValueError,"^asset_catalog_variant_policy_shape$"):
                C._entry_for(MAP,VARIANT,variants={str(MAP):{VARIANT:row}})
    def test_pinned_variant_tamper_still_rejected(self):
        row=self.row();entries={str(MAP):{"scene":SCENE}}
        pins={"variants":{str(MAP):{VARIANT:row}}}
        m=copy.deepcopy(pins);m["variants"][str(MAP)][VARIANT]["actor_sha256"]="b"*64
        with self.assertRaisesRegex(ValueError,"^asset_catalog_variants_pin$"):
            C._validate_variants(m,entries,pins)
    def test_main_does_not_select_stale_variant(self):
        self.assertEqual(C._entry_for(MAP),C._entry_for(MAP,variants={str(MAP):{VARIANT:{"placement_sha256":OLD_PLACEMENT_SHA}}}))
    def test_manual_override_remains_failed_static_check(self):
        p=new_placement();check=p["provenance"]["engineering_check"]
        self.assertIs(check["ok"],False)
        self.assertEqual(check["reasons"],["footprint_missing_up_face_within_0.25"])
        self.assertIs(p["provenance"]["user_override"],True)
        self.assertEqual(sha(C.canonical_json(p)),NEW_SHA)
        C._placement(p,{"scene":SCENE,"level_id":MAP,"placement_sha256":NEW_SHA,"netversion_u16":[0]*6})
    def test_current_source_member_gate_not_weakened(self):
        p=new_placement();proof={"scene":SCENE,"provenance":{"scene_member_sha256":"0"*64}}
        with self.assertRaises(GameDataError) as caught:catalog._placement_evidence(proof,p,PROFILES[421629])
        self.assertEqual(caught.exception.code,"placement_apk_mismatch")

class CatalogBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="spawn13-")
        self.addCleanup(self.temp.cleanup)
        self.build=SyntheticBuild(self.temp.name)
    def test_real_build_registers_account_specific_actor_hashes(self):
        with patch.dict(os.environ,{"PATH":""}):
            a=self.build.run(1);b=self.build.run(2)
        manifests=[json.loads(Path(x["manifest"]).read_text(encoding="utf-8")) for x in (a,b)]
        rows=[m["variants"][str(MAP)][VARIANT] for m in manifests]
        self.assertNotEqual(rows[0]["actor_sha256"],rows[1]["actor_sha256"])
        self.assertNotEqual(manifests[0]["source_account_binding_hash"],manifests[1]["source_account_binding_hash"])
        registry=P.load_registry(self.build.registry)
        for result,m,row in zip((a,b),manifests,rows):
            self.assertEqual(row["placement_sha256"],NEW_SHA)
            self.assertEqual(registry[result["binding"]]["variants"],m["variants"])
            output=Path(result["catalog_dir"])/SCENE/"variants"/VARIANT
            self.assertEqual(sha((output/"actor.bin").read_bytes()),row["actor_sha256"])
            self.assertEqual(json.loads((output/"placement.json").read_text(encoding="utf-8")),new_placement())
            self.assertEqual(result["maps"],1);self.assertEqual(result["variants"],1)
        self.assertEqual(len(self.build.calls),4)
    def test_new_failure_removes_partial_account_and_registrations(self):
        with self.assertRaises(GameDataError) as caught:self.build.run(1,fail=True)
        self.assertEqual(caught.exception.code,"catalog_self_check")
        self.assertFalse(self.build.registry.exists())
        self.assertFalse((self.build.xg.RUNTIME_PRIVATE/"catalog_sources.json").exists())
        self.assertEqual(list((self.build.xg.ASSET_RELEASE/"asset-catalog-acct").iterdir()),[])
    def test_failed_rebuild_preserves_prior_files_and_pins(self):
        result=self.build.run(1);folder=Path(result["catalog_dir"])
        before={p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        registry=self.build.registry.read_bytes()
        sources=(self.build.xg.RUNTIME_PRIVATE/"catalog_sources.json").read_bytes()
        with self.assertRaises(GameDataError):self.build.run(1,fail=True)
        after={p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        self.assertEqual(after,before)
        self.assertEqual(self.build.registry.read_bytes(),registry)
        self.assertEqual((self.build.xg.RUNTIME_PRIVATE/"catalog_sources.json").read_bytes(),sources)

if __name__=="__main__":unittest.main()
