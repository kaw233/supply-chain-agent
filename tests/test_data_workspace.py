"""Actual SQLite/processing/HTTP data workspace tests; no model claims."""
import base64,copy,importlib.util,json,sys,tempfile,threading,time,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.runtime import Runtime
from app.engine import DomainError,validate_pack
from app.store import dump
from app.data_workspace import cursor_decode

class WorkspaceTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);(self.root/'packs').mkdir();self.pack=json.loads((ROOT/'examples/delivery.json').read_text(encoding='utf-8'));self.pack['demo_records']=self.pack['demo_records'][:12]
  (self.root/'packs/scene.json').write_text(json.dumps(self.pack,ensure_ascii=False),encoding='utf-8');self.rt=Runtime(self.root/'runtime.db',self.root/'packs');self.dw=self.rt.workspace;self.sid=self.pack['id'];self.snap=self.dw.resolve(self.sid)['id']
 def tearDown(self):self.temp.cleanup()
 def test_counts_drill_exact_for_every_severity(self):
  s=self.dw.summary(self.sid,self.snap)
  for severity,count in s['summary']['counts'].items():
   p=self.dw.rows(self.sid,self.snap,'results',{'severity':severity,'limit':2});self.assertEqual(p['total'],count);self.assertTrue(all(x['data']['severity']==severity for x in p['items']))
 def test_all_saved_tables_and_columns_present(self):
  tables=self.dw.table_list(self.sid,self.snap)['tables'];self.assertEqual({t['id'] for t in tables},{'source','processed','mapped','results','cases'});source=next(t for t in tables if t['id']=='source');self.assertEqual(source['row_count'],12);self.assertTrue(set(self.pack['demo_records'][0])<={x['key'] for x in source['columns']})
 def test_cursor_pages_are_complete_no_duplicate(self):
  seen=[];q={'limit':3}
  while True:
   p=self.dw.rows(self.sid,self.snap,'source',q);seen.extend(x['key'] for x in p['items'])
   if not p['next_cursor']:break
   q['cursor']=p['next_cursor']
  self.assertEqual(len(seen),12);self.assertEqual(len(set(seen)),12)
 def test_cursor_cannot_be_reused_for_other_filter(self):
  c=self.dw.rows(self.sid,self.snap,'source',{'limit':2})['next_cursor']
  with self.assertRaises(DomainError):self.dw.rows(self.sid,self.snap,'source',{'limit':2,'cursor':c,'owner':'different'})
 def test_historical_cursor_does_not_mix_new_data(self):
  first=self.dw.rows(self.sid,self.snap,'source',{'limit':2});rows=copy.deepcopy(self.pack['demo_records']);rows[0]['name']='NEW-LIVE';self.rt.ingest(self.sid,rows,'replace')
  old=self.dw.rows(self.sid,self.snap,'source',{'limit':2,'cursor':first['next_cursor']});self.assertEqual(old['snapshot']['id'],self.snap);self.assertFalse(any(x['data']['name']=='NEW-LIVE' for x in self.dw.rows(self.sid,self.snap,'source',{'limit':100})['items']))
 def test_frozen_rules_after_live_config_update(self):
  old=self.dw.rules(self.sid,self.snap);p=copy.deepcopy(self.rt.pack(self.sid));p['parameters']['critical_days']=99
  with self.rt.store.db() as c:c.execute('UPDATE scenarios SET config=? WHERE id=?',(dump(p),self.sid))
  new=self.rt.run(self.sid);self.assertNotEqual(self.dw.resolve(self.sid,new['id'])['rule_hash'],old['rule_hash']);self.assertNotEqual(old['effective_parameters']['critical_days'],99);self.assertEqual(self.dw.rules(self.sid,self.snap),old)
 def test_replay_frozen_parameters_and_input(self):
  rows=copy.deepcopy(self.pack['demo_records']);rows[0]['delay_days']=77;self.rt.ingest(self.sid,rows,'replace');self.assertTrue(self.rt.replay(self.snap)['ok'])
 def test_source_content_deduplicates(self):
  with self.rt.store.db() as c:
   total=c.execute("SELECT count(*) FROM dw_rows WHERE snapshot=? AND table_id IN ('source','processed','mapped')",(self.snap,)).fetchone()[0];hashes=c.execute("SELECT count(distinct hash) FROM dw_rows WHERE snapshot=? AND table_id IN ('source','processed','mapped')",(self.snap,)).fetchone()[0]
  self.assertEqual(total,36);self.assertEqual(hashes,12)
 def test_run_record_no_duplicate_full_snapshots(self):
  with self.rt.store.db() as c:body=json.loads(c.execute('SELECT body FROM runs WHERE id=?',(self.snap,)).fetchone()[0])
  self.assertIn('workspace_snapshot',body);self.assertNotIn('input',body);self.assertNotIn('results',body)
 def test_identity_lineage_references_exact_source(self):
  key=self.dw.rows(self.sid,self.snap,'results',{'limit':1})['items'][0]['key'];r=self.dw.lineage(self.sid,self.snap,'results',key);self.assertEqual(r['lineage_mode'],'identity-mapping');self.assertTrue(r['sources'][0]['resolved']);self.assertTrue(r['rule_trace'])
 def test_notes_append_do_not_mutate_old_data(self):
  key=self.dw.rows(self.sid,self.snap,'source',{'limit':1})['items'][0]['key'];before=self.dw.rows(self.sid,self.snap,'source',{'limit':100})
  for note in ('first','second'):self.dw.note(self.sid,{'snapshot':self.snap,'table':'source','key':key,'note':note})
  self.assertEqual(self.dw.rows(self.sid,self.snap,'source',{'limit':100}),before);self.assertEqual(len(self.dw.lineage(self.sid,self.snap,'source',key)['notes']),2)
 def test_note_unknown_row_rejected(self):
  with self.assertRaises(DomainError):self.dw.note(self.sid,{'snapshot':self.snap,'table':'source','key':'not-exists','note':'x'})
 def test_quick_prompt_does_not_submit_hermes(self):
  key=self.dw.rows(self.sid,self.snap,'results',{'limit':1})['items'][0]['key'];r=self.dw.prompt(self.sid,{'snapshot':self.snap,'table':'results','key':key,'action':'remind'});self.assertFalse(r['auto_send']);self.assertIn(self.snap,r['text']);self.assertIn('当前',r['text'])
 def test_case_shortcut_references_its_result_row(self):
  row=self.dw.rows(self.sid,self.snap,'cases',{'limit':1})['items'][0];line=self.dw.lineage(self.sid,self.snap,'cases',row['key']);self.assertEqual(line['sources'][0]['table'],'results');self.assertTrue(line['sources'][0]['resolved'])
 def test_history_mode_not_forced_latest(self):
  old=self.dw.summary(self.sid,self.snap);self.rt.run(self.sid);current=self.dw.summary(self.sid,self.snap);self.assertEqual(current['snapshot']['id'],self.snap);self.assertNotEqual(current['latest']['id'],self.snap);self.assertEqual(old['summary'],current['summary'])
 def test_versions_cursor_and_time_search(self):
  page=self.dw.versions(self.sid,{'limit':2});n=self.dw.versions(self.sid,{'limit':2,'cursor':page['next_cursor']});self.assertFalse({x['id'] for x in page['items']}&{x['id'] for x in n['items']})
  date=page['items'][0]['data_at'];search=self.dw.versions(self.sid,{'from':date.replace('+00:00','Z'),'to':date.replace('+00:00','Z')});self.assertTrue(search['items']);self.assertTrue(all(x['data_at']==date for x in search['items']))
 def test_version_from_other_scene_not_allowed(self):
  p=copy.deepcopy(self.pack);p['id']='other';self.rt.install(p)
  with self.assertRaises(DomainError):self.dw.rows('other',self.snap,'results',{})
 def test_latest_error_not_zero_risk_or_new_fake_snapshot(self):
  with self.rt.store.db() as c:self.rt.store.event(c,self.sid,'data.error',self.sid,{'error':'fixture unavailable source'})
  s=self.dw.summary(self.sid);self.assertEqual(s['snapshot']['id'],self.snap);self.assertEqual(s['summary']['total'],12);self.assertEqual(s['source_health']['status'],'last_update_failed')
 def test_cursor_does_not_allow_sql_injection(self):
  self.assertEqual(self.dw.rows(self.sid,self.snap,'results',{'owner':"x' OR 1=1 --"})['total'],0)
 def test_rule_page_preserves_long_exact_source(self):
  cfg=self.rt.pack(self.sid);cfg['rule_sources']=[{'file':'full-rules.md','text':'完整原文\n'*400}];cfg['rule_coverage']=[{'source':'full-rules.md#1','rules':[x['id'] for x in cfg['rules']],'status':'mapped'}]
  with self.rt.store.db() as c:c.execute('UPDATE scenarios SET config=? WHERE id=?',(dump(cfg),self.sid))
  r=self.rt.run(self.sid);view=self.dw.rules(self.sid,r['id']);self.assertEqual(view['source_documents'],cfg['rule_sources']);self.assertEqual(view['config']['rules'],cfg['rules'])
 def test_extra_columns_not_silently_discarded(self):
  rows=copy.deepcopy(self.pack['demo_records']);rows[-1]['only_last_row']='keep me';self.rt.ingest(self.sid,rows,'replace');table=self.dw.table_list(self.sid,'latest');self.assertIn('only_last_row',{c['key'] for t in table['tables'] if t['id']=='source' for c in t['columns']})
 def test_multisource_python_intermediate_and_exact_refs(self):
  cfg=self.rt.pack(self.sid);cfg['pipeline']=[{'id':'joined','label':'三表关联','kind':'python','input':'source','inputs':['source','supplier','promise'],'source':'''def transform(rows, parameters, tables):
 suppliers={r['id']:r for r in tables['supplier']}
 promises={r['id']:r for r in tables['promise']}
 out=[]
 for row in rows:
  x=dict(row);x['supplier_note']=suppliers['S1']['note'];x['commitment']=promises['P1']['date'];x['_source_refs']=[{'table':'source','key':row['id']},{'table':'supplier','key':'S1'},{'table':'promise','key':'P1'}];out.append(x)
 return {'rows':out,'tables':{'audit_stage':[{'id':r['id'],'name':r['name'],'_source_refs':[{'table':'source','key':r['id']}]} for r in rows]}}
'''}]
  with self.rt.store.db() as c:c.execute('UPDATE scenarios SET config=? WHERE id=?',(dump(cfg),self.sid))
  bundle=[{'id':'orders','label':'订单底表','rows':self.pack['demo_records'],'data_at':'2026-09-11T01:00:00Z'}, {'id':'supplier','rows':[{'id':'S1','note':'供应商资料'}],'data_at':'2026-09-10T13:00:00Z'}, {'id':'promise','rows':[{'id':'P1','date':'2026-09-12'}]}]
  self.rt.ingest_bundle(self.sid,{'tables':bundle,'main_table':'orders'});snap=self.dw.resolve(self.sid)['id'];tabs={t['id'] for t in self.dw.table_list(self.sid,snap)['tables']};self.assertTrue({'orders','supplier','promise','joined','audit_stage','source','mapped','results','cases'}<=tabs)
  key=self.pack['demo_records'][0]['id'];line=self.dw.lineage(self.sid,snap,'results',key);self.assertEqual(len(line['sources']),3);self.assertTrue(all(x['resolved'] for x in line['sources']));self.assertEqual(line['lineage_mode'],'explicit');self.assertTrue(self.rt.replay(snap)['ok'])
 def test_recursive_case_lineage_reaches_original_input(self):
  case=self.dw.rows(self.sid,self.snap,'cases',{'limit':1})['items'][0]
  result=self.dw.lineage(self.sid,self.snap,'cases',case['key'])
  self.assertTrue(result['source_chain']['complete']);self.assertTrue({'cases','results','source'}<={n['table'] for n in result['source_chain']['nodes']})
 def test_source_row_is_a_known_terminal_not_invented_predecessor(self):
  row=self.dw.rows(self.sid,self.snap,'source',{'limit':1})['items'][0];result=self.dw.lineage(self.sid,self.snap,'source',row['key'])
  self.assertTrue(result['source_chain']['complete']);self.assertEqual(len(result['source_chain']['nodes']),1);self.assertEqual(result['source_chain']['edges'],[])
 def test_cycle_in_explicit_lineage_is_reported_not_infinite(self):
  rows=copy.deepcopy(self.pack['demo_records']);rows[0]['_source_refs']=[{'table':'source','key':rows[0]['id']}]
  run=self.rt.ingest(self.sid,rows,'replace');snap=self.dw.resolve(self.sid)['id'];line=self.dw.lineage(self.sid,snap,'source',rows[0]['id'])
  self.assertFalse(line['source_chain']['complete']);self.assertTrue(any('循环' in w for w in line['limitations']))
 def test_opaque_python_does_not_invent_lineage(self):
  cfg=self.rt.pack(self.sid);cfg['processor']={'kind':'python','source':'def transform(rows,parameters):\n return [dict(x, calculated=1) for x in rows]'}
  with self.rt.store.db() as c:c.execute('UPDATE scenarios SET config=? WHERE id=?',(dump(cfg),self.sid))
  snap=self.rt.run(self.sid)['id'];key=self.pack['demo_records'][0]['id'];line=self.dw.lineage(self.sid,snap,'results',key);self.assertEqual(line['lineage_mode'],'table-level-only');self.assertTrue(line['limitations'])
 def test_failed_pipeline_does_not_publish_new_snapshot(self):
  cfg=self.rt.pack(self.sid);cfg['processor']={'kind':'sql','source':'select * from missing_table'}
  with self.rt.store.db() as c:c.execute('UPDATE scenarios SET config=? WHERE id=?',(dump(cfg),self.sid))
  with self.assertRaises(DomainError):self.rt.run(self.sid)
  self.assertEqual(self.dw.resolve(self.sid)['id'],self.snap)
 def test_inconsistent_source_ids_rejected(self):
  with self.assertRaises(DomainError):self.dw.set_sources(self.sid,[{'id':'a','rows':[]},{'id':'a','rows':[]}])
 def test_sql_multitable_query_and_refs(self):
  cfg=self.rt.pack(self.sid);cfg['pipeline']=[{'id':'join_sql','kind':'sql','source':"SELECT source.*, aux.memo FROM source LEFT JOIN aux ON aux.id='X'"}]
  with self.rt.store.db() as c:c.execute('UPDATE scenarios SET config=? WHERE id=?',(dump(cfg),self.sid))
  self.dw.set_sources(self.sid,[{'id':'aux','rows':[{'id':'X','memo':'extra'}]}]);snap=self.rt.run(self.sid)['id'];r=self.dw.rows(self.sid,snap,'results',{'limit':1})['items'][0];self.assertEqual(r['data']['memo'],'extra');self.assertEqual(self.dw.lineage(self.sid,snap,'results',r['key'])['lineage_mode'],'table-level-only')

