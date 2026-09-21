import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def uid(prefix): return prefix+'-'+uuid4().hex[:12]
def dump(x): return json.dumps(x,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def load(s, default=None): return json.loads(s) if s else default

SCHEMA='''
CREATE TABLE IF NOT EXISTS scenarios(id TEXT PRIMARY KEY, config TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, source_revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(scenario TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(scenario,id));
CREATE TABLE IF NOT EXISTS results(scenario TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(scenario,id));
CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, scenario TEXT NOT NULL, object_id TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(scenario,object_id));
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, scenario TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, scenario TEXT NOT NULL, body TEXT NOT NULL, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, scenario TEXT, kind TEXT NOT NULL, entity TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, scenario TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, scenario TEXT NOT NULL, session TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, scenario TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY AUTOINCREMENT, scenario TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS remote_sessions(local_session TEXT PRIMARY KEY,endpoint TEXT NOT NULL,remote_id TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feishu_batches(
    scenario TEXT NOT NULL,
    event_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scenario,event_id)
);
CREATE TABLE IF NOT EXISTS feishu_artifacts(
    scenario TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scenario,artifact_id)
);
CREATE INDEX IF NOT EXISTS ix_feishu_artifacts_scenario ON feishu_artifacts(scenario,created_at DESC);
CREATE TABLE IF NOT EXISTS rag_collections(
    id TEXT PRIMARY KEY,
    scenario TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    config TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rag_collections_scenario ON rag_collections(scenario,updated_at DESC);
CREATE TABLE IF NOT EXISTS rag_documents(
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(collection_id,external_id,version)
);
CREATE INDEX IF NOT EXISTS ix_rag_documents_collection ON rag_documents(collection_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS rag_chunks(
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL,
    metadata TEXT NOT NULL,
    embedding_status TEXT NOT NULL,
    vector_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(document_id,ordinal)
);
CREATE INDEX IF NOT EXISTS ix_rag_chunks_document ON rag_chunks(document_id,ordinal);
CREATE TABLE IF NOT EXISTS rag_jobs(
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rag_jobs_collection ON rag_jobs(collection_id,created_at DESC);
CREATE TABLE IF NOT EXISTS rag_queries(
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
    query TEXT NOT NULL,
    filters TEXT NOT NULL,
    top_k INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rag_queries_collection ON rag_queries(collection_id,created_at DESC);
CREATE TABLE IF NOT EXISTS rag_hits(
    query_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL,
    rerank_score REAL,
    method TEXT NOT NULL,
    citation TEXT NOT NULL,
    PRIMARY KEY(query_id,chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_rag_hits_query ON rag_hits(query_id,rank);
CREATE TABLE IF NOT EXISTS settings(id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_scenario ON events(scenario,seq);
CREATE INDEX IF NOT EXISTS ix_runs_scenario ON runs(scenario,created_at);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session,id);
'''

DEFAULT_SETTINGS={
 'agent':{'mode':'dsh','base_url':'','model':'hermes-agent','api_key':'','timeout':15,'history_messages':12,'webui_url':'','webui_password':'','webui_cookie':'','webui_csrf':'','webui_profile':'','webui_workspace':'','webui_model':'','webui_yolo':False,'legacy_yolo_confirmed':False,
          'dsh_provider':'deepseek-official','dsh_model':'deepseek-v4-flash','dsh_model_url':'','dsh_api_key':'','dsh_approval_mode':'ask','dsh_context_threshold':0.72,'dsh_context_retain':0.16},
 'workspace':{'operator':'本机使用者','bridge_url':''},
 'automation':{'enabled':False,'scenario':'all','message':'检查本批业务风险并给出处理建议。只使用已经配置的能力；缺少 Skill 时明确说明，不假称发送。'},
}

class Store:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        with self.db() as c:
            c.executescript(SCHEMA)
            c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',('connections',dump(DEFAULT_SETTINGS)))
    @contextmanager
    def db(self):
        with self.lock:
            c=sqlite3.connect(str(self.path),timeout=15)
            c.row_factory=sqlite3.Row
            c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=15000')
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback(); raise
            finally: c.close()
    def event(self,c,scenario,kind,entity,body):
        c.execute('INSERT INTO events(scenario,kind,entity,body,created_at) VALUES (?,?,?,?,?)',(scenario,kind,entity,dump(body),now()))
    def settings(self):
        with self.db() as c: saved=load(c.execute('SELECT body FROM settings WHERE id=?',('connections',)).fetchone()[0])
        return {key:{**value,**saved.get(key,{})} for key,value in DEFAULT_SETTINGS.items()}
    def update_settings(self,data):
        settings=self.settings()
        for key in settings:
            incoming=data.get(key,{})
            for k,v in incoming.items():
                if k not in settings[key]: continue
                if k in ('api_key','password','app_secret','tenant_token','webui_password','webui_cookie','webui_csrf','dsh_api_key') and v=='': continue
                settings[key][k]=v
        from .engine import DomainError
        if settings['agent']['mode'] not in ('dsh','hermes','webui','legacy'): raise DomainError('Agent 模式无效')
        try:
            settings['agent']['timeout']=max(2,min(120,int(settings['agent']['timeout'])))
            settings['agent']['history_messages']=max(2,min(30,int(settings['agent']['history_messages'])))
            threshold=float(settings['agent']['dsh_context_threshold'])
            retain=float(settings['agent']['dsh_context_retain'])
            if not 0.2<=threshold<=0.95: raise DomainError('DSH 上下文压缩阈值需在 0.2–0.95 之间')
            if not 0.02<=retain<threshold: raise DomainError('DSH 上下文保留比例需不小于 0.02 且低于压缩阈值')
            settings['agent']['dsh_context_threshold']=threshold
            settings['agent']['dsh_context_retain']=retain
        except (ValueError,TypeError): raise DomainError('超时、历史条数和 DSH 上下文参数必须为数字')
        if settings['agent']['dsh_approval_mode'] not in ('ask','auto'): raise DomainError('DSH 工具批准方式必须为 ask 或 auto')
        with self.db() as c:
            c.execute('UPDATE settings SET body=? WHERE id=?',(dump(settings),'connections'))
        return self.public_settings()
    def public_settings(self):
        s=self.settings()
        for cfg in s.values():
            for k in list(cfg):
                if k in ('api_key','password','app_secret','tenant_token','webui_password','webui_cookie','webui_csrf','dsh_api_key'):
                    cfg['has_'+k]=bool(cfg[k]); cfg[k]=''
        return s
    def recover(self):
        """A restart must not turn an uncertain external operation into success or retry it."""
        with self.db() as c:
            for row in c.execute('SELECT id,body FROM actions').fetchall():
                a=load(row['body'])
                if a.get('status')=='executing' and not a.get('agent_task_id'):
                    a.update(status='unknown',error='运行服务已重启；外部执行结果待核对，不自动重试。')
                    c.execute('UPDATE actions SET body=? WHERE id=?',(dump(a),row['id']))
            # AgentService performs read-only remote reconciliation, never automatic rerun.
