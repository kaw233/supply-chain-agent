"""Generic checks shipped with every generated business application. No external calls."""
import json,tempfile,unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.engine import validate_pack,evaluate
from app.processing import process
from app.runtime import Runtime
class GeneratedApplicationTests(unittest.TestCase):
    def setUp(self):self.packs=[json.loads(p.read_text(encoding='utf-8')) for p in (ROOT/'packs').glob('*.json')]
    def test_contract_and_fixed_fixtures(self):
        self.assertTrue(self.packs)
        for pack in self.packs:self.assertTrue(validate_pack(pack)['ok'])
    def test_processing_and_unique_ids(self):
        for pack in self.packs:
            rows=process(pack,pack.get('demo_records',[]));ids=[str(r['id']) for r in rows];self.assertEqual(len(ids),len(set(ids)))
    def test_external_actions_stay_skills(self):
        for p in self.packs:self.assertTrue(all(a.get('executor','hermes_skill')=='hermes_skill' for a in p.get('actions',{}).values()))
    def test_clean_database_and_repeated_cases(self):
        with tempfile.TemporaryDirectory() as t:
            rt=Runtime(Path(t)/'test.sqlite3',ROOT/'packs')
            for p in self.packs:
                before=len(rt.cases(p['id']));rt.run(p['id']);rt.run(p['id']);self.assertEqual(before,len(rt.cases(p['id'])))
    def test_snapshot_replay_without_external_actions(self):
        with tempfile.TemporaryDirectory() as t:
            rt=Runtime(Path(t)/'test.sqlite3',ROOT/'packs')
            for p in self.packs:self.assertTrue(rt.replay(rt.run(p['id'])['id'])['ok'])
if __name__=='__main__':unittest.main()
