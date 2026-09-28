"""Фейковый LLM и типовые ответы агентов для сценария «возврат платежа»."""

from __future__ import annotations

import json
from typing import Any

from analyst.models import (
    ActivityDiagram, ApiSpec, ArchitectureProposal, ArchitectureReview, ChatAnswer, DiscoveryResult, ERDiagram,
    RequirementsDoc, ReviewResult, RouteDecision, SequenceDiagram, TestSuite,
)


def q(i, text, cat="business"):
    return {"id": f"Q{i}", "category": cat, "question": text, "why": "влияет на сценарии",
            "suggested_answers": ["да", "нет"]}


DISCOVERY_OPEN = {
    "context_summary": "Возврат платежа клиенту, в том числе на другую карту.",
    "domain": "Платежи",
    "known_facts": ["Есть Payment Service"],
    "assumptions": [],
    "resolved": [],
    "questions": [q(1, "Кто инициирует возврат?", "actors"), q(2, "Можно ли вернуть частичную сумму?"),
                  q(3, "Что происходит при повторном запросе?", "errors")],
    "ready": False,
    "readiness_comment": "Нужны ответы на ключевые вопросы",
}

DISCOVERY_READY = {
    **DISCOVERY_OPEN,
    "resolved": [{"question_id": "Q1", "question": "Кто инициирует возврат?", "answer": "Оператор"}],
    "questions": [],
    "assumptions": ["Timeout банка — 30 секунд"],
    "ready": True,
    "readiness_comment": "Достаточно для требований",
}


def req(i, text, prefix="FR"):
    return {"id": f"{prefix}-{i}", "text": text, "priority": "must", "source": "интервью"}


REQUIREMENTS = {
    "title": "Возврат платежа на другую карту",
    "summary": "Оператор оформляет возврат платежа клиенту.",
    "business_requirements": [req(1, "Сократить время возврата", "BR")],
    "functional_requirements": [
        req(1, "Система должна принимать запрос на возврат через API POST /api/v1/refunds"),
        req(2, "При повторном запросе с тем же Idempotency-Key система должна вернуть исходный результат"),
        req(3, "При timeout банка (30 секунд) система должна перевести возврат в статус PENDING и выполнить retry"),
    ],
    "non_functional_requirements": [
        {"id": "NFR-1", "category": "performance", "text": "Время ответа API", "metric": "p95 < 500 мс",
         "priority": "must"},
        {"id": "NFR-2", "category": "logging", "text": "Логировать каждую операцию с requestId, без PAN",
         "metric": "100% операций", "priority": "must"},
    ],
    "business_rules": [req(1, "Сумма возврата не больше суммы платежа", "RULE")],
    "acceptance_criteria": [
        {"id": "AC-1", "requirement_refs": ["FR-1"], "given": "платёж проведён", "when": "оператор создаёт возврат",
         "then": "возврат в статусе PROCESSING"},
        {"id": "AC-2", "requirement_refs": ["FR-2"], "given": "возврат создан", "when": "повторный запрос",
         "then": "возвращается тот же refundId"},
    ],
    "user_stories": [{"id": "US-1", "as_a": "оператор", "i_want": "вернуть деньги на другую карту",
                      "so_that": "клиент получил средства", "acceptance_refs": ["AC-1"]}],
    "use_cases": [{"id": "UC-1", "name": "Возврат", "actor": "Оператор", "preconditions": ["Платёж проведён"],
                   "main_flow": ["Оператор вводит сумму", "Система отправляет запрос в банк"],
                   "alternative_flows": ["Банк недоступен — ошибка 503"], "postconditions": ["Возврат создан"]}],
    "glossary": [{"term": "Возврат", "definition": "Refund"}],
    "assumptions": ["Банк поддерживает возврат на другую карту"],
    "open_issues": ["Нужен ли лимит на количество возвратов?"],
}

REVIEW = {
    "summary": "Требования в целом полные, но не хватает мониторинга.",
    "findings": [
        {"id": "RV-1", "severity": "major", "category": "monitoring", "requirement_refs": ["FR-3"],
         "problem": "Не определены метрики и алерты", "questions": ["Какие метрики нужны?"],
         "recommendation": "Добавить метрики refund_total и refund_errors"},
        {"id": "RV-2", "severity": "critical", "category": "security", "requirement_refs": ["FR-1"],
         "problem": "Не описаны роли", "questions": ["Кто имеет право делать возврат?"],
         "recommendation": "Добавить RBAC"},
    ],
    "risks": [{"id": "RISK-1", "title": "Двойной возврат", "description": "Повтор при timeout",
               "probability": "medium", "impact": "high", "mitigation": "Идемпотентность"}],
}

