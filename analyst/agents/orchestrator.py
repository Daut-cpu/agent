"""Оркестратор: выбирает агента, собирает для него контекст, сохраняет результат.

Специализированные агенты (Discovery, Requirements, Review, Architecture, API,
Diagram, QA) — это системный промпт + схема ответа. Оркестратор решает, кто
нужен, в каком порядке их запускать и когда остановиться и спросить
пользователя (интервью, «Сформировать требования?»).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel

from .. import __version__
from ..checklist import quick_check, requirements_text, score, score_bar, traceability
from ..knowledge import Retriever
from ..llm import LLM
from ..models import (
    ActivityDiagram, ApiSpec, ArchitectureProposal, ArchitectureReview, ChatAnswer, DiscoveryResult,
    ERDiagram, RequirementsDoc, ReviewResult, RouteDecision, SequenceDiagram, TestSuite,
)
from ..renderers import openapi_document
from ..storage import Store
from . import prompts
from .context import TaskContext, load_context, project_profile, render

log = logging.getLogger(__name__)

DEFAULT_EFFORT = {
    "router": "low", "chat": "low", "discovery": "medium", "requirements": "high", "review": "high",
    "architecture": "high", "architecture_review": "high", "api": "high", "diagram": "medium", "tests": "medium",
}

STEPS = ("discovery", "requirements", "review", "architecture", "architecture_review", "api",
         "sequence", "activity", "erd", "component", "tests", "final_review")
PIPELINE = ("requirements", "review", "architecture", "api", "sequence", "tests", "final_review")
NEEDS_REQUIREMENTS = {"architecture", "api", "sequence", "activity", "erd", "component", "tests"}

READY_PROMPT = "Контекст собран. Сформировать требования?"


class StepError(ValueError):
    """Шаг нельзя выполнить с текущими данными (понятная пользователю ошибка)."""


@dataclass
class Agent:
    name: str
    system: str
    schema: type[BaseModel]
    effort_key: str


AGENTS = {
    "discovery": Agent("Discovery", prompts.DISCOVERY, DiscoveryResult, "discovery"),
    "requirements": Agent("Requirements", prompts.REQUIREMENTS, RequirementsDoc, "requirements"),
    "review": Agent("Review", prompts.REVIEW, ReviewResult, "review"),
    "architecture": Agent("Architecture", prompts.ARCHITECTURE, ArchitectureProposal, "architecture"),
    "architecture_review": Agent("Architecture review", prompts.ARCHITECTURE_REVIEW, ArchitectureReview,
                                 "architecture_review"),
    "api": Agent("API", prompts.API, ApiSpec, "api"),
    "sequence": Agent("Diagram", prompts.DIAGRAM, SequenceDiagram, "diagram"),
    "activity": Agent("Diagram", prompts.DIAGRAM, ActivityDiagram, "diagram"),
    "erd": Agent("Diagram", prompts.DIAGRAM, ERDiagram, "diagram"),
    "tests": Agent("QA", prompts.QA, TestSuite, "tests"),
    "router": Agent("Orchestrator", prompts.ROUTER, RouteDecision, "router"),
    "chat": Agent("Chat", prompts.CHAT, ChatAnswer, "chat"),
}


def _normalize_path(path: str) -> str:
    import re

    path = path.split("?")[0].rstrip("/").lower()
    return re.sub(r"\{[^}]+\}|:[a-z_]+", "{}", path)


def endpoint_conflicts(spec: ApiSpec, existing: list[str]) -> list[str]:
    """Новые операции, совпадающие с уже существующими (метод + путь)."""
    known = {}
    for line in existing:
        parts = line.split(" ", 2)
        if len(parts) >= 2:
            known[(parts[0].upper(), _normalize_path(parts[1]))] = line
    conflicts = []
    for ep in spec.endpoints:
        hit = known.get((ep.method, _normalize_path(ep.path)))
        if hit and hit not in spec.reused_existing:
            conflicts.append(f"{ep.method} {ep.path} уже есть в проекте: {hit}")
    return conflicts


class Orchestrator:
    def __init__(self, store: Store, llm: LLM, retriever: Retriever, efforts: dict[str, str] | None = None):
        self.store = store
        self.llm = llm
        self.retriever = retriever
        self.efforts = {**DEFAULT_EFFORT, **(efforts or {})}

    # --- вызов агента ------------------------------------------------------------------

    def _call(self, agent_key: str, ctx: TaskContext, prompt: str) -> Any:
        agent = AGENTS[agent_key]
        log.info("agent=%s project=%s task=%s", agent.name, ctx.project["id"], ctx.task["id"])
        return self.llm.structured(
            system=[agent.system, project_profile(ctx.project)],
            prompt=prompt,
            schema=agent.schema,
            effort=self.efforts[agent.effort_key],
        )

    def _save(self, ctx: TaskContext, user: dict[str, Any], kind: str, content: dict[str, Any],
              action: str, model: str | None = None) -> dict[str, Any]:
        art = self.store.add_artifact(ctx.project["id"], ctx.task["id"], kind, content,
                                      model or self.llm.model, user["id"])
        ctx.artifacts[kind] = art
        self.store.audit(username=user["username"], project_id=ctx.project["id"], action=action,
                         input=ctx.instruction or ctx.task["problem"], artifact_id=art["id"],
                         model=art["model"], version=f"{kind} v{art['version']} · app {__version__}",
                         result=f"{kind} v{art['version']} generated")
        return art

    def _say(self, ctx: TaskContext, text: str, **meta: Any) -> dict[str, Any]:
        msg = self.store.add_message(ctx.project["id"], ctx.task["id"], "assistant", "agent", text, meta)
        ctx.messages.append(msg)
        return msg

    def _ctx(self, project_id: str, task_id: str, instruction: str = "") -> TaskContext:
        return load_context(self.store, self.retriever, project_id, task_id, instruction)

    # --- сценарии -------------------------------------------------------------------------

    def start_task(self, project_id: str, task_id: str, user: dict[str, Any]) -> list[dict[str, Any]]:
        """Первое сообщение задачи уже сохранено — начинаем интервью."""
        ctx = self._ctx(project_id, task_id)
        return self._discovery(ctx, user)

    def handle_message(self, project_id: str, task_id: str, user: dict[str, Any], text: str) -> list[dict[str, Any]]:
        ctx = self._ctx(project_id, task_id, instruction="")
        route: RouteDecision = self._call("router", ctx, render(
            ctx, knowledge=False,
            extra=f"<stage>{ctx.task['stage']}</stage>\n<artifacts_ready>{', '.join(sorted(ctx.artifacts)) or 'нет'}</artifacts_ready>",
        ))
        log.info("route intent=%s diagram=%s", route.intent, route.diagram_type)
        instruction = route.instruction
        intent = route.intent
        if intent == "answer_interview":
            return self._discovery(self._ctx(project_id, task_id), user)
        if intent == "question":
            return self._answer(self._ctx(project_id, task_id, text), user)
        if intent == "run_pipeline":
            return self.run_pipeline(project_id, task_id, user, instruction)
        step = {
            "generate_requirements": "requirements", "review_requirements": "review",
            "propose_architecture": "architecture", "review_architecture": "architecture_review",
            "generate_api": "api", "generate_tests": "tests", "final_review": "final_review",
        }.get(intent)
        if intent == "generate_diagram":
            step = route.diagram_type if route.diagram_type != "none" else "sequence"
        if step == "architecture_review" and not instruction:
            instruction = text
        if step is None:
            return self._answer(self._ctx(project_id, task_id, text), user)
        messages = self.run_step(project_id, task_id, user, step, instruction)
        if step == "requirements":
            messages += self.run_step(project_id, task_id, user, "review")
        return messages

    def run_pipeline(self, project_id: str, task_id: str, user: dict[str, Any], instruction: str = "",
                     progress: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        existing = self.store.latest_artifacts(project_id, task_id)
        for step in PIPELINE:
            if step == "requirements" and "requirements" in existing and not instruction:
                continue
            if progress:
                progress(step)
            messages += self.run_step(project_id, task_id, user, step, instruction if step == "requirements" else "")
        return messages

    def run_step(self, project_id: str, task_id: str, user: dict[str, Any], step: str,
                 instruction: str = "") -> list[dict[str, Any]]:
        if step not in STEPS:
            raise StepError(f"Неизвестный шаг {step}")
        messages: list[dict[str, Any]] = []
        ctx = self._ctx(project_id, task_id, instruction)
        if step in NEEDS_REQUIREMENTS and "requirements" not in ctx.artifacts:
            messages += self._requirements(self._ctx(project_id, task_id), user)
            ctx = self._ctx(project_id, task_id, instruction)
        handler = {
            "discovery": self._discovery, "requirements": self._requirements, "review": self._review,
            "architecture": self._architecture, "architecture_review": self._architecture_review,
            "api": self._api, "sequence": self._diagram_step("sequence"),
            "activity": self._diagram_step("activity"), "erd": self._diagram_step("erd"),
            "component": self._component, "tests": self._tests, "final_review": self._final_review,
        }[step]
        messages += handler(ctx, user)
        return messages

    # --- шаги ------------------------------------------------------------------------------

    def _discovery(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        result: DiscoveryResult = self._call("discovery", ctx, render(ctx, endpoints=True))
        art = self._save(ctx, user, "discovery", result.model_dump(), "AI Interview")
        if result.ready or not result.questions:
            self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "ready")
            assumptions = "\n".join(f"- {a}" for a in result.assumptions)
            text = f"{result.context_summary}\n\n" + (f"**Допущения:**\n{assumptions}\n\n" if assumptions else "") \
                + f"**{READY_PROMPT}**"
            return [self._say(ctx, text, artifact_id=art["id"], kind="discovery", ready=True,
                              actions=["requirements", "pipeline"])]
        self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "interview")
        qs = "\n".join(f"{i}. {q.question}" for i, q in enumerate(result.questions, 1))
        first_round = not any(m["meta"].get("kind") == "discovery" for m in ctx.messages if m["role"] == "assistant")
        intro = "Я вижу несколько неопределённых моментов." if first_round else "Спасибо, ответы сохранены. Осталось уточнить:"
        text = (f"{intro}\n\n{qs}\n\nОтветьте одним сообщением, можно не на все вопросы. "
                "Если хотите начать без ответов, напишите «хватит вопросов»: остальное я оформлю как допущения.")
        return [self._say(ctx, text, artifact_id=art["id"], kind="discovery", ready=False,
                          questions=[q.model_dump() for q in result.questions])]

    def _requirements(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        prev = ("requirements",) if ctx.instruction else ()
        doc: RequirementsDoc = self._call("requirements", ctx, render(ctx, artifacts=("discovery",) + prev))
        art = self._save(ctx, user, "requirements", doc.model_dump(), "Generate requirements")
        self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "requirements")
        text = (f"Сформированы требования «{doc.title}»: {len(doc.business_requirements)} BR, "
                f"{len(doc.functional_requirements)} FR, {len(doc.non_functional_requirements)} NFR, "
                f"{len(doc.business_rules)} бизнес-правил, {len(doc.acceptance_criteria)} критериев приёмки, "
                f"{len(doc.user_stories)} user stories, {len(doc.use_cases)} use cases.")
        if doc.open_issues:
            text += "\n\n**Открытые вопросы:**\n" + "\n".join(f"- {q}" for q in doc.open_issues)
        return [self._say(ctx, text, artifact_id=art["id"], kind="requirements")]

    def _review(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        requirements = ctx.artifact("requirements")
        # Если пользователь прислал текст требования — проверяем его, а не документ задачи.
        subject = ctx.instruction.strip()
        if subject or not requirements:
            if not subject:
                raise StepError("Нет требований для проверки: сначала сформируйте требования или пришлите текст.")
            checklist = quick_check(subject)
            extra = f"<requirement_under_review>\n{subject}\n</requirement_under_review>"
            artifacts: tuple[str, ...] = ()
        else:
            checklist = quick_check(requirements_text(requirements))
            extra = ""
            artifacts = ("requirements",)
        extra += f"\n<checklist>\n{checklist}\n</checklist>"
        review: ReviewResult = self._call("review", ctx, render(ctx, artifacts=artifacts, transcript=False, extra=extra))
        completeness = score(requirements, review) if requirements and not subject else None
        content = {"review": review.model_dump(), "checklist": checklist, "score": completeness,
                   "subject": subject}
        art = self._save(ctx, user, "review", content, "Review requirements")
        if not subject:
            self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "review")

        lines = []
        if subject and not checklist["detailed_enough"]:
            lines.append("⚠ Требование недостаточно детализировано.")
        lines.append(review.summary)
        if completeness:
            lines.append(f"```text\nRequirements completeness\n{score_bar(completeness['overall'])} "
                         f"{completeness['overall']}%\n```")
        critical = [f for f in review.findings if f.severity in ("critical", "major")]
        if critical:
            lines.append("**Главные замечания:**\n" + "\n".join(f"- {f.id}: {f.problem}" for f in critical[:8]))
        if subject:
            questions = [q for f in review.findings for q in f.questions] or checklist["questions"]
            if questions:
                lines.append("Не определено:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions[:12], 1)))
        if review.risks:
            lines.append(f"Найдено рисков: {len(review.risks)} (см. вкладку Risks).")
        return [self._say(ctx, "\n\n".join(lines), artifact_id=art["id"], kind="review")]

    def _architecture(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        arch: ArchitectureProposal = self._call(
            "architecture", ctx, render(ctx, artifacts=("requirements",), transcript=False, endpoints=True))
        art = self._save(ctx, user, "architecture", arch.model_dump(), "Propose architecture")
        self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "architecture")
        new = [c.name for c in arch.components if c.is_new]
        text = f"{arch.summary}\n\nКомпонентов: {len(arch.components)}" + (f" (новые: {', '.join(new)})" if new else "") \
            + f", взаимодействий: {len(arch.interactions)}, Kafka topics: {len(arch.topics)}."
        return [self._say(ctx, text, artifact_id=art["id"], kind="architecture")]

    def _architecture_review(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        if not ctx.instruction.strip():
            raise StepError("Опишите архитектурное решение, которое нужно проверить.")
        review: ArchitectureReview = self._call(
            "architecture_review", ctx,
            render(ctx, artifacts=("requirements",), transcript=False,
                   extra=f"<proposed_solution>\n{ctx.instruction}\n</proposed_solution>"))
        art = self._save(ctx, user, "architecture_review", review.model_dump(), "Review architecture")
        issues = "\n".join(f"{i}. {x.title}" for i, x in enumerate(review.issues, 1))
        text = (f"{review.summary}\n\n**Potential issues:**\n{issues}\n\n"
                "_Это рекомендации с обоснованием, а не утверждение архитектуры._")
        return [self._say(ctx, text, artifact_id=art["id"], kind="architecture_review")]

    def _api(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        spec: ApiSpec = self._call(
            "api", ctx, render(ctx, artifacts=("requirements", "architecture"), transcript=False, endpoints=True))
        conflicts = endpoint_conflicts(spec, ctx.endpoints)
        content = {"spec": spec.model_dump(), "openapi": openapi_document(spec), "conflicts": conflicts}
        art = self._save(ctx, user, "api", content, "Generate API")
        self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "api")
        eps = "\n".join(f"- `{e.method} {e.path}` — {e.summary}" for e in spec.endpoints)
        text = f"API-контракт «{spec.title}»:\n{eps}\n\nOpenAPI 3.1 доступен для экспорта."
        if spec.reused_existing:
            text += "\n\nИспользуются существующие API:\n" + "\n".join(f"- {e}" for e in spec.reused_existing)
        if conflicts:
            text += "\n\n⚠ Пересечения с существующими API:\n" + "\n".join(f"- {c}" for c in conflicts)
        return [self._say(ctx, text, artifact_id=art["id"], kind="api")]

    def _diagram_step(self, kind: str) -> Callable[[TaskContext, dict[str, Any]], list[dict[str, Any]]]:
        def run(ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
            api = ctx.artifact("api")
            extra = f"<diagram_type>{kind}</diagram_type>"
            if api:
                listed = "\n".join(f"- {e['method']} {e['path']}" for e in api["spec"]["endpoints"])
                extra += f"\n<api_endpoints>\n{listed}\n</api_endpoints>"
            diagram = self._call(kind, ctx, render(ctx, artifacts=("requirements", "architecture"),
                                                   transcript=False, knowledge=kind == "erd", extra=extra))
            art = self._save(ctx, user, kind, diagram.model_dump(), f"Generate {kind} diagram")
            names = {"sequence": "Sequence diagram", "activity": "Activity / BPMN diagram", "erd": "ERD"}
            return [self._say(ctx, f"{names[kind]} «{diagram.title}» готова (Mermaid и PlantUML).",
                              artifact_id=art["id"], kind=kind)]
        return run

    def _component(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        if "architecture" not in ctx.artifacts:
            return self._architecture(ctx, user)
        art = ctx.artifacts["architecture"]
        return [self._say(ctx, "Component diagram построена по текущему архитектурному решению.",
                          artifact_id=art["id"], kind="architecture")]

    def _tests(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        suite: TestSuite = self._call("tests", ctx, render(ctx, artifacts=("requirements", "api"), transcript=False,
                                                           knowledge=False))
        art = self._save(ctx, user, "test_cases", suite.model_dump(), "Generate test cases")
        self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "tests")
        listing = "\n".join(f"{c.id} {c.title}" for c in suite.test_cases)
        return [self._say(ctx, f"Сгенерировано тестовых сценариев: {len(suite.test_cases)}.\n\n```text\n{listing}\n```",
                          artifact_id=art["id"], kind="test_cases")]

    def _final_review(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        requirements = ctx.artifact("requirements")
        if not requirements:
            raise StepError("Финальная проверка возможна после формирования требований.")
        review = ctx.artifact("review")
        review_obj = review["review"] if review and not review.get("subject") else None
        completeness = score(requirements, review_obj)
        trace = traceability(requirements, (ctx.artifact("api") or {}).get("spec"), ctx.artifact("test_cases"))
        missing = [k for k in ("requirements", "review", "architecture", "api", "sequence", "test_cases")
                   if k not in ctx.artifacts]
        open_findings = [f"{f['id']}: {f['problem']}" for f in (review_obj or {}).get("findings", [])
                         if f["severity"] == "critical"]
        content = {"score": completeness, "traceability": trace, "missing_artifacts": missing,
                   "open_findings": open_findings}
        art = self._save(ctx, user, "final_review", content, "Final review", model="deterministic")
        self.store.set_task_stage(ctx.project["id"], ctx.task["id"], "done" if not missing else "review")
        lines = [f"```text\nRequirements completeness\n{score_bar(completeness['overall'])} {completeness['overall']}%\n```"]
        if trace["rows"]:
            lines.append(f"Покрытие функциональных требований тестами: {trace['coverage']}%.")
        if trace.get("uncovered_by_tests"):
            lines.append("⚠ Без тестов: " + ", ".join(trace["uncovered_by_tests"]))
        if missing:
            lines.append("⚠ Ещё не сформированы: " + ", ".join(missing))
        if open_findings:
            lines.append("⚠ Критичные замечания ревью не закрыты: " + str(len(open_findings)))
        if not missing and not open_findings and not trace.get("uncovered_by_tests"):
            lines.append("Комплект артефактов готов к экспорту. Перед публикацией требований нужно ваше подтверждение.")
        return [self._say(ctx, "\n\n".join(lines), artifact_id=art["id"], kind="final_review")]

    def _answer(self, ctx: TaskContext, user: dict[str, Any]) -> list[dict[str, Any]]:
        kinds = tuple(k for k in ("requirements", "architecture", "api") if k in ctx.artifacts)
        answer: ChatAnswer = self._call("chat", ctx, render(ctx, artifacts=kinds, endpoints=True))
        text = answer.answer + (f"\n\n_Источники: {', '.join(answer.sources)}_" if answer.sources else "")
        return [self._say(ctx, text, kind="answer")]
