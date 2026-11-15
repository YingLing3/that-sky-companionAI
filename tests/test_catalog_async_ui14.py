"""Actual desktop and wizard presentation model checks for background validation."""
from pathlib import Path
import json
import os
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]

class CatalogAsyncUiTests(unittest.TestCase):
    def test_actual_desktop_and_wizard_checking_states(self):
        node=os.environ.get('XG_TEST_NODE') or str(ROOT/'runtime/node/node.exe')
        result=subprocess.run([node,str(ROOT/'tests/catalog_async_ui14_scenarios.cjs')],
            capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr[-4000:])
        doc=json.loads(result.stdout)
        self.assertEqual(doc,{'passed':10,'failed':0})

if __name__=='__main__':unittest.main()