ARCHITECTURE = {
    "summary": "Новый Refund Service за API Gateway.",
    "components": [
        {"name": "Frontend", "kind": "frontend", "purpose": "UI оператора", "technology": "React", "is_new": False},
        {"name": "API Gateway", "kind": "gateway", "purpose": "Маршрутизация", "technology": "Kong", "is_new": False},
        {"name": "Refund Service", "kind": "service", "purpose": "Возвраты", "technology": "Java", "is_new": True},
        {"name": "Payment Service", "kind": "service", "purpose": "Платежи", "technology": "Java", "is_new": False},
        {"name": "Kafka", "kind": "queue", "purpose": "События", "technology": "Kafka", "is_new": False},
        {"name": "PostgreSQL", "kind": "database", "purpose": "Хранение", "technology": "PostgreSQL 15",
         "is_new": False},
    ],
    "interactions": [
        {"source": "Frontend", "target": "API Gateway", "protocol": "REST", "mode": "sync", "description": ""},
        {"source": "API Gateway", "target": "Refund Service", "protocol": "REST", "mode": "sync", "description": ""},
        {"source": "Refund Service", "target": "Payment Service", "protocol": "REST", "mode": "sync",
         "description": "проверка платежа"},
        {"source": "Refund Service", "target": "Kafka", "protocol": "Kafka", "mode": "async",
         "description": "refund.created"},
        {"source": "Refund Service", "target": "PostgreSQL", "protocol": "SQL", "mode": "sync", "description": ""},
    ],
    "topics": [{"name": "refund.status-changed", "producer": "Refund Service", "consumers": ["Notification"],
                "payload": "refundId, status"}],
    "datastores": [{"name": "refund_db", "technology": "PostgreSQL", "owner": "Refund Service",
                    "entities": ["refund"]}],
    "external_systems": ["Bank"],
    "decisions": [{"title": "Outbox", "decision": "Transactional outbox", "rationale": "Атомарность",
                   "alternatives": ["2PC"]}],
    "risks": [],
}


def field(name, type_="string", required=True, example=""):
    return {"name": name, "type": type_, "format": "", "required": required, "description": name,
            "example": example, "validation": ""}


API = {
    "title": "Refund API", "version": "1.0.0", "base_path": "/api/v1", "summary": "Возвраты",
    "security_scheme": "bearer",
    "endpoints": [{
        "method": "POST", "path": "/refunds", "operation_id": "createRefund", "summary": "Создать возврат",
        "description": "", "tags": ["refunds"], "authentication": "Bearer JWT", "authorization": "role REFUND_OPERATOR",
        "idempotency": "Idempotency-Key header",
        "headers": [{"name": "Idempotency-Key", "required": True, "description": "UUID", "example": "7c9e..."}],
        "path_params": [], "query_params": [],
        "request_fields": [field("paymentId", example="123456"), field("amount", "integer", example="1000"),
                           field("targetCard.id", example="987654")],
        "request_example_json": json.dumps({"paymentId": "123456", "amount": 1000, "targetCard": {"id": "987654"}}),
        "responses": [
            {"status": 202, "description": "Принято", "fields": [field("refundId"), field("status")],
             "example_json": json.dumps({"refundId": "456789", "status": "PROCESSING"})},
            {"status": 409, "description": "Конфликт", "fields": [], "example_json": ""},
        ],
        "validation_rules": ["amount > 0"],
    }, {
        "method": "GET", "path": "/payments/{paymentId}", "operation_id": "getPayment", "summary": "Платёж",
        "description": "", "tags": [], "authentication": "", "authorization": "", "idempotency": "",
        "headers": [], "path_params": [field("paymentId")], "query_params": [], "request_fields": [],
        "request_example_json": "", "responses": [], "validation_rules": [],
    }],
    "error_model_fields": [field("code"), field("message")],
    "error_codes": [{"code": "PAYMENT_NOT_FOUND", "http_status": 404, "description": "нет платежа"}],
    "reused_existing": [],
    "notes": [],
}


