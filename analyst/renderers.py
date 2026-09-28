"""Преобразование структурированных артефактов в Mermaid, PlantUML, OpenAPI и Markdown.

Агенты возвращают данные, а текст диаграмм и спецификаций строится здесь
детерминированно: синтаксис всегда корректен и одинаков для обоих форматов.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from .checklist import score_bar
from .models import (
    ActivityDiagram, ApiField, ApiSpec, ArchitectureProposal, ArchitectureReview, DiscoveryResult,
    ERDiagram, RequirementsDoc, ReviewResult, SequenceDiagram, TestSuite,
)


def _id(value: str) -> str:
    ident = re.sub(r"\W+", "_", value, flags=re.UNICODE).strip("_")
    return ident if ident and not ident[0].isdigit() else f"n_{ident}"


def _label(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").strip()


def _mm_text(value: str) -> str:
    # В тексте сообщений Mermaid «;» и «#» имеют особый смысл.
    return _label(value).replace(";", ",").replace("#", "№")


# --- Sequence -------------------------------------------------------------------------

def sequence_mermaid(d: SequenceDiagram) -> str:
    lines = ["sequenceDiagram", "    autonumber"]
    for p in d.participants:
        keyword = "actor" if p.kind == "actor" else "participant"
        lines.append(f"    {keyword} {_id(p.id)} as {_mm_text(p.label)}")
    arrows = {"sync": "->>", "async": "-)", "reply": "-->>", "none": "->>"}
    indent = 1
    for s in d.steps:
        pad = "    " * indent
        if s.kind == "message":
            lines.append(f"{pad}{_id(s.source)}{arrows[s.arrow]}{_id(s.target)}: {_mm_text(s.text)}")
        elif s.kind == "note":
            lines.append(f"{pad}Note over {_id(s.source)}: {_mm_text(s.text)}")
        elif s.kind == "group_start":
            group = s.group_type if s.group_type != "none" else "opt"
            lines.append(f"{pad}{group} {_mm_text(s.text)}")
            indent += 1
        elif s.kind == "group_else":
            lines.append(f"{'    ' * max(1, indent - 1)}else {_mm_text(s.text)}")
        elif s.kind == "group_end" and indent > 1:
            indent -= 1
            lines.append(f"{'    ' * indent}end")
    while indent > 1:
        indent -= 1
        lines.append(f"{'    ' * indent}end")
    return "\n".join(lines)


def sequence_plantuml(d: SequenceDiagram) -> str:
    kinds = {"actor": "actor", "participant": "participant", "database": "database",
             "queue": "queue", "external": "boundary"}
    lines = ["@startuml", f"title {_label(d.title)}", "autonumber"]
    for p in d.participants:
        lines.append(f'{kinds[p.kind]} "{_label(p.label)}" as {_id(p.id)}')
    arrows = {"sync": "->", "async": "->>", "reply": "-->", "none": "->"}
    depth = 0
    for s in d.steps:
        if s.kind == "message":
            lines.append(f"{_id(s.source)} {arrows[s.arrow]} {_id(s.target)}: {_label(s.text)}")
        elif s.kind == "note":
            lines.append(f"note over {_id(s.source)}: {_label(s.text)}")
        elif s.kind == "group_start":
            group = {"alt": "alt", "opt": "opt", "loop": "loop", "par": "par"}.get(s.group_type, "group")
            lines.append(f"{group} {_label(s.text)}")
            depth += 1
        elif s.kind == "group_else":
            lines.append(f"else {_label(s.text)}")
        elif s.kind == "group_end" and depth:
            depth -= 1
            lines.append("end")
    lines += ["end"] * depth + ["@enduml"]
    return "\n".join(lines)


# --- Activity / BPMN ---------------------------------------------------------------

def activity_mermaid(d: ActivityDiagram) -> str:
    shapes = {"start": '(("{}"))', "end": '((("{}")))', "task": '["{}"]', "decision": '{{"{}"}}',
              "event": '>"{}"]', "subprocess": '[["{}"]]'}
    lines = ["flowchart TD"]
    lanes: dict[str, list[str]] = {}
    for n in d.nodes:
        node = f"{_id(n.id)}{shapes[n.kind].format(_label(n.label))}"
        lanes.setdefault(n.lane, []).append(node)
    for lane, nodes in lanes.items():
        if lane:
            lines.append(f'    subgraph {_id("lane_" + lane)}["{_label(lane)}"]')
            lines += [f"        {n}" for n in nodes]
            lines.append("    end")
        else:
            lines += [f"    {n}" for n in nodes]
    for e in d.edges:
        label = f"|{_label(e.label)}|" if e.label else ""
        lines.append(f"    {_id(e.source)} -->{label} {_id(e.target)}")
    return "\n".join(lines)


def activity_plantuml(d: ActivityDiagram) -> str:
    lines = ["@startuml", f"title {_label(d.title)}"]
    for n in d.nodes:
        if n.kind == "start":
            continue
        if n.kind == "end":
            continue
        stereo = " <<choice>>" if n.kind == "decision" else ""
        if stereo:
            lines.append(f"state {_id(n.id)}{stereo}")
            lines.append(f"note right of {_id(n.id)}: {_label(n.label)}")
        else:
            lines.append(f'state "{_label(n.label)}" as {_id(n.id)}')
    ends = {n.id for n in d.nodes if n.kind in ("start", "end")}
    for e in d.edges:
        src = "[*]" if e.source in ends else _id(e.source)
        dst = "[*]" if e.target in ends else _id(e.target)
        label = f" : {_label(e.label)}" if e.label else ""
        lines.append(f"{src} --> {dst}{label}")
    lines.append("@enduml")
    return "\n".join(lines)


# --- ERD ----------------------------------------------------------------------------------

_CARD_MM = {"one-to-one": "||--||", "one-to-many": "||--o{", "many-to-one": "}o--||", "many-to-many": "}o--o{"}
_CARD_PU = {"one-to-one": "||--||", "one-to-many": "||--o{", "many-to-one": "}o--||", "many-to-many": "}o--o{"}


def erd_mermaid(d: ERDiagram) -> str:
    lines = ["erDiagram"]
    for e in d.entities:
        lines.append(f"    {_id(e.name)} {{")
        for a in e.attributes:
            key = f" {a.key}" if a.key else ""
            comment = f' "{_label(a.description)}"' if a.description else ""
            lines.append(f"        {_id(a.type) or 'string'} {_id(a.name)}{key}{comment}")
        lines.append("    }")
    for r in d.relations:
        lines.append(f'    {_id(r.left)} {_CARD_MM[r.cardinality]} {_id(r.right)} : "{_label(r.label) or "has"}"')
    return "\n".join(lines)


def erd_plantuml(d: ERDiagram) -> str:
    lines = ["@startuml", f"title {_label(d.title)}", "hide circle", "skinparam linetype ortho"]
    for e in d.entities:
        lines.append(f'entity "{_label(e.name)}" as {_id(e.name)} {{')
        keys = [a for a in e.attributes if a.key == "PK"]
        for a in keys:
            lines.append(f"  * {a.name} : {a.type} <<PK>>")
        if keys:
            lines.append("  --")
        for a in e.attributes:
            if a.key != "PK":
                stereo = f" <<{a.key}>>" if a.key else ""
                lines.append(f"  {a.name} : {a.type}{stereo}")
        lines.append("}")
    for r in d.relations:
        label = f" : {_label(r.label)}" if r.label else ""
        lines.append(f"{_id(r.left)} {_CARD_PU[r.cardinality]} {_id(r.right)}{label}")
    lines.append("@enduml")
    return "\n".join(lines)


# --- Component --------------------------------------------------------------------------

def component_mermaid(a: ArchitectureProposal) -> str:
    shapes = {"database": '[("{}")]', "queue": '[/"{}"/]', "external": '(["{}"])', "frontend": '["{}"]',
              "gateway": '{{{{"{}"}}}}', "cache": '[("{}")]', "service": '["{}"]', "other": '["{}"]'}
    lines = ["flowchart LR"]
    for c in a.components:
        text = c.name + (f"<br>{c.technology}" if c.technology else "")
        lines.append(f"    {_id(c.name)}{shapes[c.kind].format(_label(text))}")
        if c.is_new:
            lines.append(f"    style {_id(c.name)} stroke-width:3px")
    for i in a.interactions:
        arrow = "-.->" if i.mode == "async" else "-->"
        lines.append(f"    {_id(i.source)} {arrow}|{_label(i.protocol)}| {_id(i.target)}")
    return "\n".join(lines)


def component_plantuml(a: ArchitectureProposal) -> str:
    kinds = {"database": "database", "queue": "queue", "external": "cloud", "frontend": "component",
             "gateway": "boundary", "cache": "database", "service": "component", "other": "node"}
    lines = ["@startuml", "left to right direction"]
    for c in a.components:
        stereo = " <<new>>" if c.is_new else ""
        lines.append(f'{kinds[c.kind]} "{_label(c.name)}" as {_id(c.name)}{stereo}')
    for i in a.interactions:
        arrow = "..>" if i.mode == "async" else "-->"
        lines.append(f"{_id(i.source)} {arrow} {_id(i.target)} : {_label(i.protocol)}")
    lines.append("@enduml")
    return "\n".join(lines)


def architecture_ascii(a: ArchitectureProposal) -> str:
    """Текстовая схема взаимодействий как в ТЗ: источник → цели."""
    out: dict[str, list[str]] = {}
    for i in a.interactions:
        out.setdefault(i.source, []).append(f"{i.target} ({i.protocol}, {i.mode})")
    lines = []
    for source, targets in out.items():
        lines.append(source)
        for t in targets:
            lines.append(f"   +---- {t}")
    return "\n".join(lines)


# --- OpenAPI ----------------------------------------------------------------------------

def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        return None


def _field_schema(f: ApiField) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": f.type}
    if f.format:
        schema["format"] = f.format
    if f.description or f.validation:
        schema["description"] = " ".join(x for x in (f.description, f"Validation: {f.validation}" if f.validation else "") if x)
    if f.example:
        example: Any = f.example
        if f.type == "integer":
            try:
                example = int(f.example)
            except ValueError:
                pass
        elif f.type == "number":
            try:
                example = float(f.example)
            except ValueError:
                pass
        elif f.type == "boolean":
            example = f.example.lower() == "true"
        if f.type not in ("object", "array"):
            schema["example"] = example
    if f.type == "object":
        schema["properties"] = {}
    if f.type == "array":
        schema["items"] = {"type": "string"}
    return schema


def fields_schema(fields: list[ApiField]) -> dict[str, Any]:
    """Плоский список полей ("card.id", "items[].amount") → вложенная JSON-схема."""
    root: dict[str, Any] = {"type": "object", "properties": {}}
    for f in fields:
        node = root
        parts = f.name.split(".")
        for i, part in enumerate(parts):
            is_array = part.endswith("[]")
            name = part[:-2] if is_array else part
            last = i == len(parts) - 1
            props = node.setdefault("properties", {})
            if last:
                schema = _field_schema(f)
                if is_array:
                    schema = {"type": "array", "items": schema}
                existing = props.get(name)
                if existing and "properties" in existing and schema.get("type") == "object":
                    schema["properties"] = existing["properties"]
                props[name] = schema
                if f.required:
                    required = node.setdefault("required", [])
                    if name not in required:
                        required.append(name)
            else:
                child = props.get(name)
                if child is None:
                    child = {"type": "array", "items": {"type": "object", "properties": {}}} if is_array \
                        else {"type": "object", "properties": {}}
                    props[name] = child
                node = child["items"] if child.get("type") == "array" else child
                node.setdefault("type", "object")
    return root


def openapi_document(spec: ApiSpec) -> dict[str, Any]:
    security_schemes = {
        "bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        "oauth2": {"type": "oauth2", "flows": {"clientCredentials": {"tokenUrl": "/oauth/token", "scopes": {}}}},
        "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        "mtls": {"type": "mutualTLS"},
    }
    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": spec.title, "version": spec.version or "1.0.0", "description": spec.summary},
        "servers": [{"url": spec.base_path or "/"}],
        "security": [{"auth": []}],
        "paths": {},
        "components": {
            "securitySchemes": {"auth": security_schemes[spec.security_scheme]},
            "schemas": {"Error": fields_schema(spec.error_model_fields)},
        },
    }
    if spec.error_codes:
        doc["components"]["schemas"]["Error"]["x-error-codes"] = [
            {"code": c.code, "httpStatus": c.http_status, "description": c.description} for c in spec.error_codes
        ]
    for ep in spec.endpoints:
        op: dict[str, Any] = {
            "operationId": ep.operation_id,
            "summary": ep.summary,
            "description": "\n\n".join(x for x in (
                ep.description,
                f"**Authentication:** {ep.authentication}" if ep.authentication else "",
                f"**Authorization:** {ep.authorization}" if ep.authorization else "",
                f"**Idempotency:** {ep.idempotency}" if ep.idempotency else "",
                ("**Validation:**\n" + "\n".join(f"- {v}" for v in ep.validation_rules)) if ep.validation_rules else "",
            ) if x),
            "tags": ep.tags,
            "parameters": [],
            "responses": {},
        }
        for h in ep.headers:
            param = {"name": h.name, "in": "header", "required": h.required, "description": h.description,
                     "schema": {"type": "string"}}
            if h.example:
                param["example"] = h.example
            op["parameters"].append(param)
        for location, params in (("path", ep.path_params), ("query", ep.query_params)):
            for p in params:
                op["parameters"].append({"name": p.name, "in": location,
                                         "required": True if location == "path" else p.required,
                                         "description": p.description, "schema": _field_schema(p)})
        if not op["parameters"]:
            del op["parameters"]
        if ep.request_fields:
            media: dict[str, Any] = {"schema": fields_schema(ep.request_fields)}
            example = _json_or_none(ep.request_example_json)
            if example is not None:
                media["example"] = example
            op["requestBody"] = {"required": True, "content": {"application/json": media}}
        for r in ep.responses:
            resp: dict[str, Any] = {"description": r.description or str(r.status)}
            if r.fields:
                media = {"schema": fields_schema(r.fields)}
            elif r.status >= 400:
                media = {"schema": {"$ref": "#/components/schemas/Error"}}
            else:
                media = {}
            example = _json_or_none(r.example_json)
            if example is not None:
                media["example"] = example
            if media:
                resp["content"] = {"application/json": media}
            op["responses"][str(r.status)] = resp
        if not op["responses"]:
            op["responses"]["default"] = {"description": "Response"}
        doc["paths"].setdefault(ep.path, {})[ep.method.lower()] = op
    return doc


def openapi_yaml(spec: ApiSpec) -> str:
    return yaml.safe_dump(openapi_document(spec), allow_unicode=True, sort_keys=False, width=120)


# --- Markdown ----------------------------------------------------------------------------

def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "—"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(v: Any) -> str:
        return str(v).replace("|", "\\|").replace("\n", "<br>")

    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(out)


def md_discovery(d: DiscoveryResult) -> str:
    parts = [f"### Контекст\n{d.context_summary}", f"**Домен:** {d.domain}"]
    if d.known_facts:
        parts.append("**Известно:**\n" + _bullets(d.known_facts))
    if d.resolved:
        parts.append("**Уточнено в интервью:**\n" + _bullets([f"{r.question} → {r.answer}" for r in d.resolved]))
    if d.assumptions:
        parts.append("**Допущения:**\n" + _bullets(d.assumptions))
    if d.questions:
        qs = []
        for i, q in enumerate(d.questions, 1):
            hint = f"\n   _Варианты: {'; '.join(q.suggested_answers)}_" if q.suggested_answers else ""
            qs.append(f"{i}. **{q.question}** ({q.category})\n   {q.why}{hint}")
        parts.append("### Открытые вопросы\n" + "\n".join(qs))
    parts.append(f"**Готовность:** {'контекст собран' if d.ready else 'нужны уточнения'} — {d.readiness_comment}")
    return "\n\n".join(parts)


def md_requirements(r: RequirementsDoc) -> str:
    def reqs(items: list[Any]) -> str:
        return _table(["ID", "Требование", "Приоритет", "Источник"],
                      [[x.id, x.text, x.priority, x.source] for x in items]) if items else "—"

    parts = [f"## {r.title}", r.summary,
             "### Business Requirements", reqs(r.business_requirements),
             "### Functional Requirements", reqs(r.functional_requirements),
             "### Non-Functional Requirements",
             _table(["ID", "Категория", "Требование", "Метрика", "Приоритет"],
                    [[n.id, n.category, n.text, n.metric, n.priority] for n in r.non_functional_requirements])
             if r.non_functional_requirements else "—",
             "### Business Rules", reqs(r.business_rules),
             "### Acceptance Criteria"]
    parts += [f"**{a.id}** ({', '.join(a.requirement_refs)})\n- **Given** {a.given}\n- **When** {a.when}\n- **Then** {a.then}"
              for a in r.acceptance_criteria] or ["—"]
    parts.append("### User Stories")
    parts += [f"**{s.id}.** Как {s.as_a}, я хочу {s.i_want}, чтобы {s.so_that}."
              + (f" _AC: {', '.join(s.acceptance_refs)}_" if s.acceptance_refs else "") for s in r.user_stories] or ["—"]
    parts.append("### Use Cases")
    for uc in r.use_cases:
        parts.append(
            f"**{uc.id}. {uc.name}** (актор: {uc.actor})\n\n_Предусловия:_\n{_bullets(uc.preconditions)}\n\n"
            "_Основной поток:_\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(uc.main_flow, 1))
            + f"\n\n_Альтернативные потоки:_\n{_bullets(uc.alternative_flows)}\n\n_Постусловия:_\n{_bullets(uc.postconditions)}"
        )
    if r.glossary:
        parts += ["### Глоссарий", _table(["Термин", "Определение"], [[g.term, g.definition] for g in r.glossary])]
    parts += ["### Допущения", _bullets(r.assumptions), "### Открытые вопросы", _bullets(r.open_issues)]
    return "\n\n".join(parts)


SEVERITY_ICON = {"critical": "⛔", "major": "⚠", "minor": "•", "info": "ℹ"}


def md_score(s: dict[str, Any]) -> str:
    lines = ["```text", "Requirements completeness", "",
             f"{score_bar(s['overall'])} {s['overall']}%", ""]
    lines += [f"{a['title']:<20} {a['score']:>3}%" for a in s["aspects"]]
    if s["missing"]:
        lines += ["", "Missing:"] + [f"⚠ {m}" for m in s["missing"]]
    lines.append("```")
    lines.append(f"_{s['disclaimer']}_")
    return "\n".join(lines)


def md_review(r: ReviewResult, score: dict[str, Any] | None = None, checklist: dict[str, Any] | None = None) -> str:
    parts = [f"### Итог ревью\n{r.summary}"]
    if score:
        parts.append(md_score(score))
    for f in r.findings:
        refs = f" ({', '.join(f.requirement_refs)})" if f.requirement_refs else ""
        qs = "\n".join(f"   {i}. {q}" for i, q in enumerate(f.questions, 1))
        parts.append(f"{SEVERITY_ICON[f.severity]} **{f.id} [{f.severity} · {f.category}]**{refs} {f.problem}"
                     + (f"\n\n   Не определено:\n{qs}" if qs else "") + f"\n\n   _Рекомендация:_ {f.recommendation}")
    if checklist and checklist.get("vague"):
        parts.append("### Размытые формулировки\n" + _bullets(
            [f"«{v['term']}»: {v['sentence']}" for v in checklist["vague"]]))
    if r.risks:
        parts += ["### Риски", md_risks(r.risks)]
    return "\n\n".join(parts)


def md_risks(risks: list[Any]) -> str:
    return _table(["ID", "Риск", "Вероятность", "Влияние", "Митигация"],
                  [[x.id, f"**{x.title}** — {x.description}", x.probability, x.impact, x.mitigation] for x in risks])


def md_architecture(a: ArchitectureProposal) -> str:
    parts = [f"### Архитектурное решение\n{a.summary}",
             "```text\n" + architecture_ascii(a) + "\n```",
             "```mermaid\n" + component_mermaid(a) + "\n```",
             "### Компоненты",
             _table(["Компонент", "Тип", "Назначение", "Технология", "Новый"],
                    [[c.name, c.kind, c.purpose, c.technology, "да" if c.is_new else "нет"] for c in a.components]),
             "### Взаимодействия",
             _table(["Откуда", "Куда", "Протокол", "Режим", "Описание"],
                    [[i.source, i.target, i.protocol, i.mode, i.description] for i in a.interactions])]
    if a.topics:
        parts += ["### Kafka topics", _table(["Topic", "Producer", "Consumers", "Payload"],
                                             [[t.name, t.producer, ", ".join(t.consumers), t.payload] for t in a.topics])]
    if a.datastores:
        parts += ["### Базы данных", _table(["Хранилище", "Технология", "Владелец", "Сущности"],
                                            [[d.name, d.technology, d.owner, ", ".join(d.entities)] for d in a.datastores])]
    if a.external_systems:
        parts += ["### Внешние системы", _bullets(a.external_systems)]
    if a.decisions:
        parts.append("### Решения (ADR)")
        parts += [f"**{d.title}.** {d.decision}\n\n_Обоснование:_ {d.rationale}"
                  + (f"\n\n_Альтернативы:_ {'; '.join(d.alternatives)}" if d.alternatives else "") for d in a.decisions]
    if a.risks:
        parts += ["### Риски", md_risks(a.risks)]
    return "\n\n".join(parts)


def md_architecture_review(r: ArchitectureReview) -> str:
    parts = [f"### Review архитектуры\n{r.summary}", "**Potential issues:**"]
    parts += [f"{i}. {SEVERITY_ICON[x.severity]} **{x.title}** [{x.category}]\n   _Основание:_ {x.rationale}\n"
              f"   _Рекомендация:_ {x.recommendation}" for i, x in enumerate(r.issues, 1)] or ["—"]
    if r.strengths:
        parts += ["**Сильные стороны:**", _bullets(r.strengths)]
    if r.questions:
        parts += ["**Вопросы к автору решения:**", _bullets(r.questions)]
    parts.append("_Это рекомендации, а не утверждение архитектуры: решение принимает команда._")
    return "\n\n".join(parts)


def md_api(spec: ApiSpec, conflicts: list[str] | None = None) -> str:
    parts = [f"### {spec.title} ({spec.version})", spec.summary, f"**Base path:** `{spec.base_path}` · "
             f"**Security:** {spec.security_scheme}"]
    if conflicts:
        parts.append("⚠ **Пересечение с существующими API:**\n" + _bullets(conflicts))
    if spec.reused_existing:
        parts.append("**Используются существующие API:**\n" + _bullets(spec.reused_existing))
    for ep in spec.endpoints:
        parts.append(f"#### `{ep.method} {ep.path}`\n{ep.summary}. {ep.description}")
        parts.append(f"- **Authentication:** {ep.authentication}\n- **Authorization:** {ep.authorization}\n"
                     f"- **Idempotency:** {ep.idempotency}")
        if ep.headers:
            parts.append(_table(["Header", "Обяз.", "Описание", "Пример"],
                                [[h.name, "да" if h.required else "нет", h.description, h.example] for h in ep.headers]))
        params = ep.path_params + ep.query_params + ep.request_fields
        if params:
            parts.append(_table(["Поле", "Тип", "Обяз.", "Описание", "Валидация"],
                                [[f.name, f.type + (f" ({f.format})" if f.format else ""), "да" if f.required else "нет",
                                  f.description, f.validation] for f in params]))
        if ep.request_example_json:
            parts.append("Request:\n```json\n" + _pretty(ep.request_example_json) + "\n```")
        for r in ep.responses:
            body = f"\n```json\n{_pretty(r.example_json)}\n```" if r.example_json else ""
            parts.append(f"**{r.status}** — {r.description}{body}")
        if ep.validation_rules:
            parts.append("Validation:\n" + _bullets(ep.validation_rules))
    if spec.error_codes:
        parts += ["#### Error model", _table(["Поле", "Тип", "Описание"],
                                             [[f.name, f.type, f.description] for f in spec.error_model_fields]),
                  _table(["Code", "HTTP", "Описание"], [[c.code, c.http_status, c.description] for c in spec.error_codes])]
    if spec.notes:
        parts += ["#### Примечания", _bullets(spec.notes)]
    return "\n\n".join(parts)


def _pretty(text: str) -> str:
    data = _json_or_none(text)
    return json.dumps(data, ensure_ascii=False, indent=2) if data is not None else text


def md_tests(t: TestSuite) -> str:
    parts = [t.summary, "```text\n" + "\n".join(f"{c.id} {c.title}" for c in t.test_cases) + "\n```"]
    for c in t.test_cases:
        parts.append(
            f"#### {c.id} {c.title}\n**Тип:** {c.type} · **Priority:** {c.priority} · "
            f"**Требования:** {', '.join(c.requirement_refs) or '—'}\n\n**Preconditions:**\n{_bullets(c.preconditions)}\n\n"
            "**Steps:**\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(c.steps, 1))
            + f"\n\n**Expected Result:** {c.expected_result}"
        )
    if t.coverage_notes:
        parts += ["### Покрытие", _bullets(t.coverage_notes)]
    return "\n\n".join(parts)


def diagram_sources(kind: str, content: dict[str, Any]) -> dict[str, str]:
    """{'mermaid': …, 'plantuml': …} для артефакта-диаграммы."""
    if kind == "sequence":
        d = SequenceDiagram.model_validate(content)
        return {"mermaid": sequence_mermaid(d), "plantuml": sequence_plantuml(d)}
    if kind == "activity":
        d2 = ActivityDiagram.model_validate(content)
        return {"mermaid": activity_mermaid(d2), "plantuml": activity_plantuml(d2)}
    if kind == "erd":
        d3 = ERDiagram.model_validate(content)
        return {"mermaid": erd_mermaid(d3), "plantuml": erd_plantuml(d3)}
    if kind == "architecture":
        a = ArchitectureProposal.model_validate(content)
        return {"mermaid": component_mermaid(a), "plantuml": component_plantuml(a)}
    return {}


def md_diagram(kind: str, content: dict[str, Any]) -> str:
    src = diagram_sources(kind, content)
    title = content.get("title", kind)
    return (f"### {title}\n\n```mermaid\n{src['mermaid']}\n```\n\n<details><summary>PlantUML</summary>\n\n"
            f"```plantuml\n{src['plantuml']}\n```\n\n</details>")


def md_final_review(f: dict[str, Any]) -> str:
    parts = ["### Финальная проверка"]
    if f.get("score"):
        parts.append(md_score(f["score"]))
    t = f.get("traceability") or {}
    if t.get("rows"):
        parts.append(f"**Покрытие функциональных требований тестами:** {t['coverage']}%")
        parts.append(_table(["Требование", "AC", "Тесты"],
                            [[r["requirement"], "✓" if r["acceptance"] else "—", ", ".join(r["tests"]) or "—"]
                             for r in t["rows"]]))
        if t["uncovered_by_tests"]:
            parts.append("⚠ Не покрыты тестами: " + ", ".join(t["uncovered_by_tests"]))
        if t["uncovered_by_ac"]:
            parts.append("⚠ Нет критериев приёмки: " + ", ".join(t["uncovered_by_ac"]))
    if f.get("missing_artifacts"):
        parts.append("⚠ Не сформированы: " + ", ".join(f["missing_artifacts"]))
    if f.get("open_findings"):
        parts.append("**Открытые критичные замечания:**\n" + _bullets(f["open_findings"]))
    return "\n\n".join(parts)


def artifact_markdown(kind: str, content: dict[str, Any]) -> str:
    if kind == "discovery":
        return md_discovery(DiscoveryResult.model_validate(content))
    if kind == "requirements":
        return md_requirements(RequirementsDoc.model_validate(content))
    if kind == "review":
        return md_review(ReviewResult.model_validate(content["review"]), content.get("score"), content.get("checklist"))
    if kind == "architecture":
        return md_architecture(ArchitectureProposal.model_validate(content))
    if kind == "architecture_review":
        return md_architecture_review(ArchitectureReview.model_validate(content))
    if kind == "api":
        return md_api(ApiSpec.model_validate(content["spec"]), content.get("conflicts"))
    if kind in ("sequence", "activity", "erd"):
        return md_diagram(kind, content)
    if kind == "test_cases":
        return md_tests(TestSuite.model_validate(content))
    if kind == "final_review":
        return md_final_review(content)
    return "```json\n" + json.dumps(content, ensure_ascii=False, indent=2) + "\n```"
