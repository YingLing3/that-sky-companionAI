"""Offline bundled Node integration: synthetic geometry/paths, no account/config access."""
from __future__ import annotations
import argparse
import ast
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'app'
if str(APP) not in sys.path:sys.path.insert(0,str(APP))
import xg_paths
ALIAS='_node_runtime12_skyterrain'
if ALIAS not in sys.modules:
    package=types.ModuleType(ALIAS);package.__path__=[str(APP/'src/skyterrain')]
    sys.modules[ALIAS]=package
backend=importlib.import_module(ALIAS+'.backend')
bound=importlib.import_module(ALIAS+'.bound_factory')
decoder=importlib.import_module(ALIAS+'.formats._decoder')
BUNDLED=ROOT/'runtime/node/node.exe'
MODULE=APP/'engine/node_modules/meshoptimizer/meshopt_decoder.mjs'

def function(rel,name,namespace):
    source=(APP/rel).read_text(encoding='utf-8');tree=ast.parse(source)
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(APP/rel),'exec'),namespace)
    return namespace[name]

class NodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='node-runtime12-',dir=ROOT/'tests')
        self.addCleanup(self.temp.cleanup);self.folder=Path(self.temp.name)
        self.env=mock.patch.dict(os.environ,{'PATH':'','XG_NODE':'','SKY_AI_ROOT':str(self.folder/'unrelated-root'),'XG_DATA_DIR':str(self.folder/'isolated-data')})
        self.env.start();self.addCleanup(self.env.stop)
        self.tmp=mock.patch.object(tempfile,'tempdir',str(self.folder));self.tmp.start();self.addCleanup(self.tmp.stop)

    def test_default_transport_uses_bundle_with_empty_path(self):
        value=backend.JsonlWorkerTransport(self.folder)
        self.assertEqual(value.node.resolve(),BUNDLED.resolve());value.close()

    def test_bundle_precedes_old_node_in_path(self):
        old=self.folder/'old-node.exe';old.write_bytes(b'synthetic obsolete executable')
        with mock.patch.object(xg_paths.shutil,'which',return_value=str(old)):
            value=backend.JsonlWorkerTransport(self.folder,node_executable='node.exe')
            self.assertEqual(value.node.resolve(),BUNDLED.resolve());value.close()

    def test_unresolved_node_has_reextract_error(self):
        with mock.patch.object(Path,'is_file',return_value=False),mock.patch.object(xg_paths.shutil,'which',return_value=None):
            with self.assertRaises(xg_paths.NodeResolutionError) as got:
                backend.JsonlWorkerTransport(self.folder,node_executable='node')
        self.assertEqual(got.exception.code,'node_missing')
        self.assertIn('重新解压',got.exception.message)
        self.assertNotIn('安装 Node',got.exception.message)

    def test_invalid_explicit_env_does_not_fall_back_to_bundle(self):
        with mock.patch.dict(os.environ,{'XG_NODE':str(self.folder/'absent-node.exe')}):
            with self.assertRaises(xg_paths.NodeResolutionError) as got:
                backend.JsonlWorkerTransport(self.folder)
        self.assertEqual(got.exception.code,'node_invalid_override')

    def test_explicit_absolute_cold_transport_keeps_original_contract(self):
        explicit=self.folder/'node.exe';explicit.write_bytes(b'synthetic cold-only fixture')
        with mock.patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('cold explicit path must not run Node')):
            value=backend.JsonlWorkerTransport(self.folder,node_executable=str(explicit))
            self.assertEqual(value.node,explicit);self.assertIsNone(value._proc);value.close()

    def test_actual_spawn_requires_explicit_path_validation_and_exact_argv(self):
        explicit=self.folder/'node.exe';explicit.write_bytes(b'synthetic cold-only fixture')
        value=backend.JsonlWorkerTransport(self.folder,node_executable=str(explicit))
        proc=mock.Mock();proc.poll.return_value=0
        with mock.patch.object(xg_paths,'require_node_exe',return_value=str(BUNDLED)) as resolve,\
             mock.patch.object(backend.xg_exec,'popen',return_value=proc) as popen,\
             mock.patch.object(backend.threading,'Thread'):
            value._start();resolve.assert_called_once_with(explicit)
            args=popen.call_args.args[0]
            self.assertEqual(args,[str(BUNDLED),str(value._worker),'--assets',str(self.folder.resolve()),'--offline'])
            self.assertFalse(popen.call_args.kwargs['shell']);self.assertEqual(popen.call_args.kwargs['env'].get('PATH'),'')
            self.assertEqual(popen.call_args.kwargs['env']['XG_NODE'],str(BUNDLED))
        value._proc=None;value.close()

    def test_node_validation_error_reaches_caller_before_worker_spawn(self):
        value=backend.JsonlWorkerTransport(self.folder,node_executable=str(self.folder/'node.exe'))
        error=RuntimeError('synthetic Node validation failure')
        with mock.patch.object(xg_paths,'require_node_exe',side_effect=error),mock.patch.object(backend.xg_exec,'popen') as popen:
            with self.assertRaises(RuntimeError) as got:value._start()
        self.assertIs(got.exception,error);popen.assert_not_called();value.close()

    def test_placeholder_bound_factory_resolves_before_original_path_checks(self):
        with mock.patch.object(xg_paths,'require_node_exe',return_value=str(BUNDLED)) as resolve:
            factory=bound.BoundBackendFactory(self.folder,engine_root=APP/'engine',node_executable='node',foot_offset=(0,0,0))
        resolve.assert_called_once_with('node');self.assertEqual(factory.node,BUNDLED.resolve())
        self.assertEqual(factory.offset,(0.,0.,0.))

    def test_explicit_bound_factory_remains_cold(self):
        explicit=self.folder/'node.exe';explicit.write_bytes(b'synthetic cold-only fixture')
        with mock.patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('cold explicit factory must not probe')):
            factory=bound.BoundBackendFactory(self.folder,engine_root=APP/'engine',node_executable=str(explicit),foot_offset=(0,0,0))
        self.assertEqual(factory.node,explicit.resolve())

    def test_bound_relative_and_geometry_offset_gates_preserved(self):
        with self.assertRaisesRegex(bound.BindingError,'absolute_node_executable_required'):
            bound.BoundBackendFactory(self.folder,engine_root=APP/'engine',node_executable='relative/node.exe',foot_offset=(0,0,0))
        with self.assertRaisesRegex(bound.BindingError,'explicit_finite_foot_offset_required'):
            bound.BoundBackendFactory(self.folder,engine_root=APP/'engine',node_executable=str(BUNDLED),foot_offset=(0,float('nan'),0))

    def test_runtime_config_placeholders_expand_to_same_bundle(self):
        expand=function('bot/experimental_ground_entry.py','_expand_runtime_paths',{'xg_paths':xg_paths,'Path':Path,'os':os,'hashlib':hashlib})
        for placeholder in ['node','node.exe',' NODE ']:
            original={'nodeExecutable':placeholder,'unrelated':'preserved'}
            expanded=expand(original)
            self.assertEqual(Path(expanded['nodeExecutable']).resolve(),BUNDLED.resolve())
            self.assertEqual(original['nodeExecutable'],placeholder);self.assertEqual(expanded['unrelated'],'preserved')

    def test_runtime_config_explicit_path_and_missing_field_preserved(self):
        expand=function('bot/experimental_ground_entry.py','_expand_runtime_paths',{'xg_paths':xg_paths,'Path':Path,'os':os,'hashlib':hashlib})
        path=str(self.folder/'node.exe')
        with mock.patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('explicit remains cold')):
            self.assertEqual(expand({'nodeExecutable':path})['nodeExecutable'],path)
            self.assertEqual(expand({}),{})

    def test_decoder_initialization_does_not_require_node(self):
        with mock.patch.object(xg_paths,'require_node_exe',side_effect=AssertionError('lazy Node')):
            bridge=decoder.NodeMeshoptDecoder(None,MODULE)
        self.assertIsNone(bridge.node_executable);self.assertTrue(bridge.bridge.is_file())

    def test_decoder_resolves_only_when_compressed_payload_is_used(self):
        bridge=decoder.NodeMeshoptDecoder('node',MODULE)
        result=types.SimpleNamespace(returncode=0,stdout=b'v'*36)
        with mock.patch.object(xg_paths,'require_node_exe',return_value=str(BUNDLED)) as resolve,\
             mock.patch.object(decoder.xg_exec,'run',return_value=result) as run:
            self.assertEqual(bridge(1,36,b'synthetic'),b'v'*36)
        resolve.assert_called_once_with('node');self.assertEqual(run.call_args.args[0][0],str(BUNDLED))
        self.assertEqual(run.call_args.args[0][1],str(bridge.bridge));self.assertEqual(run.call_args.args[0][3:5],['1','36'])

    def test_decoder_size_gate_precedes_node_or_temporary_file(self):
        bridge=decoder.NodeMeshoptDecoder(None,MODULE)
        with mock.patch.object(xg_paths,'require_node_exe') as resolve, mock.patch.object(tempfile,'TemporaryDirectory') as tmp:
            with self.assertRaisesRegex(decoder.FormatError,'meshopt_bridge_size_limit'):bridge(1,32,b'x')
        resolve.assert_not_called();tmp.assert_not_called()

    def test_decoder_node_error_is_not_relabelled_as_meshopt_error(self):
        bridge=decoder.NodeMeshoptDecoder(None,MODULE);error=RuntimeError('synthetic missing Node')
        with mock.patch.object(xg_paths,'require_node_exe',side_effect=error),mock.patch.object(decoder.xg_exec,'run') as run:
            with self.assertRaises(RuntimeError) as got:bridge(1,36,b'x')
        self.assertIs(got.exception,error);run.assert_not_called()

    def test_cli_resolves_default_and_preserves_explicit_selection(self):
        build=mock.Mock(return_value={'ok':True})
        main=function('src/skyterrain/cli.py','main',{'argparse':argparse,'Path':Path,'json':json,'xg_paths':xg_paths,'build_map':build,'__doc__':'test','__file__':str(APP/'src/skyterrain/cli.py')})
        with contextlib.redirect_stdout(io.StringIO()),mock.patch.object(xg_paths,'require_node_exe',side_effect=lambda value: str(BUNDLED) if value is None else value) as resolve:
            main(['--apk','synthetic.apk','--map','Synthetic','--out',str(self.folder)])
            self.assertEqual(build.call_args.kwargs['node'],str(BUNDLED))
            explicit=str(self.folder/'node.exe');main(['--apk','synthetic.apk','--map','Synthetic','--out',str(self.folder),'--node',explicit])
            self.assertEqual(build.call_args.kwargs['node'],explicit)
        self.assertEqual(resolve.call_args_list,[mock.call(None),mock.call(explicit)])

    def test_cli_missing_node_is_parser_error_before_build(self):
        build=mock.Mock();output=io.StringIO()
        main=function('src/skyterrain/cli.py','main',{'argparse':argparse,'Path':Path,'json':json,'xg_paths':xg_paths,'build_map':build,'__doc__':'test','__file__':str(APP/'src/skyterrain/cli.py')})
        with mock.patch.object(Path,'is_file',return_value=False),mock.patch.object(xg_paths.shutil,'which',return_value=None),contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as got:main(['--apk','synthetic.apk','--map','Synthetic','--out',str(self.folder)])
        self.assertEqual(got.exception.code,2);self.assertIn('node_missing',output.getvalue())
        self.assertNotIn('Traceback',output.getvalue());build.assert_not_called()

    def test_build_map_resolves_before_any_output_or_archive_read(self):
        import re
        build=function('src/skyterrain/cli.py','build_map',{'re':re,'Path':Path,'os':os,'xg_paths':xg_paths})
        out=self.folder/'not-created';error=RuntimeError('synthetic unavailable Node')
        with mock.patch.object(xg_paths,'require_node_exe',side_effect=error) as resolve:
            with self.assertRaises(RuntimeError) as got:build('nonexistent.apk','Synthetic',out,node=None,meshopt=MODULE)
            self.assertIs(got.exception,error);resolve.assert_called_once_with(None)
        self.assertFalse(out.exists())

    def test_formats_cli_deferred_default_and_explicit_node(self):
        value=types.SimpleNamespace(source='synthetic',version=1,positions=[],triangles=[],geometry_kind='synthetic',coordinate_space='local',chunks=[],metadata={})
        construct=mock.Mock(return_value=object())
        main=function('src/skyterrain/formats/__main__.py','main',{'argparse':argparse,'Path':Path,'json':json,'NodeMeshoptDecoder':construct,'read_mesh':mock.Mock(return_value=value),'read_meshes':mock.Mock(return_value=value)})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['inspect','synthetic.meshes','--decoder-module',str(MODULE)]),0)
            self.assertIsNone(construct.call_args.args[0])
            explicit=str(self.folder/'node.exe');main(['inspect','synthetic.meshes','--decoder-module',str(MODULE),'--node',explicit])
            self.assertEqual(construct.call_args.args[0],explicit)

    def test_real_staged_worker_ping_with_empty_path(self):
        value=backend.JsonlWorkerTransport(self.folder,worker_profile=backend.STAGED_PROFILE)
        try:
            reply=value.request('ping')
            self.assertEqual(reply['status'],'ready');self.assertEqual(reply['protocol'],1)
            self.assertEqual(reply['dynamicCollidersProtocol'],1);self.assertEqual(value.node.resolve(),BUNDLED.resolve())
        finally:value.close()

    def test_meshopt_bridge_is_existing_nested_protocol_copy(self):
        src=APP/'xg_gamedata/terrain/pipeline/src/skyterrain/formats/meshopt_bridge.mjs'
        dst=APP/'src/skyterrain/formats/meshopt_bridge.mjs'
        self.assertEqual(dst.read_bytes(),src.read_bytes())

    def test_real_meshopt_synthetic_roundtrip_uses_bundled_node(self):
        encoder=(APP/'engine/node_modules/meshoptimizer/meshopt_encoder.js').as_uri()
        code='const m=await import('+json.dumps(encoder)+');const e=m.MeshoptEncoder||m.default?.MeshoptEncoder||m.default;await e.ready;const data=Uint8Array.from({length:144},(_,i)=>(i*7)%256);process.stdout.write(Buffer.from(e.encodeVertexBuffer(data,4,36)));'
        encoded=decoder.xg_exec.run([xg_paths.require_node_exe(),'--input-type=module','-e',code],capture_output=True,timeout=20,check=False)
        self.assertEqual(encoded.returncode,0,encoded.stderr.decode('utf-8',errors='replace'))
        bridge=decoder.NodeMeshoptDecoder(None,MODULE)
        self.assertEqual(bridge(4,36,encoded.stdout),bytes((i*7)%256 for i in range(144)))

    def test_real_meshopt_invalid_data_fails_explicitly(self):
        bridge=decoder.NodeMeshoptDecoder(None,MODULE)
        with self.assertRaisesRegex(decoder.FormatError,'node_meshopt_decode_failed'):
            bridge(1,36,b'not valid meshopt data')

if __name__=='__main__':unittest.main(verbosity=2)