def step(kind, source="", target="", text="", arrow="none", group="none"):
    return {"kind": kind, "source": source, "target": target, "text": text, "arrow": arrow, "group_type": group}


SEQUENCE = {
    "title": "Возврат платежа",
    "participants": [{"id": "Client", "label": "Client", "kind": "actor"},
                     {"id": "RefundService", "label": "Refund Service", "kind": "participant"},
                     {"id": "Bank", "label": "Bank", "kind": "external"}],
    "steps": [
        step("message", "Client", "RefundService", "POST /refunds", "sync"),
        step("group_start", text="банк ответил", group="alt"),
        step("message", "RefundService", "Bank", "refund", "sync"),
        step("group_else", text="timeout"),
        step("note", "RefundService", text="retry; статус PENDING"),
        step("group_end"),
        step("message", "RefundService", "Client", "202 PROCESSING", "reply"),
    ],
}

ACTIVITY = {
    "title": "Процесс возврата",
    "nodes": [{"id": "s", "kind": "start", "label": "Старт", "lane": ""},
              {"id": "t1", "kind": "task", "label": "Проверить платёж", "lane": "Refund Service"},
              {"id": "d1", "kind": "decision", "label": "Платёж найден?", "lane": "Refund Service"},
              {"id": "e", "kind": "end", "label": "Конец", "lane": ""}],
    "edges": [{"source": "s", "target": "t1", "label": ""}, {"source": "t1", "target": "d1", "label": ""},
              {"source": "d1", "target": "e", "label": "нет"}],
}

ERD = {
    "title": "Refund ERD",
    "entities": [{"name": "payment", "attributes": [{"name": "id", "type": "uuid", "key": "PK", "description": ""}]},
                 {"name": "refund", "attributes": [
                     {"name": "id", "type": "uuid", "key": "PK", "description": ""},
                     {"name": "payment_id", "type": "uuid", "key": "FK", "description": "платёж"}]}],
    "relations": [{"left": "payment", "right": "refund", "cardinality": "one-to-many", "label": "refunds"}],
}

TESTS = {
    "summary": "Тесты возврата",
    "test_cases": [
        {"id": "TC-001", "title": "Successful refund", "type": "positive", "requirement_refs": ["FR-1"],
         "preconditions": ["Платёж проведён"], "steps": ["POST /refunds"], "expected_result": "202 PROCESSING",
         "priority": "critical"},
        {"id": "TC-004", "title": "Duplicate request", "type": "resilience", "requirement_refs": ["FR-2"],
         "preconditions": [], "steps": ["Повторить запрос"], "expected_result": "тот же refundId",
         "priority": "high"},
    ],
    "coverage_notes": [],
}

ARCH_REVIEW = {
    "summary": "Есть риски отказоустойчивости.",
    "issues": [{"id": "AR-1", "severity": "major", "category": "single_point_of_failure",
                "title": "Service A является single point of failure", "rationale": "Один экземпляр",
                "recommendation": "Несколько реплик"}],
    "strengths": ["Простота"],
    "questions": ["Какой SLA?"],
}

CANNED: dict[type, Any] = {
    RequirementsDoc: REQUIREMENTS, ReviewResult: REVIEW, ArchitectureProposal: ARCHITECTURE, ApiSpec: API,
    SequenceDiagram: SEQUENCE, ActivityDiagram: ACTIVITY, ERDiagram: ERD, TestSuite: TESTS,
    ArchitectureReview: ARCH_REVIEW, ChatAnswer: {"answer": "В проекте есть Payment API.", "sources": ["api.yaml"]},
}


class FakeLLM:
    """Возвращает заготовки по схеме. Discovery и router управляются очередями."""

    model = "fake-model"

    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.discovery = [DISCOVERY_OPEN, DISCOVERY_READY]
        self.routes: list[dict[str, Any]] = []

    def route(self, intent, diagram_type="none", instruction=""):
        self.routes.append({"intent": intent, "diagram_type": diagram_type, "instruction": instruction,
                            "reason": "test"})

    def structured(self, *, system, prompt, schema, effort="medium"):
        self.calls.append({"system": system, "prompt": prompt, "schema": schema.__name__, "effort": effort})
        if schema is DiscoveryResult:
            data = self.discovery.pop(0) if len(self.discovery) > 1 else self.discovery[0]
        elif schema is RouteDecision:
            data = self.routes.pop(0)
        else:
            data = CANNED[schema]
        return schema.model_validate(data)