class ScaleTests(unittest.TestCase):
 def test_thousands_version_index_and_paged_rows(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'packs').mkdir();cfg=json.loads((ROOT/'examples/delivery.json').read_text(encoding='utf-8'));cfg['demo_records']=cfg['demo_records'][:1];(root/'packs/scene.json').write_text(json.dumps(cfg),encoding='utf-8');rt=Runtime(root/'x.db',root/'packs');dw=rt.workspace
   t=time.monotonic()
   with rt.store.db() as c:
    for n in range(1200):
     stamp=(datetime(2026,8,1,tzinfo=timezone.utc)+timedelta(minutes=n)).isoformat();dw.capture(c,'SCALE-'+str(n),'delivery',{'created_at':stamp,'total':1,'counts':{'normal':1}},cfg,[{'id':'results','rows':[{'id':'stable','severity':'normal'}],'kind':'result'}])
   build=time.monotonic()-t;t=time.monotonic();page=dw.versions('delivery',{'limit':30});elapsed=time.monotonic()-t
   self.assertEqual(len(page['items']),30);self.assertTrue(page['next_cursor']);self.assertLess(elapsed,2)
   with rt.store.db() as c:
    plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN SELECT id FROM dw_versions WHERE scenario=? ORDER BY data_at DESC,seq DESC LIMIT 31',('delivery',)));table_count=c.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
   self.assertIn('dw_versions_date',plan);self.assertLess(table_count,40)
   print('MEASURED snapshot_scale',json.dumps({'versions':1200,'creation_seconds':round(build,3),'page_ms':round(elapsed*1000,3),'physical_tables':table_count,'query_plan':plan}))
