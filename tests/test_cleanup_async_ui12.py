"""Synthetic async cleanup DOM/protocol regressions; no accounts or real cleanup."""
from pathlib import Path
import json
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
NODE=ROOT/'runtime/node/node.exe'

class CleanupAsyncUiTests(unittest.TestCase):
    def test_actual_javascript_async_cleanup_scenarios(self):
        r=subprocess.run([str(NODE),str(ROOT/'tests/cleanup_async12_scenarios.cjs')],cwd=ROOT,
                         capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(r.returncode,0,r.stderr[-10000:])
        result=json.loads(r.stdout);self.assertEqual(result['failed'],0);self.assertEqual(result['passed'],28)

    def test_changed_javascript_parses(self):
        for name in ('reset_cleanup.js','desktop.js','wizard.js'):
            with self.subTest(name=name):
                r=subprocess.run([str(NODE),'--check',str(ROOT/'app/web'/name)],capture_output=True,text=True,encoding='utf-8',timeout=20)
                self.assertEqual(r.returncode,0,r.stderr[-5000:])

if __name__=='__main__':unittest.main()
