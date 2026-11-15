"""Channel resource provenance gates; synthetic proofs, no captures or account I/O."""
from pathlib import Path
import copy
import hashlib
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
sys.path[:0]=[str(APP/'escort-v31-server'),str(APP)]
import qfsky
import qfsky.map_long0167
for package,path in ((qfsky,APP/'escort-v31-server/qfsky'),
                     (qfsky.map_long0167,APP/'escort-v31-server/qfsky/map_long0167')):
    if str(path) not in package.__path__:package.__path__.append(str(path))
from qfsky import apk_resource_identity as identity
from qfsky.map_long0167 import asset_bootstrap_v32 as B
from qfsky.map_long0167 import asset_catalog_v32c as C
from xg_gamedata import spawn_loader
from xg_gamedata import apk_identity
from qfsky.version_profile import PROFILES

SCHEMA='xg-apk-resource-members-v1'
SUBSET='1'*64
CHANNEL='2'*64

def digest(raw):return hashlib.sha256(raw).hexdigest()
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def profile():
    return {'build':421629,'native_sha256':B.NATIVE_SHA256,
            'apk_directory_sha256':B.APK_DIRECTORY_SHA,
            'resource_identity_schema':SCHEMA,'resource_identity_sha256':{'catalog':SUBSET}}
def provenance(channel=True):
    result={'native_sha256':B.NATIVE_SHA256,'apk_build':421629,
            'apk_archive_directory_sha256':CHANNEL if channel else B.APK_DIRECTORY_SHA}
    if channel:
        result.update(resource_identity_schema=SCHEMA,resource_identity_scope='catalog',
                      resource_identity_sha256=SUBSET,
                      reference_apk_archive_directory_sha256=B.APK_DIRECTORY_SHA)
    return result
def match(value):
    return identity.resource_provenance_matches(value,native_sha256=B.NATIVE_SHA256,
                                                reference_directory_sha256=B.APK_DIRECTORY_SHA)


class ResourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(identity,'profile_for_native_sha256',return_value=profile())
        self.patch.start();self.addCleanup(self.patch.stop)

    def test_legacy_exact_identity_still_passes(self):
        self.assertTrue(match(provenance(False)))

    def test_changed_archive_without_subset_is_rejected(self):
        value=provenance(False);value['apk_archive_directory_sha256']=CHANNEL
        self.assertFalse(match(value))

    def test_channel_proof_passes_without_rewriting_actual_archive(self):
        value=provenance();before=copy.deepcopy(value)
        self.assertTrue(match(value));self.assertEqual(value,before)
        self.assertNotEqual(value['apk_archive_directory_sha256'],B.APK_DIRECTORY_SHA)

    def test_wrong_subset_scope_schema_and_reference_fail_closed(self):
        for key,value in [('resource_identity_sha256','3'*64),('resource_identity_scope','props'),
                          ('resource_identity_schema','unknown'),
                          ('reference_apk_archive_directory_sha256','4'*64)]:
            with self.subTest(key=key):
                row=provenance();row[key]=value;self.assertFalse(match(row))

    def test_each_missing_subset_field_rejected(self):
        for key in identity._RESOURCE_KEYS:
            with self.subTest(key=key):
                value=provenance();value.pop(key);self.assertFalse(match(value))

    def test_bad_new_proof_cannot_fall_back_to_legacy_exact_archive(self):
        row=provenance(False);row['resource_identity_sha256']=SUBSET
        self.assertFalse(match(row))

    def test_native_change_is_rejected(self):
        row=provenance();row['native_sha256']='5'*64;self.assertFalse(match(row))

    def test_unknown_profile_or_missing_digest_cannot_pass(self):
        for candidate in (None,{'build':421629,'apk_directory_sha256':B.APK_DIRECTORY_SHA}):
            with patch.object(identity,'profile_for_native_sha256',return_value=candidate):
                self.assertFalse(match(provenance()))

    def test_bad_actual_directory_shape_rejected(self):
        for value in (None,1,'not-a-hash','A'*64):
            row=provenance();row['apk_archive_directory_sha256']=value;self.assertFalse(match(row))

    def test_wrong_build_and_boolean_build_rejected(self):
        for value in (421589,'421629',True):
            row=provenance();row['apk_build']=value;self.assertFalse(match(row))

    def test_invalid_provenance_container_returns_false(self):
        for value in (None,[],1,'x'):self.assertFalse(match(value))


class RuntimeSceneProofTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(identity,'profile_for_native_sha256',return_value=profile())
        self.patch.start();self.addCleanup(self.patch.stop)
        self.cache=b'synthetic-cache';self.wire=b'synthetic-wire'
        self.observed=SimpleNamespace(_counts={})

    def catalog_fixture(self,channel=True):
        prov=provenance(channel)
        prov.update(scene_member='assets/Data/Levels/Synthetic/Objects.level.bin',
                    scene_member_sha256='6'*64,runtime_capture=False,native_record_comparison=False)
        proof={'schema':'sky-catalog-partial-scene-inputs-v32',
               'phase':'finite_initializers_only_before_first_event_update',
               'scene':'Synthetic','map_id':123,'resource_revision':'fixture-revision',
               'netversion_u16':[1,2,3,0,0,0],'native_sha256':B.NATIVE_SHA256,
               'canonical_roundtrip':True,'missing_scene_inputs':['synthetic-incomplete'],
               'wire_variable_group_counts':{},'provenance':prov,
               'artifacts':{key:{'sha256':digest(data),'bytes':len(data)}
                            for key,data in [('cache',self.cache),('wire',self.wire)]}}
        for key in ('complete_scene_initialization','loaded_scene_snapshot','full_connection_ready',
                    'capture_generated','transport_used'):proof[key]=False
        placement={'provenance':{'source_member':prov['scene_member'],'source_member_sha256':prov['scene_member_sha256']}}
        entry={'scene':'Synthetic','level_id':123,'resource_revision':'fixture-revision',
               'netversion_u16':proof['netversion_u16'],'scene_provenance_sha256':digest(canonical(proof))}
        return proof,entry,placement

    def check_catalog(self,proof,entry,placement,repin=True):
        if repin:entry['scene_provenance_sha256']=digest(canonical(proof))
        return C._scene_proof(proof,entry,self.observed,self.cache,self.wire,placement)

    def bootstrap_fixture(self,channel=True):
        prov=provenance(channel)
        prov.update(scene_member='assets/Data/Levels/NightArchive/Objects.level.bin',
                    scene_member_sha256=B.MEMBER_SHA,runtime_capture=False,native_record_comparison=False)
        schema='sky-asset-scene-candidate-v32'
        proof={'schema':schema,'candidate_phase':B._PHASES[schema][0],
               'candidate_kind':B._PHASES[schema][1],'scene':B.SCENE,'map_id':B.MAP_ID,
               'resource_revision':B.RESOURCE_REVISION,'netversion_u16':list(B.NV),
               'resource_tag_u16':B.NV[0],'canonical_roundtrip':True,
               'wire_sha256':digest(self.wire),'cache_sha256':digest(self.cache),'wire_bytes':len(self.wire),
               'wire_variable_group_counts':{},'missing_scene_inputs':['synthetic-incomplete'],'provenance':prov}
        for key in ('complete_scene_initialization','loadedSceneSnapshot','capture_generated','full_connection_ready','transport_used'):
            proof[key]=False
        return proof

    def check_bootstrap(self,proof):
        return B._scene_proof(proof,{'scene_provenance_sha256':digest(canonical(proof))},self.observed,self.cache,self.wire)

    def test_catalog_accepts_channel_subset_proof(self):
        self.check_catalog(*self.catalog_fixture())

    def test_catalog_accepts_legacy_exact_proof(self):
        self.check_catalog(*self.catalog_fixture(False))

    def test_catalog_rejects_incorrect_subset_after_manifest_repin(self):
        proof,entry,placement=self.catalog_fixture();proof['provenance']['resource_identity_sha256']='7'*64
        with self.assertRaisesRegex(ValueError,'scene_resource_provenance'):self.check_catalog(proof,entry,placement)

    def test_catalog_member_pin_not_weakened(self):
        proof,entry,placement=self.catalog_fixture();proof['provenance']['scene_member_sha256']='7'*64
        with self.assertRaisesRegex(ValueError,'scene_resource_provenance'):self.check_catalog(proof,entry,placement)

    def test_catalog_artifact_pin_not_weakened(self):
        proof,entry,placement=self.catalog_fixture();proof['artifacts']['wire']['sha256']='7'*64
        with self.assertRaisesRegex(ValueError,'scene_artifact_pin'):self.check_catalog(proof,entry,placement)

    def test_catalog_outer_proof_pin_not_weakened(self):
        proof,entry,placement=self.catalog_fixture();proof['provenance']['apk_archive_directory_sha256']='8'*64
        with self.assertRaisesRegex(ValueError,'scene_provenance_pin'):self.check_catalog(proof,entry,placement,False)

    def test_catalog_readiness_claim_still_rejected(self):
        proof,entry,placement=self.catalog_fixture();proof['loaded_scene_snapshot']=True
        with self.assertRaisesRegex(ValueError,'scene_false_readiness'):self.check_catalog(proof,entry,placement)

    def test_bootstrap_accepts_channel_proof(self):
        self.check_bootstrap(self.bootstrap_fixture())

    def test_bootstrap_legacy_exact_still_passes(self):
        self.check_bootstrap(self.bootstrap_fixture(False))

    def test_bootstrap_rejects_wrong_member_and_subset(self):
        for key in ('scene_member_sha256','resource_identity_sha256'):
            proof=self.bootstrap_fixture();proof['provenance'][key]='9'*64
            with self.assertRaisesRegex(ValueError,'scene_asset_provenance'):self.check_bootstrap(proof)

    def test_bootstrap_false_readiness_still_rejected(self):
        proof=self.bootstrap_fixture();proof['loadedSceneSnapshot']=True
        with self.assertRaisesRegex(ValueError,'scene_false_readiness'):self.check_bootstrap(proof)

    def test_all_current_placement_branches_accept_valid_channel_identity(self):
        pack=spawn_loader.load(build=421629);items=list(pack['placements'].values())
        items.extend(value for variants in pack['variants'].values() for value in variants.values())
        for original in items:
            with self.subTest(scene=original['scene']):
                value=copy.deepcopy(original);value['provenance'].update(provenance())
                entry={'scene':value['scene'],'level_id':value['map_id'],
                       'placement_sha256':digest(canonical(value)),'netversion_u16':[0]*6}
                C._placement(value,entry)
        self.assertEqual(len(items),116)

    def test_placement_new_identity_does_not_hide_wrong_subset(self):
        value=copy.deepcopy(next(iter(spawn_loader.load(build=421629)['placements'].values())))
        value['provenance'].update(provenance());value['provenance']['resource_identity_sha256']='0'*64
        entry={'scene':value['scene'],'level_id':value['map_id'],
               'placement_sha256':digest(canonical(value)),'netversion_u16':[0]*6}
        with self.assertRaises(ValueError):C._placement(value,entry)


class PinnedProfileContractTests(unittest.TestCase):
    def test_production_provenance_builder_matches_actual_runtime_profile(self):
        pinned=PROFILES[421629]
        checked={'build':421629,'native_sha256':pinned['native_sha256'],
                 'resource_identity_schema':pinned['resource_identity_schema'],
                 'consumer_sha256':{'catalog':pinned['resource_identity_sha256']['catalog']},
                 'apk_archive_directory_sha256':CHANNEL,
                 'reference_apk_archive_directory_sha256':pinned['apk_directory_sha256']}
        proof=apk_identity.resource_provenance(checked,'catalog')
        self.assertTrue(match(proof))
        self.assertEqual(proof['apk_archive_directory_sha256'],CHANNEL)
        self.assertEqual(proof['resource_identity_scope'],'catalog')

    def test_production_builder_rejects_missing_or_legacy_consumer_proof(self):
        for candidate in ({},{'legacy':True}):
            with self.assertRaises(apk_identity.GameDataError):
                apk_identity.resource_provenance(candidate,'catalog')


if __name__=='__main__':unittest.main()
