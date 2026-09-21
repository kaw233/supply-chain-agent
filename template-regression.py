import unittest,tempfile,threading,time,json,copy,sys,io
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT))
from server import create_server
from app.agent import AgentService,TERMINAL
from app.engine import DomainError,validate_pack
from app.processing import process
from app.remote import WebUI,NativeAPI,event_stream
from app import bridge
from protocol_peer import Peer

class WorkbenchTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.peer=Peer()
 @classmethod
 def tearDownClass(cls):cls.peer.close()
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite3';self.s=create_server(port=0,db=self.path,poll_interval=.05);self.rt=self.s.runtime;self.a=self.s.agent;self.base=self.rt.base_url
  self.thread=threading.Thread(target=self.s.serve_forever,daemon=True);self.thread.start();self.peer.fail_status=False;self.peer.drop_stream=False;self.peer.lost_submit=False
 def tearDown(self):
  self.a.shutdown();self.s.shutdown();self.s.server_close();time.sleep(.15)
  try:self.tmp.cleanup()
  except OSError:pass
 def cfg(self,mode='webui'):
  self.rt.store.update_settings({'agent':{'mode':mode,'webui_url':self.peer.base,'webui_password':'fixture-password','base_url':self.peer.base+'/v1','api_key':'fixture-key','timeout':2}})
 def wait(self,tid,predicate,timeout=5):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   t=self.a.task(tid)
   if predicate(t):return t
   time.sleep(.035)
  self.fail('Timed out: '+json.dumps(self.a.task(tid),ensure_ascii=False))
 def submit(self,msg='HELLO',mode='webui',**kw):self.cfg(mode);return self.a.submit({'scenario_id':'materials','message':msg,**kw})
 def req(self,path,body=None,method=None):
  try:
   with urlopen(Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json'}),timeout=12) as r:return r.status,json.loads(r.read())
  except HTTPError as ex:return ex.code,json.loads(ex.read())
 def test_01_default_real_without_fallback(self):
  self.assertEqual(self.rt.store.settings()['agent']['mode'],'webui')
  with self.assertRaises(DomainError):self.a.submit({'message':'你好'})
  self.assertEqual(len(self.a.list_tasks()),0)
 def test_02_templates_and_case_dedupe(self):
  self.assertEqual(sum(s['metrics']['total'] for s in self.rt.list_scenarios()),108)
  ids={c['id'] for c in self.rt.cases('materials')};self.rt.run('materials');self.assertEqual(ids,{c['id'] for c in self.rt.cases('materials')})
 def test_03_no_studio_routes(self):
  for route in ['/api/delivery/export','/api/packages/import','/api/scenarios/materials/simulate']:
   self.assertEqual(self.req(route,{'kind':'risk'})[0],404)
  self.assertFalse(self.req('/api/app')[1]['maker_enabled'])
 def test_04_webui_login_prefix_csrf_and_output(self):
  t=self.submit();t=self.wait(t['id'],lambda t:t['status']=='completed');self.assertIn('本地协议测试',t['result']['reply']);self.assertTrue(t['handle']['message_count']==0)
  sent=[c for c in self.peer.calls if c['path']=='/api/chat/start'][-1];self.assertEqual(sent['headers']['X-Hermes-Csrf-Token'],'csrf-fixture')
 def test_05_native_runs_status(self):
  t=self.submit(mode='hermes');t=self.wait(t['id'],lambda t:t['status']=='completed');self.assertTrue(t['handle']['run_id']);self.assertIn('本地协议测试',t['result']['reply'])
 def test_06_read_refresh_does_not_submit(self):
  t=self.submit('HOLD');self.wait(t['id'],lambda x:bool(x['handle']));n=self.peer.starts
  for _ in range(4):self.req('/api/agent/sessions/'+t['session_id']+'/snapshot');self.req('/api/agent/tasks/'+t['id'])
  self.assertEqual(self.peer.starts,n);self.assertFalse(self.a.task(t['id'])['stop_requested'])
  self.a.stop(t['id']);self.wait(t['id'],lambda t:t['status']=='cancelled')
 def test_07_stop_preserves_data_and_automation(self):
  self.rt.store.update_settings({'automation':{'enabled':True}});t=self.submit('HOLD');self.wait(t['id'],lambda x:bool(x['handle']));self.a.stop(t['id']);self.wait(t['id'],lambda x:x['status']=='cancelled')
  self.assertTrue(self.rt.store.settings()['automation']['enabled']);self.assertEqual(self.rt.run('materials')['status'],'completed')
 def test_08_manual_continue_only(self):
  t=self.submit('HOLD');self.wait(t['id'],lambda x:bool(x['handle']));self.a.stop(t['id']);self.wait(t['id'],lambda x:x['status']=='cancelled');n=self.peer.starts;time.sleep(.15);self.assertEqual(n,self.peer.starts)
  with self.assertRaises(DomainError):self.a.continue_task(t['id'],{'message':'HELLO'})
  new=self.a.continue_task(t['id'],{'confirm':True,'message':'只整理结果 HELLO'});self.wait(new['id'],lambda x:x['status']=='completed');self.assertEqual(self.a.task(t['id'])['status'],'cancelled');self.assertEqual(new['continued_from'],t['id'])
 def test_09_same_session_single_run(self):
  t=self.submit('HOLD');self.wait(t['id'],lambda x:bool(x['handle']))
  with self.assertRaises(DomainError):self.a.submit({'scenario_id':'materials','session_id':t['session_id'],'message':'HELLO'})
  self.a.stop(t['id'])
 def test_10_native_approval_exact_request(self):
  t=self.submit('APPROVE',mode='hermes');t=self.wait(t['id'],lambda x:bool(x.get('card')));self.a.respond(t['id'],{'card_key':t['card']['key'],'choice':'once'});self.wait(t['id'],lambda x:x['status']=='completed')
  call=[c for c in self.peer.calls if c['path'].endswith('/approval')][-1];self.assertIn('request_id',call['body']);self.assertNotIn('approval_id',call['body'])
 def test_11_webui_approval_mirror_identity(self):
  t=self.submit('APPROVE');t=self.wait(t['id'],lambda x:bool(x.get('card')));self.a.respond(t['id'],{'card_key':t['card']['key'],'choice':'once'});self.wait(t['id'],lambda x:x['status']=='completed')
  c=[c for c in self.peer.calls if c['path']=='/api/approval/respond'][-1];self.assertIn('mirror_token',c['body']);self.assertIn('run_id',c['body'])
 def test_12_stale_approval_not_new_request(self):
  t=self.submit('APPROVE');t=self.wait(t['id'],lambda x:bool(x.get('card')));old=t['card']['key'];r=self.peer.runs[t['handle']['run_id']];r['approval']['approval_id']='another-request'
  with self.assertRaises(DomainError):self.a.respond(t['id'],{'card_key':old,'choice':'once'})
  self.assertIsNotNone(r['approval']);self.a.stop(t['id'])
 def test_13_refusal(self):
  t=self.submit('APPROVE');t=self.wait(t['id'],lambda x:bool(x.get('card')));self.a.respond(t['id'],{'card_key':t['card']['key'],'choice':'deny'});self.wait(t['id'],lambda x:x['status']=='failed')
 def test_14_missing_info(self):
  t=self.submit('CLARIFY');t=self.wait(t['id'],lambda x:x['status']=='waiting_input');self.assertEqual(t['card']['kind'],'input');self.a.respond(t['id'],{'card_key':t['card']['key'],'answer':'华东工厂'});self.wait(t['id'],lambda x:x['status']=='completed')
 def test_15_remote_elsewhere_stop(self):
  t=self.submit('HOLD');t=self.wait(t['id'],lambda x:bool(x['handle']));self.peer.finish(t['handle']['run_id'],'cancelled');self.wait(t['id'],lambda x:x['status']=='cancelled')
 def test_16_sse_tools_and_dedupe(self):
  t=self.submit('HOLD');t=self.wait(t['id'],lambda x:bool(x['activity']));self.assertEqual(t['activity'][0]['name'],'fixture_lookup')
  self.assertTrue(self.a.event(t['id'],'tool.start',{},'same'));self.assertFalse(self.a.event(t['id'],'tool.start',{},'same'));self.a.stop(t['id'])
 def test_17_missing_event_buffer_status_completes(self):
  self.peer.drop_stream=True;t=self.submit(mode='hermes');t=self.wait(t['id'],lambda x:x['status']=='completed');self.assertIn('本地协议测试',t['result']['reply'])
 def test_18_webui_missing_stream_recovers_final_history(self):
  self.peer.drop_stream=True;t=self.submit();t=self.wait(t['id'],lambda x:x['status']=='completed');self.assertIn('非真实 Hermes',t['result']['reply'])
 def test_19_restart_observation_only(self):
  t=self.submit('HOLD');t=self.wait(t['id'],lambda x:bool(x['handle']));n=self.peer.starts;self.a.shutdown();time.sleep(.1);self.a=AgentService(self.rt,poll_interval=.05);self.s.agent=self.a
  self.peer.finish(t['handle']['run_id']);self.wait(t['id'],lambda x:x['status']=='completed');self.assertEqual(n,self.peer.starts)
 def test_20_stopped_restart_not_resume(self):
  t=self.submit('HOLD');t=self.wait(t['id'],lambda x:bool(x['handle']));self.a.stop(t['id']);self.wait(t['id'],lambda x:x['status']=='cancelled');n=self.peer.starts;self.a.shutdown();self.a=AgentService(self.rt,poll_interval=.05);self.s.agent=self.a;time.sleep(.15);self.assertEqual(n,self.peer.starts);self.assertEqual(self.a.task(t['id'])['status'],'cancelled')
 def test_21_ambiguous_submission_not_retried(self):
  self.peer.lost_submit=True;t=self.submit('HOLD',mode='hermes');t=self.wait(t['id'],lambda x:x['status']=='unknown');n=self.peer.starts;time.sleep(.25);self.assertEqual(n,self.peer.starts)
  with self.assertRaises(DomainError):self.a.continue_task(t['id'],{'confirm':True,'message':'again'})
 def test_22_idempotent_submit(self):
  self.cfg();body={'scenario_id':'materials','message':'HOLD','request_id':'intent-one'};t=self.a.submit(body);t2=self.a.submit(body);self.assertEqual(t['id'],t2['id']);self.wait(t['id'],lambda x:bool(x['handle']));self.a.stop(t['id'])
 def test_23_auto_stop_does_not_pause_new_event(self):
  self.cfg();self.rt.store.update_settings({'automation':{'enabled':True,'message':'HOLD'}});rows=[self.rt.raw_records('materials')[0]]
  one=self.rt.ingest('materials',rows,event_id='batch-a')['automatic_task'];self.wait(one['id'],lambda x:bool(x['handle']));self.a.stop(one['id']);self.wait(one['id'],lambda x:x['status']=='cancelled')
  same=self.rt.ingest('materials',rows,event_id='batch-a')['automatic_task'];self.assertIsNone(same)
  two=self.rt.ingest('materials',rows,event_id='batch-b')['automatic_task'];self.assertNotEqual(one['id'],two['id']);self.assertNotEqual(one['session_id'],two['session_id']);self.a.stop(two['id'])
  self.rt.store.update_settings({'automation':{'enabled':False}});self.assertIsNone(self.rt.ingest('materials',rows,event_id='batch-c')['automatic_task'])
 def test_24_page_bridge_updates_without_reexecuting_tool(self):
  t=self.submit('BRIDGE_FILTER');t=self.wait(t['id'],lambda x:x['status']=='completed');self.assertEqual(t['outputs'][0]['command']['filters']['severity'],'high')
 def test_25_stopped_bridge_mutation_rejected(self):
  t=self.submit('HOLD');self.wait(t['id'],lambda x:bool(x['handle']));raw=self.a.raw_task(t['id']);self.a.stop(t['id'])
  with self.assertRaises(DomainError):bridge.invoke(self.a,t['id'],raw['_bridge_token'],{'operation':'rules.run','parameters':{},'request_id':'one'})
 def test_26_python_sql_processing_and_replay(self):
  for cfg in ({'kind':'sql','source':'SELECT * FROM source'}, {'kind':'python','source':'def transform(rows, parameters):\n    return rows'}):
   pack=self.rt.pack('materials');pack['processor']=cfg;d=self.rt.draft_config('materials',pack);self.assertTrue(d['validation']['ok']);out=self.rt.publish(d['id']);self.assertTrue(self.rt.replay(out['run']['id'])['ok'])
 def test_27_bad_python_and_acceptance_cannot_apply(self):
  p=self.rt.pack('materials');p['processor']={'kind':'python','source':'def broken('};d=self.rt.draft_config('materials',p);self.assertFalse(d['validation']['ok'])
  with self.assertRaises(DomainError):self.rt.publish(d['id'])
  p=self.rt.pack('materials');p['acceptance']=[]
  with self.assertRaises(DomainError):self.rt.draft_config('materials',p)
 def test_28_data_source_failure_retains_results(self):
  p=self.rt.pack('materials');p['data_source']={'kind':'file','location':'not-found-file.json'};self.rt.publish(self.rt.draft_config('materials',p)['id']);before=self.rt.rows('materials')
  with self.assertRaises(DomainError):self.rt.pull_source('materials')
  self.assertEqual(before,self.rt.rows('materials'));self.assertTrue(self.rt.dashboard('materials')['source_health']['error'])
 def test_29_data_missing_and_case_not_auto_closed(self):
  row=self.rt.raw_records('materials')[0];row['stock']=None;self.rt.ingest('materials',[row]);r=next(r for r in self.rt.rows('materials') if r['id']==row['id']);self.assertEqual(r['severity'],'unknown')
  with self.assertRaises(DomainError):self.rt.update_case(r['case_id'],{'status':'closed'})
 def test_30_action_revision_confirmation(self):
  cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','email.send',[cid]);old=a['draft_revision'];m=copy.deepcopy(a['messages']);m[0]['subject']='changed';self.rt.edit_action(a['id'],{'messages':m})
  self.cfg()
  with self.assertRaises(DomainError):self.rt.execute_action(a['id'],old)
 def test_31_skill_success_text_not_business_receipt(self):
  self.cfg();cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','email.send',[cid]);out=self.rt.execute_action(a['id'],a['draft_revision']);self.wait(out['agent_task_id'],lambda x:x['status']=='completed');self.assertEqual(self.rt.action(a['id'])['status'],'needs_review');self.assertNotEqual(self.rt.case_detail(cid)['status'],'closed')
 def test_32_public_hosts_allowed_not_only_intranet(self):
  self.assertEqual(WebUI({'webui_url':'https://example.org/hermes/ui'}).base,'https://example.org/hermes/ui');self.assertEqual(NativeAPI({'base_url':'https://api.example.org/prefix'}).base,'https://api.example.org/prefix/v1')
 def test_33_legacy_is_explicit_not_fallback(self):
  self.cfg('legacy')
  with self.assertRaises(DomainError):self.a.submit({'scenario_id':'materials','message':'hello'})
  self.rt.store.update_settings({'agent':{'legacy_yolo_confirmed':True}});t=self.a.submit({'scenario_id':'materials','message':'hello'});self.wait(t['id'],lambda x:x['status']=='completed')
 def test_34_terminal_never_revived(self):
  t=self.submit();self.wait(t['id'],lambda x:x['status']=='completed');self.a.patch(t['id'],status='running');self.assertEqual(self.a.task(t['id'])['status'],'completed')
 def test_35_sse_multiline_and_thinking_not_displayed(self):
  raw=io.BytesIO(b'id: e1\nevent: tool.start\ndata: {"name":\ndata: "x"}\n\n');self.assertEqual(list(event_stream(raw)),[('tool.start',{'name':'x'},'e1')])
 def test_36_settings_redact_credentials(self):
  self.cfg();data=self.rt.store.public_settings()['agent'];self.assertEqual(data['webui_password'],'');self.assertTrue(data['has_webui_password'])
 def test_37_confirmed_action_updates_never_close_case(self):
  cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','email.send',[cid]);a.update(status='executing',agent_task_id='local-fixture');self.rt.save_action(a)
  result=self.rt.action_receipt(a['id'],a['messages'][0]['id'],'submitted','external-test-id','protocol test, not delivery','local-fixture');self.assertEqual(result['status'],'submitted');self.assertNotEqual(self.rt.case_detail(cid)['status'],'closed')
 def test_38_not_configured_action_never_simulates(self):
  cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','email.send',[cid])
  with self.assertRaises(DomainError):self.rt.execute_action(a['id'],a['draft_revision'])
  self.assertNotEqual(self.rt.action(a['id'])['status'],'submitted')


 def test_39_no_direct_email_or_feishu_settings_or_imports(self):
  self.assertNotIn('email',self.rt.store.public_settings());self.assertNotIn('feishu',self.rt.store.public_settings())
  import ast
  for path in (ROOT/'app').glob('*.py'):
   tree=ast.parse(path.read_text())
   for node in ast.walk(tree):
    if isinstance(node,ast.Import):self.assertFalse(any(x.name=='smtplib' for x in node.names))
   self.assertNotIn('https://open.feishu.cn',path.read_text())
 def test_40_feishu_read_is_remote_agent_task(self):
  self.cfg();n=self.peer.starts;out=self.rt.invoke({'scenario_id':'materials','capability':'feishu.read','parameters':{'q':'采购交期'},'request_id':'read-skill'})
  t=self.wait(out['task']['id'],lambda x:x['status']=='completed');self.assertEqual(self.peer.starts,n+1);self.assertIn('飞书读取 Skill',t['message']);self.assertEqual(out['type'],'agent_task')
 def test_41_feishu_target_never_demo_fallback(self):
  cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','feishu.send',[cid]);self.assertTrue(a['blocked']);self.assertEqual(a['messages'][0]['recipient'],'')
  self.cfg()
  with self.assertRaises(DomainError):self.rt.execute_action(a['id'],a['draft_revision'])
 def test_42_feishu_send_uses_hermes_once(self):
  self.cfg();cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','feishu.send',[cid],{'recipient':'fixture-target-only'});n=self.peer.starts
  out=self.rt.execute_action(a['id'],a['draft_revision']);self.wait(out['agent_task_id'],lambda x:x['status']=='completed');again=self.rt.execute_action(a['id'],a['draft_revision']);self.assertEqual(out['agent_task_id'],again['agent_task_id']);self.assertEqual(n+1,self.peer.starts)
 def test_43_custom_skill_binding_without_python_change(self):
  p=self.rt.pack('materials');p['actions']['custom.lookup']={'executor':'hermes_skill','ui':'query','name':'自定义技能','instruction':'查询业务对象','skill_hint':'my-own-skill'};self.rt.publish(self.rt.draft_config('materials',p)['id']);self.cfg()
  out=self.rt.invoke({'scenario_id':'materials','capability':'custom.lookup','parameters':{'key':'a'}});t=self.wait(out['task']['id'],lambda x:x['status']=='completed');self.assertIn('my-own-skill',t['message'])
 def test_44_action_stop_no_stuck_execution(self):
  self.cfg();p=self.rt.pack('materials');p['actions']['email.send']['instruction']='HOLD';self.rt.publish(self.rt.draft_config('materials',p)['id']);cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','email.send',[cid]);out=self.rt.execute_action(a['id'],a['draft_revision']);self.wait(out['agent_task_id'],lambda x:bool(x['handle']));self.a.stop(out['agent_task_id']);self.wait(out['agent_task_id'],lambda x:x['status']=='cancelled');self.assertEqual(self.rt.action(a['id'])['status'],'needs_review');self.assertTrue(all(m['status']!='pending' for m in self.rt.action(a['id'])['messages']))
 def test_45_terminal_observation_cannot_reopen_card_or_phase(self):
  t=self.submit();t=self.wait(t['id'],lambda x:x['status']=='completed');self.a.patch(t['id'],status='running',card={'kind':'approval'},phase='running again');later=self.a.task(t['id']);self.assertIsNone(later['card']);self.assertEqual(later['phase'],t['phase'])
 def test_46_tools_have_one_current_status(self):
  t=self.submit('HOLD');self.wait(t['id'],lambda x:bool(x['handle']));self.a.record_tool(t['id'],'tool.started',{'name':'skill-example','call_id':'call-1'});self.a.record_tool(t['id'],'tool.completed',{'name':'skill-example','call_id':'call-1','duration':.2});act=[x for x in self.a.task(t['id'])['activity'] if x['name']=='skill-example'];self.assertEqual(len(act),1);self.assertEqual(act[0]['status'],'completed');self.a.stop(t['id'])
 def test_47_unknown_skill_reply_not_execution_receipt(self):
  self.cfg();p=self.rt.pack('materials');p['actions']['email.send']['skill_hint']='not-installed-skill';self.rt.publish(self.rt.draft_config('materials',p)['id']);cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');a=self.rt.prepare_action('materials','email.send',[cid]);out=self.rt.execute_action(a['id'],a['draft_revision']);self.wait(out['agent_task_id'],lambda x:x['status']=='completed');self.assertEqual(self.rt.action(a['id'])['status'],'needs_review');self.assertIn('not-installed-skill',self.a.task(out['agent_task_id'])['message'])
 def test_48_custom_notification_uses_field_binding(self):
  p=self.rt.pack('materials');p['actions']['custom.send']=dict(p['actions']['email.send'],name='另一个业务通知',recipient_field='owner_email');self.rt.publish(self.rt.draft_config('materials',p)['id']);cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high');out=self.rt.invoke({'scenario_id':'materials','capability':'custom.send','case_ids':[cid]});self.assertEqual(out['action']['name'],'另一个业务通知');self.assertIn('@',out['action']['messages'][0]['recipient'])

 def test_49_manual_continue_action_binds_new_task_and_preserves_receipt(self):
  self.cfg(); cid=next(c['id'] for c in self.rt.cases('materials') if c['severity']=='high')
  plan=self.rt.prepare_action('materials','email.send',[cid]);out=self.rt.execute_action(plan['id'],plan['draft_revision'])
  original=self.wait(out['agent_task_id'],lambda x:x['status']=='completed')
  resumed=self.a.continue_task(original['id'],{'confirm':True,'message':'HOLD 只核对原操作，不重发'})
  self.wait(resumed['id'],lambda x:bool(x['handle']))
  current=self.rt.action(plan['id']); self.assertEqual(current['agent_task_id'],resumed['id'])
  self.assertIn('unknown 分项先查询核对',resumed['message'])
  self.rt.action_receipt(plan['id'],plan['messages'][0]['id'],'submitted','verified-query-test-id','lookup from test provider',resumed['id'])
  self.rt.agent_action_finished(self.a.raw_task(original['id']))
  self.assertEqual(self.rt.action(plan['id'])['agent_task_id'],resumed['id'])
  self.assertEqual(self.rt.action(plan['id'])['messages'][0]['status'],'submitted')
  self.a.stop(resumed['id'])
 def test_50_bridge_describes_configured_skill_actions_not_local_senders(self):
  t=self.submit('HOLD');self.wait(t['id'],lambda x:bool(x['handle']));raw=self.a.raw_task(t['id'])
  d=bridge.invoke(self.a,t['id'],raw['_bridge_token'],{'operation':'describe'})
  self.assertTrue(all(c.get('executor')=='hermes_skill' for c in d['capabilities'] if c['id'] in ('email.send','feishu.send','feishu.read')))
  self.assertNotIn('action.execute',d['operations']);self.a.stop(t['id'])

if __name__=='__main__':unittest.main(verbosity=2)
