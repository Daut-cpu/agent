"""Прикладной слой: права доступа, фоновые задания, подтверждение действий, аудит."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import __version__
from .agents.orchestrator import STEPS, Orchestrator, StepError
from .exporters import ExportError, export
from .knowledge import BM25Retriever, DocumentError, Retriever, chunk_text, knowledge_tree, parse_document
from .llm import LLM, LLMError
from .renderers import artifact_markdown, diagram_sources
from .security import ROLES, hash_token, new_token, role_allows
from .storage import NotFound, Store

log = logging.getLogger(__name__)

CONTEXT_FIELDS = ("domain", "tech_stack", "architecture", "business_rules", "glossary", "integrations",
                  "existing_apis", "database", "nfr")
PROTECTED_CONTEXT = ("architecture",)  # изменение существующей архитектурной документации — через подтверждение

# Кто может подтвердить действие.
ACTION_ROLES = {"delete_document": "owner", "publish_artifact": "editor", "update_architecture": "owner"}


class Forbidden(PermissionError):
    pass


class Unauthorized(PermissionError):
    pass


class Conflict(RuntimeError):
    pass


class BadRequest(ValueError):
    pass


class AnalystService:
    def __init__(self, store: Store, llm: LLM, retriever: Retriever | None = None,
                 workers: int = 4, inline_jobs: bool = False):
        self.store = store
        self.llm = llm
        self.retriever = retriever or BM25Retriever(store)
        self.orchestrator = Orchestrator(store, llm, self.retriever)
        self.inline_jobs = inline_jobs
        self.executor = None if inline_jobs else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent")
        store.fail_stale_jobs()

    # --- пользователи и права ---------------------------------------------------------------

    def create_user(self, username: str, is_admin: bool = False) -> tuple[dict[str, Any], str]:
        if self.store.user_by_name(username):
            raise Conflict(f"Пользователь {username} уже существует")
        token = new_token()
        return self.store.create_user(username, hash_token(token), is_admin), token

    def rotate_token(self, username: str) -> str:
        token = new_token()
        self.store.set_user_token(username, hash_token(token))
        return token

    def authenticate(self, token: str | None) -> dict[str, Any]:
        user = self.store.user_by_token_hash(hash_token(token)) if token else None
        if not user:
            raise Unauthorized("Неверный или отсутствующий токен")
        return user

    def require(self, user: dict[str, Any], project_id: str, role: str = "viewer") -> str:
        actual = self.store.role(project_id, user["id"])
        if actual is None:
            # Не раскрываем существование чужих проектов.
            raise NotFound("project")
        if not role_allows(actual, role):
            raise Forbidden(f"Нужна роль {role}, у вас {actual}")
        return actual

    # --- проекты ------------------------------------------------------------------------------

    def create_project(self, user: dict[str, Any], name: str, description: str = "",
                       context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not name.strip():
            raise BadRequest("Название проекта не может быть пустым")
        project = self.store.create_project(name.strip(), description, self._clean_context(context or {}), user["id"])
        self.store.audit(username=user["username"], project_id=project["id"], action="Create project",
                         input=name, version=__version__, result="created")
        return project

    @staticmethod
    def _clean_context(context: dict[str, Any]) -> dict[str, Any]:
        unknown = set(context) - set(CONTEXT_FIELDS)
        if unknown:
            raise BadRequest(f"Неизвестные поля контекста: {', '.join(sorted(unknown))}")
        return {k: str(v) for k, v in context.items()}

    def update_project(self, user: dict[str, Any], project_id: str, name: str | None = None,
                       description: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require(user, project_id, "editor")
        project = self.store.get_project(project_id)
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
        if description is not None:
            fields["description"] = description
        pending = None
        if context is not None:
            context = self._clean_context(context)
            merged = {**project["context"], **context}
            protected = {k: v for k, v in context.items()
                         if k in PROTECTED_CONTEXT and project["context"].get(k) and project["context"][k] != v}
            for k in protected:
                merged[k] = project["context"][k]
            fields["context"] = merged
            if protected:
                pending = self.store.create_action(
                    project_id, "update_architecture", {"context": protected},
                    "Изменить существующую архитектурную документацию проекта", user["id"])
        project = self.store.update_project(project_id, **fields) if fields else project
        self.store.audit(username=user["username"], project_id=project_id, action="Update project",
                         input=", ".join(sorted((context or {}).keys())), version=__version__,
                         result="pending confirmation" if pending else "updated")
        return {**project, "pending_action": pending}

    def add_member(self, user: dict[str, Any], project_id: str, username: str, role: str) -> None:
        self.require(user, project_id, "owner")
        if role not in ROLES:
            raise BadRequest(f"Роль должна быть одной из: {', '.join(ROLES)}")
        member = self.store.user_by_name(username)
        if not member:
            raise NotFound("user")
        self.store.set_member(project_id, member["id"], role)
        self.store.audit(username=user["username"], project_id=project_id, action="Grant role",
                         input=f"{username}: {role}", version=__version__, result="ok")

    # --- документы и база знаний --------------------------------------------------------------

    def upload_document(self, user: dict[str, Any], project_id: str, filename: str, data: bytes,
                        category: str | None = None) -> dict[str, Any]:
        self.require(user, project_id, "editor")
        try:
            parsed = parse_document(filename, data, category)
        except DocumentError as e:
            raise BadRequest(str(e)) from e
        duplicate = self.store.find_document_by_hash(project_id, parsed.sha256)
        if duplicate:
            raise Conflict(f"Этот файл уже загружен как {duplicate['filename']}")
        chunks = chunk_text(parsed.text)
        doc = self.store.add_document(
            project_id, filename=filename, media_type=parsed.media_type, category=parsed.category,
            size=len(data), sha256=parsed.sha256, content=parsed.text,
            meta={**parsed.meta, "chunks": len(chunks)}, user_id=user["id"], chunks=chunks)
        self.store.audit(username=user["username"], project_id=project_id, action="Upload document",
                         input=filename, version=__version__, result=f"{parsed.category}, {len(chunks)} chunks")
        return doc

    def request_document_delete(self, user: dict[str, Any], project_id: str, document_id: str) -> dict[str, Any]:
        self.require(user, project_id, "editor")
        doc = self.store.get_document(project_id, document_id)
        return self.store.create_action(project_id, "delete_document", {"document_id": document_id},
                                        f"Удалить документ {doc['filename']}", user["id"])

    def search(self, user: dict[str, Any], project_id: str, query: str, k: int = 8) -> list[dict[str, Any]]:
        self.require(user, project_id)
        return [h.__dict__ for h in self.retriever.search(project_id, query, k)]

    def knowledge(self, user: dict[str, Any], project_id: str) -> dict[str, Any]:
        self.require(user, project_id)
        return knowledge_tree(self.store, project_id)

    # --- задачи и чат ---------------------------------------------------------------------------

    def create_task(self, user: dict[str, Any], project_id: str, problem: str, title: str = "") -> dict[str, Any]:
        self.require(user, project_id, "editor")
        problem = problem.strip()
        if not problem:
            raise BadRequest("Опишите бизнес-задачу")
        title = title.strip() or (problem[:80] + ("…" if len(problem) > 80 else ""))
        task = self.store.create_task(project_id, title, problem, user["id"])
        self.store.add_message(project_id, task["id"], "user", user["username"], problem)
        job = self._submit(user, project_id, task["id"], "discovery",
                           lambda: self.orchestrator.start_task(project_id, task["id"], user))
        return {**task, "job": job}

    def post_message(self, user: dict[str, Any], project_id: str, task_id: str, text: str) -> dict[str, Any]:
        self.require(user, project_id, "editor")
        self.store.get_task(project_id, task_id)
        if not text.strip():
            raise BadRequest("Пустое сообщение")
        self._ensure_idle(project_id, task_id)
        msg = self.store.add_message(project_id, task_id, "user", user["username"], text.strip())
        job = self._submit(user, project_id, task_id, "message",
                           lambda: self.orchestrator.handle_message(project_id, task_id, user, text.strip()))
        return {"message": msg, "job": job}

    def run_step(self, user: dict[str, Any], project_id: str, task_id: str, step: str,
                 instruction: str = "") -> dict[str, Any]:
        self.require(user, project_id, "editor")
        self.store.get_task(project_id, task_id)
        if step != "pipeline" and step not in STEPS:
            raise BadRequest(f"Неизвестный шаг {step}: {', '.join(STEPS + ('pipeline',))}")
        self._ensure_idle(project_id, task_id)
        if step == "pipeline":
            def fn() -> list[dict[str, Any]]:
                return self.orchestrator.run_pipeline(project_id, task_id, user, instruction)
        else:
            def fn() -> list[dict[str, Any]]:
                return self.orchestrator.run_step(project_id, task_id, user, step, instruction)
        return self._submit(user, project_id, task_id, step, fn)

    def task_detail(self, user: dict[str, Any], project_id: str, task_id: str) -> dict[str, Any]:
        role = self.require(user, project_id)
        task = self.store.get_task(project_id, task_id)
        latest = self.store.latest_artifacts(project_id, task_id)
        return {
            **task,
            "role": role,
            "messages": self.store.messages(project_id, task_id),
            "artifacts": {k: self._artifact_view(a) for k, a in latest.items()},
            "job": self.store.active_job(project_id, task_id),
        }

    def _artifact_view(self, art: dict[str, Any]) -> dict[str, Any]:
        view = {k: art[k] for k in ("id", "kind", "version", "status", "model", "created_at")}
        view["content"] = art["content"]
        view["markdown"] = artifact_markdown(art["kind"], art["content"])
        sources = diagram_sources(art["kind"], art["content"])
        if sources:
            view["diagrams"] = sources
        return view

    def artifact(self, user: dict[str, Any], project_id: str, artifact_id: str) -> dict[str, Any]:
        self.require(user, project_id)
        return self._artifact_view(self.store.get_artifact(project_id, artifact_id))

    def artifact_history(self, user: dict[str, Any], project_id: str, task_id: str,
                         kind: str | None = None) -> list[dict[str, Any]]:
        self.require(user, project_id)
        self.store.get_task(project_id, task_id)
        return self.store.artifact_history(project_id, task_id, kind)

    def request_publish(self, user: dict[str, Any], project_id: str, artifact_id: str) -> dict[str, Any]:
        self.require(user, project_id, "editor")
        art = self.store.get_artifact(project_id, artifact_id)
        if art["status"] == "published":
            raise Conflict("Артефакт уже опубликован")
        return self.store.create_action(project_id, "publish_artifact", {"artifact_id": artifact_id},
                                        f"Опубликовать {art['kind']} v{art['version']}", user["id"])

    def export(self, user: dict[str, Any], project_id: str, task_id: str, fmt: str) -> tuple[bytes, str, str]:
        self.require(user, project_id)
        project = self.store.get_project(project_id)
        task = self.store.get_task(project_id, task_id)
        try:
            result = export(fmt, project, task, self.store.latest_artifacts(project_id, task_id))
        except ExportError as e:
            raise BadRequest(str(e)) from e
        self.store.audit(username=user["username"], project_id=project_id, action="Export",
                         input=f"{task['title']} → {fmt}", version=__version__, result=result[2])
        return result

    # --- подтверждение действий (human-in-the-loop) ---------------------------------------------

    def actions(self, user: dict[str, Any], project_id: str, status: str | None = "pending") -> list[dict[str, Any]]:
        self.require(user, project_id)
        return self.store.actions(project_id, status)

    def decide(self, user: dict[str, Any], project_id: str, action_id: str, approve: bool) -> dict[str, Any]:
        action = self.store.get_action(project_id, action_id)
        self.require(user, project_id, ACTION_ROLES.get(action["action"], "owner"))
        if not self.store.decide_action(project_id, action_id, "approved" if approve else "rejected", user["id"]):
            raise Conflict("Действие уже обработано")
        if approve:
            self._execute(user, project_id, action)
        self.store.audit(username=user["username"], project_id=project_id,
                         action=f"{'Confirm' if approve else 'Reject'} {action['action']}",
                         input=action["summary"], artifact_id=action["payload"].get("artifact_id", ""),
                         version=__version__, result="executed" if approve else "rejected")
        return self.store.get_action(project_id, action_id)

    def _execute(self, user: dict[str, Any], project_id: str, action: dict[str, Any]) -> None:
        payload = action["payload"]
        if action["action"] == "delete_document":
            self.store.delete_document(project_id, payload["document_id"])
        elif action["action"] == "publish_artifact":
            self.store.set_artifact_status(project_id, payload["artifact_id"], "published")
        elif action["action"] == "update_architecture":
            project = self.store.get_project(project_id)
            self.store.update_project(project_id, context={**project["context"], **payload["context"]})
        else:
            raise BadRequest(f"Неизвестное действие {action['action']}")

    # --- аудит и задания ------------------------------------------------------------------------

    def audit_log(self, user: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
        self.require(user, project_id, "owner")
        return self.store.audit_log(project_id)

    def job(self, user: dict[str, Any], job_id: str) -> dict[str, Any]:
        project_id = self.store.job_project(job_id)
        self.require(user, project_id)
        return self.store.get_job(project_id, job_id)

    def _ensure_idle(self, project_id: str, task_id: str) -> None:
        if self.store.active_job(project_id, task_id):
            raise Conflict("Агент ещё работает над предыдущим запросом по этой задаче")

    def _submit(self, user: dict[str, Any], project_id: str, task_id: str, kind: str,
                fn: Callable[[], list[dict[str, Any]]]) -> dict[str, Any]:
        job = self.store.create_job(project_id, task_id, kind, user["id"])

        def run() -> None:
            self.store.update_job(job["id"], "running")
            try:
                messages = fn()
            except (StepError, LLMError, NotFound) as e:
                self._fail(job, user, project_id, task_id, str(e))
            except Exception:  # noqa: BLE001 — задание не должно падать молча
                log.exception("job %s failed", job["id"])
                self._fail(job, user, project_id, task_id, "Внутренняя ошибка агента. Подробности в логе сервера.")
            else:
                self.store.update_job(job["id"], "succeeded", result={"messages": [m["id"] for m in messages]})

        if self.executor:
            self.executor.submit(run)
        else:
            run()
        return self.store.get_job(project_id, job["id"])

    def _fail(self, job: dict[str, Any], user: dict[str, Any], project_id: str, task_id: str, error: str) -> None:
        self.store.update_job(job["id"], "failed", error=error)
        self.store.add_message(project_id, task_id, "assistant", "agent", f"⚠ {error}", {"kind": "error"})
        self.store.audit(username=user["username"], project_id=project_id, action=f"Job {job['kind']}",
                         model=self.llm.model, version=__version__, result=f"failed: {error}")
