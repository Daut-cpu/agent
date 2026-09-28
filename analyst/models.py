"""Структуры, которые возвращают агенты.

Каждая модель используется как JSON-схема structured outputs: модель обязана
вернуть ровно такую структуру. Поэтому все поля обязательные (пустая строка или
пустой список вместо «нет значения»), а вложенные объекты — плоские.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Priority = Literal["must", "should", "could"]
Severity = Literal["critical", "major", "minor", "info"]
Level = Literal["low", "medium", "high"]


# --- AI Interview -----------------------------------------------------------

class Question(BaseModel):
    id: str  # Q1, Q2 …
    category: Literal[
        "business", "scope", "actors", "data", "integration", "errors",
        "security", "nfr", "operations", "other",
    ]
    question: str
    why: str  # зачем это нужно знать
    suggested_answers: list[str]


class ResolvedQuestion(BaseModel):
    question_id: str
    question: str
    answer: str


class DiscoveryResult(BaseModel):
    context_summary: str
    domain: str
    known_facts: list[str]
    assumptions: list[str]
    resolved: list[ResolvedQuestion]
    questions: list[Question]
    ready: bool
    readiness_comment: str


# --- Требования ---------------------------------------------------------------

class Requirement(BaseModel):
    id: str  # BR-1, FR-1, RULE-1
    text: str
    priority: Priority
    source: str  # ответ в интервью, документ или «допущение»


class NonFunctionalRequirement(BaseModel):
    id: str  # NFR-1
    category: Literal[
        "performance", "availability", "reliability", "security", "logging",
        "monitoring", "scalability", "compliance", "usability", "other",
    ]
    text: str
    metric: str  # измеримый критерий: «p95 < 500 мс»
    priority: Priority


class AcceptanceCriterion(BaseModel):
    id: str  # AC-1
    requirement_refs: list[str]
    given: str
    when: str
    then: str


class UserStory(BaseModel):
    id: str  # US-1
    as_a: str
    i_want: str
    so_that: str
    acceptance_refs: list[str]


class UseCase(BaseModel):
    id: str  # UC-1
    name: str
    actor: str
    preconditions: list[str]
    main_flow: list[str]
    alternative_flows: list[str]
    postconditions: list[str]


class GlossaryTerm(BaseModel):
    term: str
    definition: str


class RequirementsDoc(BaseModel):
    title: str
    summary: str
    business_requirements: list[Requirement]
    functional_requirements: list[Requirement]
    non_functional_requirements: list[NonFunctionalRequirement]
    business_rules: list[Requirement]
    acceptance_criteria: list[AcceptanceCriterion]
    user_stories: list[UserStory]
    use_cases: list[UseCase]
    glossary: list[GlossaryTerm]
    assumptions: list[str]
    open_issues: list[str]


# --- Review -------------------------------------------------------------------

FindingCategory = Literal[
    "ambiguity", "contradiction", "missing_scenario", "error_handling", "edge_case",
    "nfr", "security", "logging", "monitoring", "idempotency", "timeout_retry",
    "fault_tolerance", "other",
]


class ReviewFinding(BaseModel):
    id: str  # RV-1
    severity: Severity
    category: FindingCategory
    requirement_refs: list[str]
    problem: str
    questions: list[str]
    recommendation: str


class Risk(BaseModel):
    id: str  # RISK-1
    title: str
    description: str
    probability: Level
    impact: Level
    mitigation: str


class ReviewResult(BaseModel):
    summary: str
    findings: list[ReviewFinding]
    risks: list[Risk]


# --- Архитектура ----------------------------------------------------------------

class Component(BaseModel):
    name: str
    kind: Literal["frontend", "gateway", "service", "database", "queue", "cache", "external", "other"]
    purpose: str
    technology: str
    is_new: bool


class Interaction(BaseModel):
    source: str
    target: str
    protocol: str  # REST, gRPC, Kafka, SQL …
    mode: Literal["sync", "async"]
    description: str


class Topic(BaseModel):
    name: str
    producer: str
    consumers: list[str]
    payload: str


class DataStore(BaseModel):
    name: str
    technology: str
    owner: str
    entities: list[str]


class Decision(BaseModel):
    title: str
    decision: str
    rationale: str
    alternatives: list[str]


class ArchitectureProposal(BaseModel):
    summary: str
    components: list[Component]
    interactions: list[Interaction]
    topics: list[Topic]
    datastores: list[DataStore]
    external_systems: list[str]
    decisions: list[Decision]
    risks: list[Risk]


class ArchitectureIssue(BaseModel):
    id: str  # AR-1
    severity: Severity
    category: Literal[
        "single_point_of_failure", "retry", "transactions", "idempotency", "sla",
        "security", "scalability", "consistency", "observability", "coupling", "other",
    ]
    title: str
    rationale: str
    recommendation: str


class ArchitectureReview(BaseModel):
    summary: str
    issues: list[ArchitectureIssue]
    strengths: list[str]
    questions: list[str]


# --- API ---------------------------------------------------------------------------

FieldType = Literal["string", "integer", "number", "boolean", "object", "array"]


class ApiField(BaseModel):
    # Вложенность — через точку, массивы — через []: "card.id", "items[].amount".
    name: str
    type: FieldType
    format: str  # uuid, date-time, int64 … или ""
    required: bool
    description: str
    example: str
    validation: str  # «> 0», «^[0-9]{16}$», «max 255» или ""


class ApiHeader(BaseModel):
    name: str
    required: bool
    description: str
    example: str


class ApiResponse(BaseModel):
    status: int
    description: str
    fields: list[ApiField]
    example_json: str


class Endpoint(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    operation_id: str
    summary: str
    description: str
    tags: list[str]
    authentication: str
    authorization: str
    idempotency: str
    headers: list[ApiHeader]
    path_params: list[ApiField]
    query_params: list[ApiField]
    request_fields: list[ApiField]
    request_example_json: str
    responses: list[ApiResponse]
    validation_rules: list[str]


class ErrorCode(BaseModel):
    code: str  # PAYMENT_NOT_FOUND
    http_status: int
    description: str


class ApiSpec(BaseModel):
    title: str
    version: str
    base_path: str
    summary: str
    security_scheme: Literal["bearer", "oauth2", "apiKey", "mtls"]
    endpoints: list[Endpoint]
    error_model_fields: list[ApiField]
    error_codes: list[ErrorCode]
    reused_existing: list[str]  # существующие API, на которые опирается решение
    notes: list[str]


# --- Диаграммы -------------------------------------------------------------------

class Participant(BaseModel):
    id: str
    label: str
    kind: Literal["actor", "participant", "database", "queue", "external"]


class SequenceStep(BaseModel):
    # message: source → target; note: text над source;
    # group_start / group_else / group_end: блоки alt/opt/loop/par.
    kind: Literal["message", "note", "group_start", "group_else", "group_end"]
    source: str
    target: str
    text: str
    arrow: Literal["sync", "async", "reply", "none"]
    group_type: Literal["alt", "opt", "loop", "par", "none"]


class SequenceDiagram(BaseModel):
    title: str
    participants: list[Participant]
    steps: list[SequenceStep]


class FlowNode(BaseModel):
    id: str
    kind: Literal["start", "end", "task", "decision", "event", "subprocess"]
    label: str
    lane: str  # исполнитель (для BPMN-дорожек) или ""


class FlowEdge(BaseModel):
    source: str
    target: str
    label: str


class ActivityDiagram(BaseModel):
    title: str
    nodes: list[FlowNode]
    edges: list[FlowEdge]


class EntityAttribute(BaseModel):
    name: str
    type: str
    key: Literal["PK", "FK", "UK", ""]
    description: str


class Entity(BaseModel):
    name: str
    attributes: list[EntityAttribute]


class Relation(BaseModel):
    left: str
    right: str
    cardinality: Literal["one-to-one", "one-to-many", "many-to-one", "many-to-many"]
    label: str


class ERDiagram(BaseModel):
    title: str
    entities: list[Entity]
    relations: list[Relation]


# --- QA ---------------------------------------------------------------------------

class TestCase(BaseModel):
    __test__ = False  # не путать pytest

    id: str  # TC-001
    title: str
    type: Literal["positive", "negative", "edge", "security", "performance", "resilience"]
    requirement_refs: list[str]
    preconditions: list[str]
    steps: list[str]
    expected_result: str
    priority: Literal["critical", "high", "medium", "low"]


class TestSuite(BaseModel):
    __test__ = False

    summary: str
    test_cases: list[TestCase]
    coverage_notes: list[str]


# --- Оркестратор и чат -------------------------------------------------------------

Intent = Literal[
    "answer_interview", "generate_requirements", "review_requirements",
    "propose_architecture", "review_architecture", "generate_api", "generate_diagram",
    "generate_tests", "run_pipeline", "final_review", "question",
]


class RouteDecision(BaseModel):
    intent: Intent
    diagram_type: Literal["sequence", "activity", "erd", "component", "none"]
    instruction: str  # уточнение пользователя для агента-исполнителя
    reason: str


class ChatAnswer(BaseModel):
    answer: str
    sources: list[str]
