"""Private bounded conversation cache and durable task/delivery identities."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def chinese_number(number):
    if not 0 <= number < 1000:
        return str(number)
    digits='零一二三四五六七八九'
    if number<10:return digits[number]
    if number<100:
        return (digits[number//10] if number>=20 else '')+'十'+(digits[number%10] if number%10 else '')
    rest=number%100
    return digits[number//100]+'百'+(('零' if rest<10 else '')+chinese_number(rest) if rest else '')


class RuntimeStore:
    def __init__(self, root):
        requested=Path(root).absolute()
        if requested.is_symlink():raise ValueError('state directory cannot be a symlink')
        self.root=requested.resolve()
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.stat().st_uid!=os.getuid():raise ValueError('state must belong to service user')
        self.root.chmod(0o700)
        self.path=self.root/'device-runtime.sqlite3'
        if self.path.is_symlink() or self.path.exists() and (self.path.stat().st_nlink!=1 or self.path.stat().st_uid!=os.getuid()):
            raise ValueError('state file must be private and service-owned')
        # Create with private permissions before SQLite opens it.
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd);self.path.chmod(0o600)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS conversations(scope TEXT PRIMARY KEY,thread_id TEXT,history TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,scope TEXT NOT NULL,request_id TEXT NOT NULL,label TEXT NOT NULL,prompt TEXT NOT NULL,state TEXT NOT NULL,result TEXT NOT NULL DEFAULT '',created REAL NOT NULL,UNIQUE(scope,request_id));
                CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,task_id TEXT NOT NULL,phase TEXT NOT NULL,text TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',claim TEXT,attempts INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS delivery_scope ON deliveries(scope,state,id);
            ''')
            if 'execution' not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
                db.execute("ALTER TABLE tasks ADD COLUMN execution TEXT NOT NULL DEFAULT '{}'")

    def record_execution(self,scope,task_id,model,thread_id):
        if not isinstance(model,str) or not model or not isinstance(thread_id,str) or not thread_id:raise ValueError('confirmed worker identity required')
        value=json.dumps({'executor':'local Codex CLI','model':model,'threadId':thread_id,'source':'thread/start response'})
        with self.db() as db:
            if db.execute("UPDATE tasks SET execution=? WHERE scope=? AND id=? AND state='running'",(value,scope,task_id)).rowcount!=1:
                raise ValueError('active owned task required')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=2)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def thread(self,scope):
        with self.db() as db:
            row=db.execute('SELECT thread_id FROM conversations WHERE scope=?',(scope,)).fetchone()
            return row['thread_id'] if row else None

    def set_thread(self,scope,thread):
        with self.db() as db:
            row=db.execute('SELECT thread_id FROM conversations WHERE scope=?',(scope,)).fetchone()
            if row and row['thread_id'] and row['thread_id']!=thread:
                raise ValueError('refusing to silently replace the saved conversation')
            db.execute('INSERT INTO conversations(scope,thread_id) VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET thread_id=excluded.thread_id',(scope,thread))

    def history(self,scope):
        with self.db() as db:
            row=db.execute('SELECT history FROM conversations WHERE scope=?',(scope,)).fetchone()
            return [{'role':item['role'],'text':item['text']} for item in json.loads(row['history'])] if row else []

    def remember(self,scope,event_id,role,text):
        if role not in ('user','assistant') or not isinstance(text,str) or not text.strip():return
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO conversations(scope) VALUES (?)',(scope,))
            items=json.loads(db.execute('SELECT history FROM conversations WHERE scope=?',(scope,)).fetchone()['history'])
            if any(item['id']==event_id for item in items):return
            items.append({'id':event_id,'role':role,'text':text[:2000]})
            items=items[-32:]
            while sum(len(item['text']) for item in items)>24000:items.pop(0)
            db.execute('UPDATE conversations SET history=? WHERE scope=?',(json.dumps(items,ensure_ascii=False),scope))

    def tasks(self,scope):
        with self.db() as db:return [dict(row) for row in db.execute('SELECT * FROM tasks WHERE scope=? ORDER BY created',(scope,))]

    def task(self,scope,task_id):
        with self.db() as db:
            row=db.execute('SELECT * FROM tasks WHERE scope=? AND (id=? OR label=?)',(scope,task_id,task_id)).fetchone()
            return dict(row) if row else None

    def create_task(self,scope,request_id,prompt):
        if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=4000:raise ValueError('task text must be 1..4000 characters')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=256:raise ValueError('stable tool call identity required')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM tasks WHERE scope=? AND request_id=?',(scope,request_id)).fetchone()
            if old:return dict(old)
            count=db.execute('SELECT COUNT(*) FROM tasks WHERE scope=?',(scope,)).fetchone()[0]
            active=db.execute("SELECT COUNT(*) FROM tasks WHERE scope=? AND state IN ('queued','running')",(scope,)).fetchone()[0]
            if count>=128 or active>=8:raise ValueError('task capacity reached')
            task_id=str(uuid4());words=('灯塔','竹林','晨星','海风','山谷','白帆','红叶','松林')
            label=words[count%8]+chinese_number(count//8+1)+'号'
            db.execute('INSERT INTO tasks(id,scope,request_id,label,prompt,state,created) VALUES (?,?,?,?,?,?,?)',(task_id,scope,request_id,label,prompt.strip(),'queued',time.time()))
            db.execute('INSERT INTO deliveries(scope,task_id,phase,text) VALUES (?,?,?,?)',(scope,task_id,'queued','任务已排队，结果尚未产生。'))
            return dict(db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())

    def start_task(self,scope,task_id):
        with self.db() as db:
            changed=db.execute("UPDATE tasks SET state='running' WHERE scope=? AND id=? AND state='queued'",(scope,task_id)).rowcount
            if changed:db.execute('INSERT INTO deliveries(scope,task_id,phase,text) VALUES (?,?,?,?)',(scope,task_id,'running','后台 Codex 已开始执行，结果尚未确认。'))
            return bool(changed)

    def progress(self,scope,task_id,text):
        if not isinstance(text,str) or not text.strip():return
        with self.db() as db:
            count=db.execute("SELECT COUNT(*) FROM deliveries WHERE scope=? AND task_id=? AND phase='progress'",(scope,task_id)).fetchone()[0]
            if count<8:db.execute('INSERT INTO deliveries(scope,task_id,phase,text) VALUES (?,?,?,?)',(scope,task_id,'progress',text[:1500]))

    def finish_task(self,scope,task_id,state,result):
        if state not in ('completed','failed','interrupted','cancelled'):raise ValueError('invalid terminal state')
        with self.db() as db:
            changed=db.execute("UPDATE tasks SET state=?,result=? WHERE scope=? AND id=? AND state IN ('queued','running')",(state,str(result)[:3000],scope,task_id)).rowcount
            if changed:db.execute('INSERT INTO deliveries(scope,task_id,phase,text) VALUES (?,?,?,?)',(scope,task_id,state,str(result)[:3000]))

    def claim_event(self,scope,claim):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE deliveries SET state='superseded' WHERE scope=? AND state='pending' AND phase IN ('queued','running','progress') AND task_id IN (SELECT id FROM tasks WHERE scope=? AND state NOT IN ('queued','running'))",(scope,scope))
            row=db.execute("SELECT d.*,t.label FROM deliveries d JOIN tasks t ON t.id=d.task_id WHERE d.scope=? AND d.state='pending' ORDER BY d.id LIMIT 1",(scope,)).fetchone()
            if row is None:return None
            db.execute("UPDATE deliveries SET state='inflight',claim=?,attempts=attempts+1 WHERE id=?",(claim,row['id']))
            return {**dict(row),'attempts':row['attempts']+1}

    def finish_event(self,scope,event_id,claim):
        with self.db() as db:return bool(db.execute("UPDATE deliveries SET state='sent',claim=NULL WHERE scope=? AND id=? AND claim=? AND state='inflight'",(scope,event_id,claim)).rowcount)

    def release_claims(self,scope,claim):
        with self.db() as db:db.execute("UPDATE deliveries SET state=CASE WHEN attempts>=2 THEN 'unconfirmed' ELSE 'pending' END,claim=NULL WHERE scope=? AND claim=? AND state='inflight'",(scope,claim))

    def retry(self,scope,task_id):
        if self.task(scope,task_id) is None:raise ValueError('task is not owned by this conversation')
        with self.db() as db:db.execute("UPDATE deliveries SET state='pending',attempts=0,claim=NULL WHERE scope=? AND task_id=? AND state IN ('sent','unconfirmed') AND phase IN ('completed','failed','interrupted','cancelled')",(scope,task_id))

    def recover(self):
        with self.db() as db:
            rows=db.execute("SELECT scope,id FROM tasks WHERE state IN ('queued','running')").fetchall()
            db.execute("UPDATE deliveries SET state=CASE WHEN attempts>=2 THEN 'unconfirmed' ELSE 'pending' END,claim=NULL WHERE state='inflight'")
        for row in rows:self.finish_task(row['scope'],row['id'],'interrupted','服务已重启，先前任务中断；为避免重复执行，未自动重跑。')
