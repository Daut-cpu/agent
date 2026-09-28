"""Сборка контекста для агентов: профиль проекта, база знаний, интервью, артефакты.

Контекст собирается только из данных одного проекта (project_id задачи),
поэтому сведения других проектов в промпт попасть не могут.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from ..knowledge import Hit, Retriever, existing_endpoints
from ..storage import Store

PROJECT_FIELDS = [
    ("domain", "Домен"),
    ("tech_stack", "Технологический стек"),
    ("architecture", "Архитектура"),
    ("business_rules", "Бизнес-правила"),
    ("glossary", "Глоссарий"),
    ("integrations", "Интеграции"),
    ("existing_apis", "Существующие API"),
    ("database", "База данных"),
    ("nfr", "NFR"),
]

MAX_MESSAGE_CHARS = 6000
# Небольшая база знаний передаётся целиком: лексический поиск не находит,
# например, английскую OpenAPI-спецификацию по русскому описанию задачи.
FULL_KNOWLEDGE_CHARS = int(os.environ.get("ANALYST_FULL_KB_CHARS", "150000"))
MAX_TRANSCRIPT_CHARS = 60000


@dataclass
class TaskContext:
    project: dict[str, Any]
    task: dict[str, Any]
    messages: list[dict[str, Any]]
    artifacts: dict[str, dict[str, Any]]
    knowledge: list[Hit] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    instruction: str = ""

    def artifact(self, kind: str) -> dict[str, Any] | None:
        art = self.artifacts.get(kind)
        return art["content"] if art else None


def load_context(store: Store, retriever: Retriever, project_id: str, task_id: str,
                 instruction: str = "", k: int = 8) -> TaskContext:
    project = store.get_project(project_id)
    task = store.get_task(project_id, task_id)
    messages = store.messages(project_id, task_id)
    artifacts = store.latest_artifacts(project_id, task_id)
    title = ""
    if "requirements" in artifacts:
        title = artifacts["requirements"]["content"].get("title", "")
    query = " ".join(x for x in (task["problem"], title, instruction) if x)
    return TaskContext(
        project=project, task=task, messages=messages, artifacts=artifacts,
        knowledge=_knowledge(store, retriever, project_id, query, k),
        endpoints=existing_endpoints(store, project_id),
        instruction=instruction,
    )


def _knowledge(store: Store, retriever: Retriever, project_id: str, query: str, k: int) -> list[Hit]:
    chunks = store.chunks(project_id)
    if sum(len(c["text"]) for c in chunks) <= FULL_KNOWLEDGE_CHARS:
        return [Hit(c["document_id"], c["filename"], c["category"], c["text"], 1.0) for c in chunks]
    return retriever.search(project_id, query, k=k)


def project_profile(project: dict[str, Any]) -> str:
    """Стабильный блок системного промпта: меняется только при правке проекта."""
    ctx = project.get("context") or {}
    lines = [f"<project name=\"{project['name']}\">", project.get("description") or ""]
    for key, title in PROJECT_FIELDS:
        value = ctx.get(key)
        if value:
            lines.append(f"## {title}\n{value}")
    lines.append("</project>")
    return "\n".join(lines)


def _as_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def render(ctx: TaskContext, *, artifacts: tuple[str, ...] = (), transcript: bool = True,
           knowledge: bool = True, endpoints: bool = False, extra: str = "") -> str:
    parts = [f"<task title=\"{ctx.task['title']}\">\n{ctx.task['problem']}\n</task>"]
    if transcript:
        lines, total = [], 0
        for m in reversed(ctx.messages):
            text = m["content"][:MAX_MESSAGE_CHARS]
            total += len(text)
            if total > MAX_TRANSCRIPT_CHARS:
                break
            who = "Пользователь" if m["role"] == "user" else "Агент"
            lines.append(f"[{who}] {text}")
        if lines:
            parts.append("<conversation>\n" + "\n\n".join(reversed(lines)) + "\n</conversation>")
    if knowledge and ctx.knowledge:
        docs = "\n".join(
            f"<document name=\"{h.filename}\" category=\"{h.category}\">\n{h.text}\n</document>" for h in ctx.knowledge
        )
        parts.append(f"<knowledge_base>\n{docs}\n</knowledge_base>")
    if endpoints:
        listed = "\n".join(f"- {e}" for e in ctx.endpoints) or "(в проекте пока нет описанных API)"
        parts.append(f"<existing_apis>\n{listed}\n</existing_apis>")
    for kind in artifacts:
        content = ctx.artifact(kind)
        if content is not None:
            parts.append(f"<artifact kind=\"{kind}\">\n{_as_json(content)}\n</artifact>")
    if extra:
        parts.append(extra)
    if ctx.instruction:
        parts.append(f"<user_instruction>\n{ctx.instruction}\n</user_instruction>")
    return "\n\n".join(parts)
