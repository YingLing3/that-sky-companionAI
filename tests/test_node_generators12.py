"""Bundled Node generator entry tests; mathematical data, no APK/accounts/network."""
from pathlib import Path
import json,os,subprocess,sys,tempfile,unittest

ROOT=Path(__file__).resolve().parents[1];APP=ROOT/'app';PIPE=APP/'xg_gamedata/terrain/pipeline'
BUNDLED=ROOT/'runtime/node/node.exe'

class GeneratorNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='node-generators12-',dir=ROOT/'tests');self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name).resolve();(self.work/'tmp').mkdir();(self.work/'data').mkdir()
        self.env=dict(os.environ,PATH='',PYTHONPATH='',PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8',
            TEMP=str(self.work/'tmp'),TMP=str(self.work/'tmp'),XG_DATA_DIR=str(self.work/'data'))
        self.env.pop('XG_NODE',None)
    def child(self,body,*,nested=True,env=None):
        prefix='\n'.join(['from pathlib import Path','import os,sys,json,runpy,types,io,struct,unittest',
            'from unittest.mock import patch',f'APP=Path({str(APP)!r})',f'PIPE=Path({str(PIPE)!r})',
            f'BUNDLED=Path({str(BUNDLED)!r}).resolve()',"WORK=Path.cwd()",
            "sys.path[:0]=[str(APP),str(PIPE/'src' if "+repr(nested)+" else APP/'src'),str(APP.parent/'tests')]",
            'import xg_paths'])+'\n'
        result=subprocess.run([sys.executable,'-B','-X','utf8','-c',prefix+body],cwd=self.work,env=env or self.env,
            capture_output=True,text=True,encoding='utf-8',timeout=50)
        self.assertEqual(result.returncode,0,result.stdout[-4000:]+result.stderr[-6000:]);return result
    def direct(self,relative,args):
        return subprocess.run([sys.executable,'-B','-X','utf8',str(APP/relative),*args],cwd=self.work,env=self.env,
            capture_output=True,text=True,encoding='utf-8',timeout=35)

    def test_ensure_pipeline_empty_path_uses_real_bundle(self):
        self.child("from xg_gamedata import terrain\nassert Path(terrain.ensure_pipeline())==BUNDLED")
    def test_ensure_pipeline_ignores_old_node_on_path(self):
        old=self.work/'old-node';old.mkdir();(old/'node.exe').write_bytes(b'non-executable-old-node-fixture')
        env=dict(self.env,PATH=str(old))
        self.child("from xg_gamedata import terrain\nassert Path(terrain.ensure_pipeline())==BUNDLED",env=env)
    def test_ensure_pipeline_install_root_independent_of_runtime_data_and_cwd(self):
        env=dict(self.env,SKY_AI_ROOT=str(self.work/'other-root'),XG_DATA_DIR=str(self.work/'data'))
        self.child("from xg_gamedata import terrain\nassert Path(terrain.ensure_pipeline())==BUNDLED",env=env)
    def test_invalid_explicit_environment_reports_specific_error(self):
        env=dict(self.env,XG_NODE=str(self.work/'missing-node.exe'))
        self.child("from xg_gamedata import terrain\nfrom xg_gamedata.common import GameDataError\ntry:terrain.ensure_pipeline()\nexcept GameDataError as e:assert e.code=='node_invalid_override'\nelse:raise AssertionError('missing override accepted')",env=env)
    def test_missing_installation_message_reextract_not_install_node(self):
        self.child("""from xg_gamedata import terrain
with patch.object(xg_paths,'INSTALL_ROOT',WORK/'absent'),patch.object(xg_paths,'ROOT',WORK/'absent'),patch.object(xg_paths,'RELEASE',WORK/'absent/release'):
    try:terrain.ensure_pipeline()
    except Exception as e:
        assert e.code=='node_missing';assert '解压' in e.message;assert '装好' not in e.message
    else:raise AssertionError('missing Node accepted')
""")
    def test_top_terrain_child_receives_exact_absolute_node_and_env(self):
        self.child("""from xg_gamedata import terrain
from xg_gamedata.common import Progress,game_dir
seen={}
def popen(cmd,**kw):
    seen.update(cmd=cmd,env=kw['env'])
    target=Path(cmd[cmd.index('--catalog')+1]);target.write_text(json.dumps({'entries':[{'scene_name':'MathMap'}]}))
    return types.SimpleNamespace(stdout=iter(()),wait=lambda:0)
with patch.object(terrain,'inspect_apk'),patch.object(xg_paths,'short_path',side_effect=lambda p:str(p)),patch.object(terrain.xg_exec,'popen',side_effect=popen):
    r=terrain.run(WORK/'not-used.apk',WORK/'data',['MathMap'],False,1,Progress('terrain',0,stream=io.StringIO()))
assert r['ok'];assert Path(seen['cmd'][seen['cmd'].index('--node')+1])==BUNDLED
assert Path(seen['env']['XG_NODE'])==BUNDLED
""")
    def test_terrain_cli_entry_help_from_foreign_cwd_without_pythonpath(self):
        r=self.direct('xg_gamedata/terrain/terrain_build.py',['--help']);self.assertEqual(r.returncode,0,r.stderr);self.assertIn('--node',r.stdout)
    def test_pipeline_cli_direct_help_bootstraps_app_without_pythonpath(self):
        r=self.direct('xg_gamedata/terrain/pipeline/src/skyterrain/cli.py',['--help']);self.assertEqual(r.returncode,0,r.stderr)
    def test_formats_cli_direct_help_bootstraps_app_without_pythonpath(self):
        r=self.direct('xg_gamedata/terrain/pipeline/src/skyterrain/formats/__main__.py',['--help']);self.assertEqual(r.returncode,0,r.stderr)
    def test_all_maps_direct_help_does_not_require_node_flag(self):
        r=self.direct('xg_gamedata/terrain/pipeline/tools/build_all_maps.py',['--help']);self.assertEqual(r.returncode,0,r.stderr);self.assertIn('[--node NODE]',r.stdout)
    def test_navigation_direct_help_does_not_require_node_flag(self):
        r=self.direct('xg_gamedata/terrain/pipeline/tools/build_beamo_navigation.py',['--help']);self.assertEqual(r.returncode,0,r.stderr);self.assertIn('[--node NODE]',r.stdout)
    def test_pipeline_cli_default_parser_sends_bundled_absolute(self):
        self.child("""from skyterrain import cli
seen={}
def build(*a,**kw):seen.update(kw);return {'fixture':True}
with patch.object(cli,'build_map',side_effect=build):cli.main(['--apk','unused.apk','--map','Math','--out','out'])
assert Path(seen['node'])==BUNDLED
""")
    def test_pipeline_cli_explicit_path_overrides_environment(self):
        env=dict(self.env,XG_NODE=str(self.work/'missing-node.exe'))
        self.child("""from skyterrain import cli
seen={}
with patch.object(cli,'build_map',side_effect=lambda *a,**kw:seen.update(kw) or {}):
    cli.main(['--apk','unused.apk','--map','Math','--out','out','--node',str(BUNDLED)])
assert Path(seen['node'])==BUNDLED
""",env=env)
    def test_pipeline_cli_bare_placeholder_uses_bundle(self):
        self.child("""from skyterrain import cli
seen={}
with patch.object(cli,'build_map',side_effect=lambda *a,**kw:seen.update(kw) or {}):
    cli.main(['--apk','unused.apk','--map','Math','--out','out','--node','node.exe'])
assert Path(seen['node'])==BUNDLED
""")
    def test_terrain_builder_parser_resolves_before_heavy_stages(self):
        self.child("""g=runpy.run_path(str(APP/'xg_gamedata/terrain/terrain_build.py'));g=g['main'].__globals__
class Stop(BaseException):pass
seen={}
def base(*a):seen['node']=a[4];raise Stop()
monitor=types.SimpleNamespace(start=lambda:None,stop=lambda:None)
with patch.dict(g,apk_inventory=lambda apk:(['Math'],{'sha256':'a'*64,'bytes':1},{}),base_stage=base,MemoryMonitor=lambda:monitor):
    try:g['main'](['--apk','unused.apk','--out',str(WORK/'out')])
    except Stop:pass
assert Path(seen['node'])==BUNDLED
""")
    def test_all_maps_parser_default_resolves_before_inventory(self):
        self.child("""g=runpy.run_path(str(PIPE/'tools/build_all_maps.py'));g=g['main'].__globals__
class Stop(BaseException):pass
calls=[];real=xg_paths.require_node_exe
def resolve(value=None):calls.append(real(value));return calls[-1]
with patch.object(xg_paths,'require_node_exe',side_effect=resolve),patch.dict(g,inventory=lambda *a,**kw:(_ for _ in ()).throw(Stop())):
    try:g['main'](['--apk','unused.apk','--out',str(WORK/'out')])
    except Stop:pass
assert calls and Path(calls[0])==BUNDLED
""")
    def test_navigation_parser_default_passes_resolved_path(self):
        self.child("""g=runpy.run_path(str(PIPE/'tools/build_beamo_navigation.py'));g=g['main'].__globals__;seen={}
def build(*a,**kw):seen.update(kw);return {'rows':[{'status':'reuse_base'}]}
with patch.dict(g,build_batch=build):assert g['main'](['--world-summary','unused','--base-catalog','unused','--out','out'])==0
assert Path(seen['node'])==BUNDLED
""")
    def test_navigation_identity_uses_central_resolver_without_which(self):
        self.child("""g=runpy.run_path(str(PIPE/'tools/build_beamo_navigation.py'));node,identity=g['builder_identity']('node')
assert Path(node)==BUNDLED;assert len(identity['node_sha256'])==64
""")
    def test_decoder_constructor_and_size_rejection_do_not_resolve_node(self):
        self.child("""from skyterrain.formats import NodeMeshoptDecoder
module=WORK/'math.mjs';module.write_text('export const MeshoptDecoder={};')
with patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('early Node lookup')):
    decoder=NodeMeshoptDecoder(None,module)
    try:decoder(-1,36,b'')
    except Exception as e:assert str(e)=='meshopt_bridge_size_limit'
    else:raise AssertionError('negative count accepted')
""")
    def test_real_nested_meshopt_bridge_with_math_module_empty_path(self):
        self.child("""from skyterrain.formats import NodeMeshoptDecoder
module=WORK/'math.mjs';module.write_text('export const MeshoptDecoder={ready:Promise.resolve(),decodeVertexBuffer(out){out.fill(7);}};')
decoder=NodeMeshoptDecoder(None,module)
assert decoder(1,36,b'')==bytes([7])*36
""")
    def test_backend_default_uses_bundle_but_absolute_cold_path_does_not_probe(self):
        self.child("""from skyterrain.backend import JsonlWorkerTransport
assert JsonlWorkerTransport(WORK).node==BUNDLED
fake=WORK/'node.exe';fake.write_bytes(b'cold-fixture')
with patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('cold probe')):
    assert JsonlWorkerTransport(WORK,node_executable=str(fake)).node==fake
""")
    def test_backend_actual_start_preserves_node_error_not_generic_worker_failure(self):
        self.child("""from skyterrain.backend import JsonlWorkerTransport
worker=JsonlWorkerTransport(WORK,node_executable=str(BUNDLED))
with patch.object(xg_paths,'require_node_exe',side_effect=xg_paths.NodeResolutionError('node_unusable','fixture')):
    try:worker._start()
    except xg_paths.NodeResolutionError as e:assert e.code=='node_unusable'
    else:raise AssertionError('missing Node error')
""")
    def test_bound_factory_placeholder_and_absolute_cold_contract(self):
        self.child("""from skyterrain import bound_factory
BoundBackendFactory=bound_factory.BoundBackendFactory
# The nested generator ships navigation builders, not a runtime worker. This
# cold-only test supplies a mathematical fixed worker; production pins stay intact.
engine=WORK/'engine';engine.mkdir();worker=engine/'worker.mjs';worker.write_text('// cold fixture')
with patch.object(bound_factory,'WORKER',worker):
    factory=BoundBackendFactory(WORK,engine_root=engine,node_executable='node',foot_offset=(0,0,0))
    assert factory.node==BUNDLED
    fake=WORK/'node.exe';fake.write_bytes(b'cold-fixture')
    with patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('cold probe')):
        factory=BoundBackendFactory(WORK,engine_root=engine,node_executable=fake,foot_offset=(0,0,0))
    assert factory.node==fake
""")
    def test_props_generation_publication_remains_python_only_empty_path(self):
        self.child("""from test_prop_generation8 import GenerationTests
suite=unittest.TestSuite([GenerationTests('test_synthetic_full_pipeline_publishes_only_runtime'),GenerationTests('test_clean_geometry_preserves_triangle_winding')])
with patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('props must not require Node')):
    result=unittest.TextTestRunner().run(suite)
assert result.wasSuccessful()
""",nested=False)
    def test_catalog_wire_and_placement_remain_python_only_empty_path(self):
        self.child("""from xg_gamedata import catalog
with patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('catalog must not require Node')):
    wire=b'123';cache=catalog._cache_from_wire(wire);assert cache[14:17]==wire
    assert struct.unpack_from('<H',cache,0x200E)[0]==3
    row,encoded=catalog._placement({'placements':{'Math':{'scene':'Math','map_id':1,'wire_position':[1,2,3]}}},'Math',1)
    assert json.loads(encoded)==row
""",nested=False)
    def test_missing_node_direct_cli_does_not_create_output(self):
        self.env['XG_NODE']=str(self.work/'absent-node.exe')
        r=self.direct('xg_gamedata/terrain/terrain_build.py',['--apk','unused.apk','--out',str(self.work/'out')])
        self.assertNotEqual(r.returncode,0);self.assertIn('node_invalid_override',r.stderr);self.assertFalse((self.work/'out').exists())

    def test_stage_functions_resolve_bare_node_before_child_environment(self):
        self.child("""g=runpy.run_path(str(APP/'xg_gamedata/terrain/terrain_build.py'));g=g['main'].__globals__
class Stop(BaseException):pass
seen=[]
def run(cmd,log,line,env):seen.append((cmd,env));raise Stop()
summary={'stages':{},'maps':{}};progress=types.SimpleNamespace(total=1,update=lambda *a,**k:None)
with patch.dict(g,run_stream=run):
    try:g['base_stage'](WORK/'unused.apk',WORK/'base-work',['Math'],['Math'],'node',False,90,summary,progress)
    except Stop:pass
    try:g['nav_stage'](WORK/'nav-work',WORK/'world.json',WORK/'base.json',['Math'],['Math'],'node.exe',1,summary,progress)
    except Stop:pass
assert len(seen)==2
for cmd,env in seen:
    assert Path(cmd[cmd.index('--node')+1])==BUNDLED;assert Path(env['XG_NODE'])==BUNDLED
""")
    def test_navigation_worker_direct_api_resolves_bare_node_and_env(self):
        self.child("""g=runpy.run_path(str(PIPE/'tools/build_beamo_navigation.py'));g=g['run_worker'].__globals__
attempt=WORK/'attempt';attempt.mkdir();seen={}
def run(cmd,**kw):seen.update(cmd=cmd,env=kw['env']);return types.SimpleNamespace(returncode=0)
with patch.object(g['xg_exec'],'run',side_effect=run):result=g['run_worker']('node',str(WORK/'asset.json'),attempt)
assert result['exit_code']==0;assert Path(seen['cmd'][0])==BUNDLED;assert Path(seen['env']['XG_NODE'])==BUNDLED
""")

if __name__=='__main__':unittest.main()
