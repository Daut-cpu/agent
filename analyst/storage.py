"""Хранилище на SQLite: проекты, документы, задачи, артефакты, аудит.

Каждый запрос к данным проекта фильтруется по `project_id` — это основа
изоляции: данные одного проекта не попадают в контекст другого.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .security import Cipher

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    token_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    project_id TEXT NOT NULL REFERENCES projects(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id)
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    category TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    uploaded_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id),
    seq INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    problem TEXT NOT NULL,
    stage TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    role TEXT NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id, created_at);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    content TEXT NOT NULL,
    model TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, kind, version);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    username TEXT NOT NULL,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    input TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    model TEXT NOT NULL,
    version TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit(project_id, ts);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class NotFound(LookupError):
    pass


class Store:
    def __init__(self, path: str | Path = ":memory:", cipher: Cipher | None = None):
        self.path = str(path)
        self.cipher = cipher or Cipher()
        self._lock = threading.RLock()
        # Для :memory: держим одно соединение, иначе каждая операция видела бы пустую БД.
        self._shared = sqlite3.connect(self.path, check_same_thread=False) if self.path == ":memory:" else None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = self._shared or sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys = ON")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                if not self._shared:
                    db.close()

    def _one(self, sql: str, *args: Any) -> dict[str, Any] | None:
        with self._conn() as db:
            row = db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        with self._conn() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    # --- users ------------------------------------------------------------------

    def create_user(self, username: str, token_hash: str, is_admin: bool = False) -> dict[str, Any]:
        user = {"id": new_id(), "username": username, "token_hash": token_hash,
                "is_admin": int(is_admin), "created_at": time.time()}
        with self._conn() as db:
            db.execute("INSERT INTO users VALUES (:id, :username, :token_hash, :is_admin, :created_at)", user)
        return user

    def set_user_token(self, username: str, token_hash: str) -> None:
        with self._conn() as db:
            if not db.execute("UPDATE users SET token_hash=? WHERE username=?", (token_hash, username)).rowcount:
                raise NotFound(username)

    def user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE token_hash=?", token_hash)

    def user_by_name(self, username: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE username=?", username)

    def count_users(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM users")["n"]

    # --- projects -----------------------------------------------------------------

    def create_project(self, name: str, description: str, context: dict[str, Any], user_id: str) -> dict[str, Any]:
        now = time.time()
        project = {"id": new_id(), "name": name, "description": description,
                   "context": json.dumps(context, ensure_ascii=False), "created_by": user_id,
                   "created_at": now, "updated_at": now}
        with self._conn() as db:
            db.execute(
                "INSERT INTO projects VALUES (:id, :name, :description, :context, :created_by, :created_at, :updated_at)",
                project,
            )
            db.execute("INSERT INTO members VALUES (?, ?, 'owner')", (project["id"], user_id))
        return self.get_project(project["id"])

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM projects WHERE id=?", project_id)
        if not row:
            raise NotFound("project")
        row["context"] = json.loads(row["context"])
        return row

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        if "context" in fields:
            fields["context"] = json.dumps(fields["context"], ensure_ascii=False)
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        with self._conn() as db:
            db.execute(f"UPDATE projects SET {sets} WHERE id=:id", {**fields, "id": project_id})
        return self.get_project(project_id)

    def projects_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT p.*, m.role FROM projects p JOIN members m ON m.project_id=p.id "
            "WHERE m.user_id=? ORDER BY p.updated_at DESC",
            user_id,
        )
        for row in rows:
            row["context"] = json.loads(row["context"])
        return rows

    def role(self, project_id: str, user_id: str) -> str | None:
        row = self._one("SELECT role FROM members WHERE project_id=? AND user_id=?", project_id, user_id)
        return row["role"] if row else None

    def set_member(self, project_id: str, user_id: str, role: str) -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO members VALUES (?, ?, ?) ON CONFLICT(project_id, user_id) DO UPDATE SET role=excluded.role",
                (project_id, user_id, role),
            )

    def members(self, project_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT u.username, m.role FROM members m JOIN users u ON u.id=m.user_id WHERE m.project_id=?",
            project_id,
        )

    # --- documents ------------------------------------------------------------------

    def add_document(self, project_id: str, *, filename: str, media_type: str, category: str,
                     size: int, sha256: str, content: str, meta: dict[str, Any], user_id: str,
                     chunks: list[str]) -> dict[str, Any]:
        doc = {"id": new_id(), "project_id": project_id, "filename": filename, "media_type": media_type,
               "category": category, "size": size, "sha256": sha256,
               "content": self.cipher.encrypt(content), "meta": json.dumps(meta, ensure_ascii=False),
               "uploaded_by": user_id, "created_at": time.time()}
        with self._conn() as db:
            db.execute(
                "INSERT INTO documents VALUES (:id, :project_id, :filename, :media_type, :category, :size, "
                ":sha256, :content, :meta, :uploaded_by, :created_at)",
                doc,
            )
            db.executemany(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                [(new_id(), project_id, doc["id"], i, self.cipher.encrypt(t)) for i, t in enumerate(chunks)],
            )
        return self.get_document(project_id, doc["id"])

    def _doc_row(self, row: dict[str, Any], with_content: bool) -> dict[str, Any]:
        row["meta"] = json.loads(row["meta"])
        if with_content:
            row["content"] = self.cipher.decrypt(row["content"])
        else:
            row.pop("content", None)
        return row

    def get_document(self, project_id: str, document_id: str, with_content: bool = False) -> dict[str, Any]:
        row = self._one("SELECT * FROM documents WHERE id=? AND project_id=?", document_id, project_id)
        if not row:
            raise NotFound("document")
        return self._doc_row(row, with_content)

    def documents(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM documents WHERE project_id=? ORDER BY created_at", project_id)
        return [self._doc_row(r, False) for r in rows]

    def find_document_by_hash(self, project_id: str, sha256: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM documents WHERE project_id=? AND sha256=?", project_id, sha256)
        return self._doc_row(row, False) if row else None

    def delete_document(self, project_id: str, document_id: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM chunks WHERE document_id=? AND project_id=?", (document_id, project_id))
            if not db.execute("DELETE FROM documents WHERE id=? AND project_id=?", (document_id, project_id)).rowcount:
                raise NotFound("document")

    def chunks(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT c.id, c.document_id, c.seq, c.text, d.filename, d.category FROM chunks c "
            "JOIN documents d ON d.id=c.document_id WHERE c.project_id=? AND d.project_id=? "
            "ORDER BY d.created_at, c.seq",
            project_id, project_id,
        )
        for row in rows:
            row["text"] = self.cipher.decrypt(row["text"])
        return rows

    def knowledge_version(self, project_id: str) -> str:
        row = self._one(
            "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), 0) AS t FROM documents WHERE project_id=?", project_id
        )
        return f"{row['n']}:{row['t']}"

    # --- tasks & messages -------------------------------------------------------------

    def create_task(self, project_id: str, title: str, problem: str, user_id: str) -> dict[str, Any]:
        now = time.time()
        task = {"id": new_id(), "project_id": project_id, "title": title, "problem": problem,
                "stage": "interview", "created_by": user_id, "created_at": now, "updated_at": now}
        with self._conn() as db:
            db.execute(
                "INSERT INTO tasks VALUES (:id, :project_id, :title, :problem, :stage, :created_by, "
                ":created_at, :updated_at)",
                task,
            )
        return task

    def get_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM tasks WHERE id=? AND project_id=?", task_id, project_id)
        if not row:
            raise NotFound("task")
        return row

    def task_project(self, task_id: str) -> str:
        row = self._one("SELECT project_id FROM tasks WHERE id=?", task_id)
        if not row:
            raise NotFound("task")
        return row["project_id"]

    def tasks(self, project_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM tasks WHERE project_id=? ORDER BY updated_at DESC", project_id)

    def set_task_stage(self, project_id: str, task_id: str, stage: str) -> None:
        with self._conn() as db:
            db.execute("UPDATE tasks SET stage=?, updated_at=? WHERE id=? AND project_id=?",
                       (stage, time.time(), task_id, project_id))

    def add_message(self, project_id: str, task_id: str, role: str, author: str, content: str,
                    meta: dict[str, Any] | None = None) -> dict[str, Any]:
        msg = {"id": new_id(), "project_id": project_id, "task_id": task_id, "role": role, "author": author,
               "content": self.cipher.encrypt(content), "meta": json.dumps(meta or {}, ensure_ascii=False),
               "created_at": time.time()}
        with self._conn() as db:
            db.execute(
                "INSERT INTO messages VALUES (:id, :project_id, :task_id, :role, :author, :content, :meta, :created_at)",
                msg,
            )
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (msg["created_at"], task_id))
        return {**msg, "content": content, "meta": meta or {}}

    def messages(self, project_id: str, task_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM messages WHERE task_id=? AND project_id=? ORDER BY created_at", task_id, project_id
        )
        for row in rows:
            row["content"] = self.cipher.decrypt(row["content"])
            row["meta"] = json.loads(row["meta"])
        return rows

    # --- artifacts ------------------------------------------------------------------------

    def add_artifact(self, project_id: str, task_id: str, kind: str, content: dict[str, Any],
                     model: str, user_id: str) -> dict[str, Any]:
        with self._conn() as db:
            version = db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM artifacts WHERE task_id=? AND kind=?", (task_id, kind)
            ).fetchone()[0]
            art = {"id": new_id(), "project_id": project_id, "task_id": task_id, "kind": kind,
                   "version": version, "status": "draft",
                   "content": self.cipher.encrypt(json.dumps(content, ensure_ascii=False)),
                   "model": model, "created_by": user_id, "created_at": time.time()}
            db.execute(
                "INSERT INTO artifacts VALUES (:id, :project_id, :task_id, :kind, :version, :status, :content, "
                ":model, :created_by, :created_at)",
                art,
            )
        return {**art, "content": content}

    def _art_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["content"] = json.loads(self.cipher.decrypt(row["content"]))
        return row

    def get_artifact(self, project_id: str, artifact_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM artifacts WHERE id=? AND project_id=?", artifact_id, project_id)
        if not row:
            raise NotFound("artifact")
        return self._art_row(row)

    def latest_artifacts(self, project_id: str, task_id: str) -> dict[str, dict[str, Any]]:
        rows = self._all(
            "SELECT a.* FROM artifacts a JOIN (SELECT kind, MAX(version) AS v FROM artifacts "
            "WHERE task_id=? AND project_id=? GROUP BY kind) m ON a.kind=m.kind AND a.version=m.v "
            "WHERE a.task_id=? AND a.project_id=?",
            task_id, project_id, task_id, project_id,
        )
        return {r["kind"]: self._art_row(r) for r in rows}

    def artifact_history(self, project_id: str, task_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT id, kind, version, status, model, created_by, created_at FROM artifacts "
               "WHERE task_id=? AND project_id=?")
        args: list[Any] = [task_id, project_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return self._all(sql + " ORDER BY created_at DESC", *args)

    def set_artifact_status(self, project_id: str, artifact_id: str, status: str) -> None:
        with self._conn() as db:
            if not db.execute("UPDATE artifacts SET status=? WHERE id=? AND project_id=?",
                              (status, artifact_id, project_id)).rowcount:
                raise NotFound("artifact")

    # --- jobs ---------------------------------------------------------------------------------

    def create_job(self, project_id: str, task_id: str, kind: str, user_id: str) -> dict[str, Any]:
        now = time.time()
        job = {"id": new_id(), "project_id": project_id, "task_id": task_id, "kind": kind, "status": "queued",
               "error": "", "result": "{}", "created_by": user_id, "created_at": now, "updated_at": now}
        with self._conn() as db:
            db.execute(
                "INSERT INTO jobs VALUES (:id, :project_id, :task_id, :kind, :status, :error, :result, "
                ":created_by, :created_at, :updated_at)",
                job,
            )
        return self.get_job(project_id, job["id"])

    def update_job(self, job_id: str, status: str, error: str = "", result: dict[str, Any] | None = None) -> None:
        with self._conn() as db:
            db.execute("UPDATE jobs SET status=?, error=?, result=?, updated_at=? WHERE id=?",
                       (status, error, json.dumps(result or {}, ensure_ascii=False), time.time(), job_id))

    def get_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM jobs WHERE id=? AND project_id=?", job_id, project_id)
        if not row:
            raise NotFound("job")
        row["result"] = json.loads(row["result"])
        return row

    def job_project(self, job_id: str) -> str:
        row = self._one("SELECT project_id FROM jobs WHERE id=?", job_id)
        if not row:
            raise NotFound("job")
        return row["project_id"]

    def active_job(self, project_id: str, task_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT id FROM jobs WHERE task_id=? AND project_id=? AND status IN ('queued', 'running') "
            "ORDER BY created_at DESC",
            task_id, project_id,
        )
        return self.get_job(project_id, row["id"]) if row else None

    def fail_stale_jobs(self) -> None:
        """После перезапуска сервера незавершённые задания уже никто не выполнит."""
        with self._conn() as db:
            db.execute("UPDATE jobs SET status='failed', error='Сервер был перезапущен' "
                       "WHERE status IN ('queued', 'running')")

    # --- human-in-the-loop actions -----------------------------------------------------------

    def create_action(self, project_id: str, action: str, payload: dict[str, Any], summary: str,
                      user_id: str) -> dict[str, Any]:
        row = {"id": new_id(), "project_id": project_id, "action": action,
               "payload": json.dumps(payload, ensure_ascii=False), "summary": summary, "status": "pending",
               "requested_by": user_id, "decided_by": "", "created_at": time.time(), "decided_at": None}
        with self._conn() as db:
            db.execute(
                "INSERT INTO actions VALUES (:id, :project_id, :action, :payload, :summary, :status, "
                ":requested_by, :decided_by, :created_at, :decided_at)",
                row,
            )
        return self.get_action(project_id, row["id"])

    def get_action(self, project_id: str, action_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM actions WHERE id=? AND project_id=?", action_id, project_id)
        if not row:
            raise NotFound("action")
        row["payload"] = json.loads(row["payload"])
        return row

    def action_project(self, action_id: str) -> str:
        row = self._one("SELECT project_id FROM actions WHERE id=?", action_id)
        if not row:
            raise NotFound("action")
        return row["project_id"]

    def actions(self, project_id: str, status: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM actions WHERE project_id=?", [project_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = self._all(sql + " ORDER BY created_at DESC", *args)
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def decide_action(self, project_id: str, action_id: str, status: str, user_id: str) -> bool:
        """Атомарно переводит действие из pending; False — если его уже обработали."""
        with self._conn() as db:
            return bool(db.execute(
                "UPDATE actions SET status=?, decided_by=?, decided_at=? "
                "WHERE id=? AND project_id=? AND status='pending'",
                (status, user_id, time.time(), action_id, project_id),
            ).rowcount)

    # --- audit ---------------------------------------------------------------------------------

    def audit(self, *, username: str, project_id: str, action: str, input: str = "", artifact_id: str = "",
              model: str = "", version: str = "", result: str = "") -> None:
        with self._conn() as db:
            db.execute(
                "INSERT INTO audit (ts, username, project_id, action, input, artifact_id, model, version, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), username, project_id, action, input[:2000], artifact_id, model, version, result[:2000]),
            )

    def audit_log(self, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM audit WHERE project_id=? ORDER BY id DESC LIMIT ?", project_id, limit)
