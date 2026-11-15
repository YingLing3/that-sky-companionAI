"""Consumer-level channel APK regressions. ZIP/native/mesh payloads are synthetic."""
from pathlib import Path
import ast
import hashlib
import json
import re
import sys
import types
import unittest
from unittest.mock import patch
import zipfile
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
sys.path[:0]=[str(APP),str(APP/'src'),str(ROOT/'tests')]
import test_apk_identity12 as fixture
from xg_gamedata import props as gen, apk_identity as identity, terrain
from xg_gamedata.common import GameDataError
from xg_gamedata.prop_build import extract,budget,reviewed_geometry,rules
from prop_synthetic_fixtures import profile
NATIVE=fixture.NATIVE
SCENE=fixture.SCENE
MESH=fixture.MESH
DEFS=fixture.DEFS
BAKED='assets/Data/Levels/CandleSpace/BstBaked.meshes'
RES='assets/Data/Levels/CandleSpace/Resources.lua.luac'
PIPE=APP/'xg_gamedata/terrain/pipeline'


def helpers(path,names,extra=None):
    source=Path(path).read_text(encoding='utf8');tree=ast.parse(source)
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    assert len(nodes)==len(names)
    ns=dict(__file__=str(path),Path=Path,zipfile=zipfile,hashlib=hashlib,json=json,re=re,sys=sys,
        validate_apk_members=identity.validate_apk_members,EMPTY_HASH='0'*64,
        scene_hash=lambda name:'synthetic-'+name,MAX_MEMBER=256*1024*1024,
        PROFILE='synthetic-profile',ELF_SHA256='unset')
    ns.update(extra or {})
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)
    return ns


