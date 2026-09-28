"""Детерминированная проверка требований и Requirements Score.

Работает без LLM: ищет в тексте требований упоминания обязательных аспектов
(timeout, retry, идемпотентность, логирование…) и размытые формулировки.
Результат дополняет ревью агента и служит основой для показателя полноты.
Score — вспомогательный индикатор, а не формальная оценка качества.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import RequirementsDoc, ReviewResult


@dataclass(frozen=True)
class Aspect:
    key: str
    title: str
    keywords: tuple[str, ...]
    questions: tuple[str, ...]
    weight: float = 1.0
    finding_categories: tuple[str, ...] = field(default=())


ASPECTS: tuple[Aspect, ...] = (
    Aspect(
        "business", "Business logic",
        ("должен", "должна", "должно", "must", "shall", "правил", "rule", "статус", "status", "сценари", "scenario"),
        ("Кто инициирует операцию и при каких условиях она разрешена?",
         "Какие статусы проходит операция и что видит пользователь на каждом шаге?"),
        weight=1.5, finding_categories=("ambiguity", "contradiction", "missing_scenario"),
    ),
    Aspect(
        "api", "API",
        ("api", "endpoint", "эндпоинт", "http", "rest", "grpc", "request", "response", "запрос", "ответ", "контракт"),
        ("Какой контракт у операции: метод, путь, поля запроса и ответа?",
         "Какой статус возвращается клиенту сразу после запроса?"),
    ),
    Aspect(
        "errors", "Error handling",
        ("ошибк", "error", "исключен", "exception", "отказ", "fail", "недоступ", "unavailable", "4xx", "5xx",
         "отклон", "reject"),
        ("Что происходит, если внешняя система вернула ошибку или недоступна?",
         "Какие коды ошибок и сообщения получает клиент?"),
        weight=1.3, finding_categories=("error_handling",),
    ),
    Aspect(
        "timeout_retry", "Timeout / retry",
        ("timeout", "тайм-аут", "таймаут", "время ожидания", "retry", "повторн", "ретра", "backoff", "попыт"),
        ("Какой timeout у вызовов внешних систем?", "Нужен ли retry, сколько попыток и с какой задержкой?",
         "Что делать при timeout: считать операцию неуспешной или проверять статус?"),
        finding_categories=("timeout_retry",),
    ),
    Aspect(
        "idempotency", "Idempotency",
        ("идемпотент", "idempot", "дублир", "дубл", "duplicate", "повторный запрос", "idempotency-key",
         "request id", "requestid"),
        ("Что происходит при повторном запросе?", "Как определить повторный запрос (ключ идемпотентности)?"),
        finding_categories=("idempotency",),
    ),
    Aspect(
        "security", "Security",
        ("авториз", "аутентиф", "auth", "oauth", "jwt", "токен", "token", "шифр", "encrypt", "pci", "rbac",
         "роль", "прав доступ", "маскир", "персональн", "pii", "tls"),
        ("Кто имеет право выполнять операцию (роли, scopes)?",
         "Какие данные чувствительные и как они защищаются (маскирование, шифрование, PCI DSS)?"),
        weight=1.3, finding_categories=("security",),
    ),
    Aspect(
        "logging", "Logging",
        ("лог", "log", "журнал", "аудит", "audit", "трассир", "trace"),
        ("Какие события и поля логируются? Что запрещено писать в лог?",
         "Нужен ли аудит действий пользователя?"),
        finding_categories=("logging",),
    ),
    Aspect(
        "monitoring", "Monitoring",
        ("монитор", "метрик", "metric", "alert", "алерт", "dashboard", "дашборд", "prometheus", "grafana",
         "sla", "slo"),
        ("Какие метрики нужны (количество, ошибки, latency)?",
         "Какие алерты и пороги срабатывания?", "Как отслеживать результат операции?"),
        finding_categories=("monitoring",),
    ),
    Aspect(
        "nfr", "NFR",
        ("rps", "tps", "latency", "задержк", "производительн", "нагрузк", " мс", " ms", "секунд", "p95", "p99",
         "доступност", "availability", "99."),
        ("Какая ожидаемая нагрузка (RPS) и допустимое время ответа (p95)?",
         "Какая требуемая доступность сервиса?"),
        finding_categories=("nfr",),
    ),
    Aspect(
        "fault_tolerance", "Fault tolerance",
        ("отказоустойч", "fallback", "circuit", "резерв", "реплик", "replica", "деградац", "degrad",
         "восстановл", "recovery", "компенсац", "saga", "outbox"),
        ("Как система ведёт себя при отказе зависимого сервиса?",
         "Как восстанавливается согласованность данных после сбоя?"),
        finding_categories=("fault_tolerance",),
    ),
    Aspect(
        "edge_cases", "Edge cases",
        ("граничн", "edge", "пуст", "нулев", "максимальн", "минимальн", "частичн", "partial", "лимит", "limit",
         "превыша", "одновремен", "concurren"),
        ("Что при частичной сумме, нулевой или превышающей допустимую?",
         "Что при одновременных запросах по одному объекту?"),
        finding_categories=("edge_case",),
    ),
)

VAGUE_TERMS = (
    "быстро", "быстрый", "удобн", "и т.д", "и т. д", "и тд", "etc", "как можно", "примерно", "оптимальн",
    "достаточн", "некоторые", "при необходимости", "по возможности", "user-friendly", "fast", "appropriate",
    "адекватн", "эффективн", "гибк", "надёжн", "надежн", "современн", "интуитивн", "своевременн",
)

SEVERITY_PENALTY = {"critical": 30, "major": 15, "minor": 5, "info": 0}


def requirements_text(doc: RequirementsDoc | dict[str, Any]) -> str:
    if isinstance(doc, dict):
        doc = RequirementsDoc.model_validate(doc)
    parts = [doc.title, doc.summary]
    for group in (doc.business_requirements, doc.functional_requirements, doc.business_rules):
        parts += [r.text for r in group]
    parts += [f"{n.category} {n.text} {n.metric}" for n in doc.non_functional_requirements]
    parts += [f"{a.given} {a.when} {a.then}" for a in doc.acceptance_criteria]
    parts += [f"{s.as_a} {s.i_want} {s.so_that}" for s in doc.user_stories]
    for uc in doc.use_cases:
        parts += uc.main_flow + uc.alternative_flows + uc.preconditions + uc.postconditions
    return "\n".join(parts)


def _mentions(text: str, aspect: Aspect) -> int:
    lowered = f" {text.lower()} "
    return sum(lowered.count(k) for k in aspect.keywords)


def vague_statements(text: str) -> list[tuple[str, str]]:
    """(термин, предложение) для размытых формулировок."""
    found = []
    for sentence in re.split(r"(?<=[.!?\n])\s+", text):
        low = sentence.lower()
        for term in VAGUE_TERMS:
            if re.search(r"(?<!\w)" + re.escape(term), low):
                found.append((term, sentence.strip()))
                break
    return found


def quick_check(text: str) -> dict[str, Any]:
    """Проверка произвольного текста требования без LLM.

    Пример: «Сервис должен отправлять запрос на возврат платежа.» → не определены
    timeout, retry, идемпотентность, логирование, мониторинг…
    """
    missing = [a for a in ASPECTS if a.key != "business" and not _mentions(text, a)]
    questions = [q for a in missing for q in a.questions]
    vague = vague_statements(text)
    return {
        "detailed_enough": not missing and not vague,
        "missing_aspects": [a.title for a in missing],
        "questions": questions,
        "vague": [{"term": t, "sentence": s} for t, s in vague],
    }


def score(doc: RequirementsDoc | dict[str, Any], review: ReviewResult | dict[str, Any] | None = None) -> dict[str, Any]:
    """Показатель полноты требований по аспектам.

    Аспект получает базу за упоминания в тексте требований (0 упоминаний — 20%,
    1 — 70%, 2+ — 100%) и штраф за открытые замечания ревью этой категории.
    """
    text = requirements_text(doc)
    if isinstance(review, dict):
        review = ReviewResult.model_validate(review)
    findings = review.findings if review else []

    rows = []
    for aspect in ASPECTS:
        hits = _mentions(text, aspect)
        base = 20 if hits == 0 else 70 if hits == 1 else 100
        penalty = sum(
            SEVERITY_PENALTY[f.severity] for f in findings if f.category in aspect.finding_categories
        )
        value = max(0, min(100, base - penalty))
        rows.append({"key": aspect.key, "title": aspect.title, "score": value, "mentions": hits,
                     "weight": aspect.weight})

    total_weight = sum(r["weight"] for r in rows)
    overall = round(sum(r["score"] * r["weight"] for r in rows) / total_weight)
    missing = [
        f"{r['title']}: не описано" if r["mentions"] == 0 else f"{r['title']}: есть замечания ревью"
        for r in rows if r["score"] < 60
    ]
    return {
        "overall": overall,
        "aspects": rows,
        "missing": missing,
        "vague": [{"term": t, "sentence": s} for t, s in vague_statements(text)][:20],
        "disclaimer": "Score — вспомогательный индикатор полноты, а не формальная оценка качества требований.",
    }


def score_bar(value: int, width: int = 10) -> str:
    filled = round(value / 100 * width)
    return "█" * filled + "░" * (width - filled)


def traceability(requirements: dict[str, Any] | None, api: dict[str, Any] | None,
                 tests: dict[str, Any] | None) -> dict[str, Any]:
    """Матрица трассировки: какие требования покрыты критериями приёмки и тестами."""
    if not requirements:
        return {"rows": [], "uncovered_by_tests": [], "uncovered_by_ac": [], "coverage": 0}
    doc = RequirementsDoc.model_validate(requirements)
    ids = [r.id for r in doc.business_requirements + doc.functional_requirements + doc.business_rules]
    ids += [n.id for n in doc.non_functional_requirements]
    ac_refs = {ref for ac in doc.acceptance_criteria for ref in ac.requirement_refs}
    test_refs: dict[str, list[str]] = {}
    for tc in (tests or {}).get("test_cases", []):
        for ref in tc.get("requirement_refs", []):
            test_refs.setdefault(ref, []).append(tc["id"])
    rows = [{"requirement": rid, "acceptance": rid in ac_refs, "tests": test_refs.get(rid, [])} for rid in ids]
    fr_ids = [r.id for r in doc.functional_requirements]
    covered = [rid for rid in fr_ids if test_refs.get(rid)]
    return {
        "rows": rows,
        "uncovered_by_tests": [rid for rid in fr_ids if not test_refs.get(rid)],
        "uncovered_by_ac": [r.id for r in doc.functional_requirements if r.id not in ac_refs],
        "coverage": round(100 * len(covered) / len(fr_ids)) if fr_ids else 0,
        "api_endpoints": [f"{e['method']} {e['path']}" for e in (api or {}).get("endpoints", [])],
    }