class GeneratorChannelTests(unittest.TestCase):
    write_apk=fixture.ApkIdentityTests.write_apk
    def setUp(self):
        fixture.ApkIdentityTests.setUp(self)
        self.payloads.update({BAKED:b'synthetic baked marker',RES:b'synthetic Lua marker'})
        self.base=self.write_apk('generator-base.apk')
        with zipfile.ZipFile(self.base) as z:
            self.manifest['members'].update({i.filename:{'size':i.file_size,'crc32':i.CRC} for i in z.infolist()})
            actual=identity.archive_directory_sha(z)
        self.manifest['reference_apk_archive_directory_sha256']=actual;self.profile['apk_directory_sha256']=actual
        self.manifest['consumers']['terrain']['members'].extend([SCENE,BAKED,RES])
        self.repin()
        self.native=self.manifest['native_sha256']
        self.inventory=helpers(PIPE/'tools/build_all_maps.py',{'inventory','_resume_identity_matches'})
        self.verify=helpers(PIPE/'tools/build_beamo_world.py',{'verify_profile'},{'ELF_SHA256':self.native})['verify_profile']
        self.build_calls=[]
    def repin(self):
        self.profile['resource_identity_sha256']={c:identity.consumer_digest(self.manifest,c) for c in identity.CONSUMERS}
    def fake_extract(self,z,apk_identity,out_dir,*,primary_index=None,progress=None):
        # Real consumer validation remains active; only expensive game codecs are
        # replaced with mathematical shapes, never reference APK data.
        self.build_calls.append(apk_identity)
        rows={}
        for i in range(174):
            name='SyntheticProp%03d'%i;shape=profile();rel='all/runtime/'+name+'.json'
            file=out_dir/'runtime'/(name+'.json')
            sha=gen._write(file,dict(schema='skyterrain-props-v1',coordinate_space='sky_native',props={name:shape}))
            rows[name]=dict(status='ready',runtime_eligible=True,reasons=[],shard=rel,sha256=sha,
                byte_length=file.stat().st_size,vertex_count=8,triangle_count=12,source_kind='render_mesh_candidate')
        for i in range(348):
            rows['SyntheticMissing%03d'%i]=dict(status='ineligible',runtime_eligible=False,reasons=['synthetic'],
                shard=None,sha256=None,byte_length=0,vertex_count=0,triangle_count=0,source_kind='unavailable')
        index=dict(schema='skyterrain-props-index-v1',coordinate_space='sky_native',apk=apk_identity,props=rows,counts={})
        gen._write(primary_index,index);return index
    def build(self,apk,tag):
        scratch=self.root/tag;scratch.mkdir()
        with patch.object(extract,'build_all_index',side_effect=self.fake_extract),patch.object(budget,'extend',side_effect=lambda scratch,index,progress:index):
            return gen._build(Path(apk),scratch,lambda event:None)
    def generate(self,apk,output):
        with patch.object(extract,'build_all_index',side_effect=self.fake_extract),patch.object(budget,'extend',side_effect=lambda scratch,index,progress:index):
            return gen.generate(apk,output)
    def assert_rejected_before_decoder(self,path,code):
        count=len(self.build_calls)
        with self.assertRaises(GameDataError) as caught:self.build(path,'reject-'+str(count))
        self.assertEqual(caught.exception.code,code);self.assertEqual(len(self.build_calls),count)
    def test_props_channel_order_and_signature_change_preserve_canonical_identity(self):
        variant=self.write_apk('channel.apk',reverse=True,extra=[('META-INF/CHANNEL.SF',b'channel')],compressed=True)
        _,a=self.build(self.base,'a');_,b=self.build(variant,'b')
        self.assertNotEqual(a['sha256'],b['sha256']);self.assertNotEqual(a['apk_archive_directory_sha256'],b['apk_archive_directory_sha256'])
        self.assertEqual(a['resource_identity_sha256'],b['resource_identity_sha256'])
        self.assertEqual(b['sha256'],hashlib.sha256(variant.read_bytes()).hexdigest())
        self.assertNotEqual(b['apk_archive_directory_sha256'],b['reference_apk_archive_directory_sha256'])
    def test_props_unused_assets_data_payload_is_not_globally_locked(self):
        variant=self.write_apk('unused.apk',extra=[('assets/Data/Textures/Unused.png',b'opaque')])
        _,result=self.build(variant,'unused');self.assertEqual(result['resource_identity_sha256'],self.profile['resource_identity_sha256']['props'])
    def test_props_changed_consumed_member_rejected_before_decoder(self):self.assert_rejected_before_decoder(self.write_apk('bad.apk',{MESH:b'changed'}),'apk_resource_mismatch')
    def test_props_missing_consumed_member_rejected_before_decoder(self):self.assert_rejected_before_decoder(self.write_apk('missing.apk',removed=[DEFS]),'apk_resource_mismatch')
    def test_props_wrong_native_rejected_before_decoder(self):self.assert_rejected_before_decoder(self.write_apk('native.apk',{NATIVE:b'wrong native'}),'apk_identity_mismatch')
    def test_props_extra_definition_table_changes_discovery_rejected(self):
        self.assert_rejected_before_decoder(self.write_apk('extra-def.apk',extra=[('assets/Data/Resources/PlaceableDefsExtra.json',b'[]')]),'apk_resource_mismatch')
    def test_props_duplicate_consumed_member_rejected(self):self.assert_rejected_before_decoder(self.write_apk('dup-used.apk',extra=[(DEFS,self.payloads[DEFS])]),'apk_resource_mismatch')
    def test_props_duplicate_unrelated_channel_is_ignored(self):
        variant=self.write_apk('dup-meta.apk',extra=[('META-INF/CHANNEL.SF',b'a'),('META-INF/CHANNEL.SF',b'b')])
        _,result=self.build(variant,'dup');self.assertEqual(result['resource_identity_sha256'],self.profile['resource_identity_sha256']['props'])
    def test_whole_apk_sha_no_longer_policy_gate(self):
        proof=identity.validate_apk_members(self.base,('props',),expected_build=421629)
        with patch.dict(rules.POLICY,{'native_sha256':self.native}):
            a=reviewed_geometry.load_policy(dict(proof,sha256='1'*64));b=reviewed_geometry.load_policy(dict(proof,sha256='2'*64))
        self.assertEqual(a,b)
    def test_policy_still_requires_native_build_and_props_proof(self):
        proof=identity.validate_apk_members(self.base,('props',),expected_build=421629)
        changes=[{'native_sha256':'f'*64},{'build':421589},{'identity_mode':'whole-apk'},{'resource_identity_sha256':None},{'consumer_sha256':{'terrain':'a'*64}}]
        with patch.dict(rules.POLICY,{'native_sha256':self.native}):
            for changed in changes:
                with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'unsupported_apk_geometry_policy'):reviewed_geometry.load_policy(dict(proof,**changed))
    def test_generated_marker_records_true_variant_hash_and_separate_resource_pin(self):
        variant=self.write_apk('marker.apk',extra=[('META-INF/CHANNEL.SF',b'changed')]);out=self.root/'published'
        result=self.generate(variant,out);marker=json.loads((out/'generation.json').read_text(encoding='utf8'))
        self.assertEqual(marker['apk_sha256'],hashlib.sha256(variant.read_bytes()).hexdigest())
        self.assertEqual(marker['resource_identity_sha256'],self.profile['resource_identity_sha256']['props'])
        self.assertNotEqual(marker['apk_archive_directory_sha256'],marker['reference_apk_archive_directory_sha256'])
        self.assertEqual((result['ready'],result['known']),(174,522));self.assertTrue(gen.validate_generated(out)['ready'])
    def test_resource_rejection_preserves_previous_complete_output(self):
        out=self.root/'published';self.generate(self.base,out)
        before={p.relative_to(out).as_posix():p.read_bytes() for p in out.rglob('*') if p.is_file()}
        with self.assertRaises(gen.PropGenerationError) as caught:self.generate(self.write_apk('bad-publish.apk',{DEFS:b'changed'}),out)
        self.assertEqual(caught.exception.code,'apk_resource_mismatch')
        after={p.relative_to(out).as_posix():p.read_bytes() for p in out.rglob('*') if p.is_file()};self.assertEqual(before,after)
    def test_terrain_inventory_rows_stable_actual_directory_not_forged(self):
        variant=self.write_apk('terrain-channel.apk',reverse=True,extra=[('META-INF/CHANNEL.SF',b'new')])
        rows,a=self.inventory['inventory'](self.base,with_identity=True);rows2,b=self.inventory['inventory'](variant,with_identity=True)
        self.assertEqual(rows,rows2);self.assertEqual(a['resource_identity_sha256'],b['resource_identity_sha256'])
        self.assertNotEqual(a['terrain_archive_directory_sha256'],b['terrain_archive_directory_sha256'])
        legacy_rows,actual=self.inventory['inventory'](variant);self.assertEqual(legacy_rows,rows2);self.assertEqual(actual,b['terrain_archive_directory_sha256'])
    def test_terrain_changed_payload_rejected_before_inventory_ready(self):
        path=self.write_apk('terrain-bad.apk',{BAKED:b'changed'})
        with self.assertRaises(GameDataError) as caught:self.inventory['inventory'](path)
        self.assertEqual(caught.exception.code,'apk_resource_mismatch')
    def test_terrain_ignores_unused_props_member_change(self):
        path=self.write_apk('only-props-bad.apk',{DEFS:b'changed'})
        rows,proof=self.inventory['inventory'](path,with_identity=True);self.assertEqual(len(rows),1)
        self.assertEqual(proof['resource_identity_sha256'],self.profile['resource_identity_sha256']['terrain'])
    def test_terrain_unused_duplicate_passes_but_consumed_duplicate_rejects(self):
        path=self.write_apk('terrain-meta-dup.apk',extra=[('META-INF/X',b'a'),('META-INF/X',b'b')])
        rows,_=self.inventory['inventory'](path,with_identity=True);self.assertEqual(len(rows),1)
        path=self.write_apk('terrain-used-dup.apk',extra=[(BAKED,self.payloads[BAKED])])
        with self.assertRaises(GameDataError) as caught:self.inventory['inventory'](path)
        self.assertEqual(caught.exception.code,'apk_resource_mismatch')
    def test_name_only_inventory_does_not_lock_unread_payload_crc(self):
        a='assets/Data/UnusedOne/Choice.mesh';b='assets/Data/UnusedTwo/Choice.mesh'
        self.manifest['consumers']['terrain']['inventories']=[dict(label='ambiguous-only',pattern=r'assets/Data/Unused(?:One|Two)/Choice\.mesh',members=[a,b])];self.repin()
        first=self.write_apk('names-only-a.apk',extra=[(a,b'old'),(b,b'other')]);second=self.write_apk('names-only-b.apk',extra=[(a,b'longer different unread bytes'),(b,b'other')])
        x=identity.validate_apk_members(first,('terrain',));y=identity.validate_apk_members(second,('terrain',))
        self.assertEqual(x['resource_identity_sha256'],y['resource_identity_sha256']);self.assertNotEqual(x['apk_archive_directory_sha256'],y['apk_archive_directory_sha256'])
    def test_new_ambiguous_mesh_name_is_discovery_change(self):
        self.manifest['consumers']['terrain']['inventories']=[dict(label='used-basename',pattern=r'assets/.*/Synthetic\.mesh',members=[MESH])];self.repin()
        path=self.write_apk('ambiguous.apk',extra=[('assets/Other/Synthetic.mesh',b'unused-until-added')])
        with self.assertRaises(GameDataError) as caught:self.inventory['inventory'](path)
        self.assertEqual(caught.exception.code,'apk_resource_mismatch')
    def test_resume_uses_canonical_identity_for_channel_variants(self):
        match=self.inventory['_resume_identity_matches'];proof=dict(resource_identity_sha256='a'*64,terrain_archive_directory_sha256='b'*64)
        self.assertTrue(match(dict(resource_identity_sha256='a'*64,archive_directory_sha256='c'*64),proof))
        self.assertFalse(match(dict(resource_identity_sha256='d'*64,archive_directory_sha256='b'*64),proof))
    def test_legacy_resume_retains_exact_actual_input_contract(self):
        match=self.inventory['_resume_identity_matches'];proof=dict(resource_identity_sha256='a'*64,terrain_archive_directory_sha256='b'*64)
        self.assertTrue(match(dict(archive_directory_sha256='b'*64),proof));self.assertFalse(match(dict(archive_directory_sha256='c'*64),proof))
    def test_beamo_profile_uses_same_terrain_native_and_member_validator(self):
        variant=self.write_apk('beamo-meta.apk',extra=[('META-INF/X',b'a'),('META-INF/X',b'b')])
        with zipfile.ZipFile(variant) as z:result=self.verify(z)
        self.assertEqual(result['sha256'],self.native);self.assertEqual(result['resource_identity_sha256'],self.profile['resource_identity_sha256']['terrain'])
        self.assertNotEqual(result['apk_archive_directory_sha256'],result['reference_apk_archive_directory_sha256'])
        bad=self.write_apk('beamo-bad.apk',{MESH:b'changed'})
        with zipfile.ZipFile(bad) as z,self.assertRaises(GameDataError):self.verify(z)
    def test_generic_unprofiled_reader_keeps_strict_duplicate_default(self):
        path=self.write_apk('generic-dup.apk',extra=[('META-INF/X',b'a'),('META-INF/X',b'b')])
        ns=helpers(PIPE/'src/skyterrain/cli.py',{'build_map'},{'os':__import__('os'),'NodeMeshoptDecoder':lambda *a:None,'FormatError':ValueError,'xg_paths':types.SimpleNamespace(require_node_exe=lambda value:value)})
        with self.assertRaisesRegex(ValueError,'duplicate_archive_members'):
            ns['build_map'](path,'CandleSpace',self.root/'generic-output',node='unused',meshopt='unused',profile=None)
    def test_terrain_launcher_scopes_validation_to_terrain_and_current_build(self):
        with patch.object(terrain,'ensure_pipeline'),patch.object(terrain,'inspect_apk',side_effect=RuntimeError('stop before process')) as checked:
            with self.assertRaisesRegex(RuntimeError,'stop before process'):terrain.run(self.base,self.root,[],False,1,types.SimpleNamespace())
        checked.assert_called_once_with(self.base,progress=None,consumers=('terrain',),expected_build=421629)

if __name__=='__main__':unittest.main()
