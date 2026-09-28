from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from analyst.api import create_app
from analyst.service import AnalystService
from analyst.storage import Store

from analyst_fakes import FakeLLM
from test_analyst_core import OPENAPI


@pytest.fixture
def env():
    llm = FakeLLM()
    service = AnalystService(Store(), llm, inline_jobs=True)
    client = TestClient(create_app(service))
    _, token = service.create_user("david")
    _, other = service.create_user("eve")
    return {"llm": llm, "service": service, "client": client,
            "h": {"Authorization": f"Bearer {token}"}, "other": {"Authorization": f"Bearer {other}"}}


def _project(env, **context):
    r = env["client"].post("/api/v1/projects", headers=env["h"],
                           json={"name": "Refund", "description": "Возвраты", "context": context})
    assert r.status_code == 201
    return r.json()["id"]


def test_auth_required(env):
    assert env["client"].get("/api/v1/projects").status_code == 401
    r = env["client"].get("/api/v1/projects", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


def test_full_workflow(env):
    c, h, llm = env["client"], env["h"], env["llm"]
    pid = _project(env, tech_stack="Java, Kafka, PostgreSQL")

    r = c.post(f"/api/v1/projects/{pid}/documents", headers=h, files={"file": ("existing-api.yaml", OPENAPI)})
    assert r.status_code == 201 and r.json()["category"] == "API"
    assert c.get(f"/api/v1/projects/{pid}/knowledge", headers=h).json()["tree"]["API"][0]["filename"] \
        == "existing-api.yaml"
    assert c.get(f"/api/v1/projects/{pid}/knowledge?q=refunds", headers=h).json()[0]["filename"] == "existing-api.yaml"

    # 1. Задача → интервью, а не ТЗ.
    r = c.post(f"/api/v1/projects/{pid}/tasks", headers=h,
               json={"problem": "Нужно добавить возможность вернуть деньги клиенту на другую банковскую карту."})
    assert r.status_code == 202 and r.json()["job"]["status"] == "succeeded"
    tid = r.json()["id"]
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    assert task["stage"] == "interview"
    assert task["messages"][-1]["content"].startswith("Я вижу несколько неопределённых моментов.")
    assert "Кто инициирует возврат?" in task["messages"][-1]["content"]
    # Контекст проекта и база знаний попали в промпт.
    discovery_call = llm.calls[0]
    assert "Java, Kafka, PostgreSQL" in discovery_call["system"][1]
    assert "<knowledge_base>" in discovery_call["prompt"]

    # 2. Ответ на интервью → контекст собран.
    llm.route("answer_interview")
    r = c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=h, json={"text": "Оператор, частично можно"})
    assert r.json()["job"]["status"] == "succeeded"
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    assert task["stage"] == "ready" and "Контекст собран. Сформировать требования?" in task["messages"][-1]["content"]
    assert "Оператор, частично можно" in llm.calls[-1]["prompt"]

    # 3. «Да» → требования + автоматическое ревью со score.
    llm.route("generate_requirements")
    c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=h, json={"text": "Да"})
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    assert set(task["artifacts"]) == {"discovery", "requirements", "review"}
    assert 0 < task["artifacts"]["review"]["content"]["score"]["overall"] < 100
    assert "Requirements completeness" in task["messages"][-1]["content"]

    # 4. Полный цикл: архитектура, API, sequence, тесты, финальная проверка.
    r = c.post(f"/api/v1/projects/{pid}/tasks/{tid}/steps/pipeline", headers=h, json={})
    assert r.status_code == 202 and r.json()["status"] == "succeeded", r.json()
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    for kind in ("architecture", "api", "sequence", "test_cases", "final_review"):
        assert kind in task["artifacts"], kind
    assert task["artifacts"]["review"]["version"] == 2
    assert "mermaid" in task["artifacts"]["sequence"]["diagrams"]
    api_prompt = next(call for call in llm.calls if call["schema"] == "ApiSpec")["prompt"]
    assert "POST /api/v1/refunds — createRefund" in api_prompt  # существующие API переданы агенту
    final = task["artifacts"]["final_review"]["content"]
    assert final["traceability"]["uncovered_by_tests"] == ["FR-3"]

    # 5. Экспорт.
    r = c.get(f"/api/v1/projects/{pid}/tasks/{tid}/export?format=openapi", headers=h)
    assert r.status_code == 200 and "openapi: 3.1.0" in r.text
    r = c.get(f"/api/v1/projects/{pid}/tasks/{tid}/export?format=pdf", headers=h)
    assert r.content.startswith(b"%PDF")
    assert c.get(f"/api/v1/projects/{pid}/tasks/{tid}/export?format=xls", headers=h).status_code == 422

    # 6. История версий и аудит.
    hist = c.get(f"/api/v1/projects/{pid}/tasks/{tid}/artifacts?kind=review", headers=h).json()
    assert [a["version"] for a in hist] == [2, 1]
    audit = c.get(f"/api/v1/projects/{pid}/audit", headers=h).json()
    actions = {a["action"] for a in audit}
    assert {"Upload document", "AI Interview", "Generate requirements", "Generate API", "Export"} <= actions
    gen = next(a for a in audit if a["action"] == "Generate API")
    assert gen["username"] == "david" and gen["model"] == "fake-model" and gen["artifact_id"]


def test_review_single_requirement_text(env):
    c, h, llm = env["client"], env["h"], env["llm"]
    pid = _project(env)
    tid = c.post(f"/api/v1/projects/{pid}/tasks", headers=h, json={"problem": "Возврат"}).json()["id"]
    text = "Сервис должен отправлять запрос на возврат платежа."
    llm.route("review_requirements", instruction=text)
    c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=h, json={"text": text})
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    reply = task["messages"][-1]["content"]
    assert reply.startswith("⚠ Требование недостаточно детализировано.")
    assert "Не определено:" in reply
    assert task["artifacts"]["review"]["content"]["subject"] == text


def test_architecture_review_and_diagram_routing(env):
    c, h, llm = env["client"], env["h"], env["llm"]
    pid = _project(env)
    tid = c.post(f"/api/v1/projects/{pid}/tasks", headers=h, json={"problem": "Возврат"}).json()["id"]
    llm.route("review_architecture", instruction="Frontend → API Gateway → Service A → Service B → PostgreSQL")
    c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=h, json={"text": "Проверь решение"})
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    assert "single point of failure" in task["messages"][-1]["content"]
    assert "не утверждение архитектуры" in task["messages"][-1]["content"]

    llm.route("generate_diagram", diagram_type="erd")
    c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=h, json={"text": "Нарисуй ERD"})
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    # ERD требует требований — они сформированы автоматически.
    assert {"requirements", "erd"} <= set(task["artifacts"])
    assert task["artifacts"]["erd"]["diagrams"]["mermaid"].startswith("erDiagram")

    llm.route("question")
    c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=h, json={"text": "Какие API есть?"})
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    assert "Источники: api.yaml" in task["messages"][-1]["content"]


def test_project_isolation_and_rbac(env):
    c, h, other = env["client"], env["h"], env["other"]
    pid = _project(env)
    tid = c.post(f"/api/v1/projects/{pid}/tasks", headers=h, json={"problem": "Возврат"}).json()["id"]
    # Чужой пользователь не видит проект, задачи и не может загружать документы.
    assert c.get(f"/api/v1/projects/{pid}", headers=other).status_code == 404
    assert c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=other).status_code == 404
    assert c.post(f"/api/v1/projects/{pid}/documents", headers=other,
                  files={"file": ("a.txt", b"x")}).status_code == 404
    assert c.get("/api/v1/projects", headers=other).json() == []

    # Viewer читает, но не пишет.
    assert c.post(f"/api/v1/projects/{pid}/members", headers=h,
                  json={"username": "eve", "role": "viewer"}).status_code == 204
    assert c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=other).status_code == 200
    r = c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=other, json={"text": "hi"})
    assert r.status_code == 403
    assert c.get(f"/api/v1/projects/{pid}/audit", headers=other).status_code == 403

    # Документы другого проекта не попадают в контекст.
    pid2 = c.post("/api/v1/projects", headers=other, json={"name": "Other"}).json()["id"]
    c.post(f"/api/v1/projects/{pid2}/documents", headers=other, files={"file": ("secret.txt", "Возврат секрет")})
    env["llm"].calls.clear()
    env["llm"].route("answer_interview")
    c.post(f"/api/v1/projects/{pid}/tasks/{tid}/messages", headers=h, json={"text": "Возврат"})
    assert all("секрет" not in call["prompt"] for call in env["llm"].calls)


def test_human_in_the_loop_delete_and_publish(env):
    c, h, other = env["client"], env["h"], env["other"]
    pid = _project(env)
    doc = c.post(f"/api/v1/projects/{pid}/documents", headers=h, files={"file": ("rules.md", "Правило")}).json()
    assert c.post(f"/api/v1/projects/{pid}/documents", headers=h,
                  files={"file": ("copy.md", "Правило")}).status_code == 409

    r = c.delete(f"/api/v1/projects/{pid}/documents/{doc['id']}", headers=h)
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert len(c.get(f"/api/v1/projects/{pid}/documents", headers=h).json()) == 1  # ещё не удалён
    action = r.json()["id"]

    c.post(f"/api/v1/projects/{pid}/members", headers=h, json={"username": "eve", "role": "editor"})
    assert c.post(f"/api/v1/projects/{pid}/actions/{action}/confirm", headers=other).status_code == 403
    assert c.post(f"/api/v1/projects/{pid}/actions/{action}/confirm", headers=h).json()["status"] == "approved"
    assert c.get(f"/api/v1/projects/{pid}/documents", headers=h).json() == []
    assert c.post(f"/api/v1/projects/{pid}/actions/{action}/confirm", headers=h).status_code == 409

    tid = c.post(f"/api/v1/projects/{pid}/tasks", headers=h, json={"problem": "Возврат"}).json()["id"]
    c.post(f"/api/v1/projects/{pid}/tasks/{tid}/steps/requirements", headers=h)
    art = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()["artifacts"]["requirements"]
    pub = c.post(f"/api/v1/projects/{pid}/artifacts/{art['id']}/publish", headers=h).json()
    c.post(f"/api/v1/projects/{pid}/actions/{pub['id']}/reject", headers=h)
    assert c.get(f"/api/v1/projects/{pid}/artifacts/{art['id']}", headers=h).json()["status"] == "draft"
    pub = c.post(f"/api/v1/projects/{pid}/artifacts/{art['id']}/publish", headers=h).json()
    c.post(f"/api/v1/projects/{pid}/actions/{pub['id']}/confirm", headers=h)
    assert c.get(f"/api/v1/projects/{pid}/artifacts/{art['id']}", headers=h).json()["status"] == "published"


def test_architecture_context_change_requires_confirmation(env):
    c, h = env["client"], env["h"]
    pid = _project(env, architecture="Монолит")
    r = c.patch(f"/api/v1/projects/{pid}", headers=h,
                json={"context": {"architecture": "Микросервисы", "domain": "Платежи"}})
    body = r.json()
    assert body["context"] == {"architecture": "Монолит", "domain": "Платежи"}
    assert body["pending_action"]["action"] == "update_architecture"
    c.post(f"/api/v1/projects/{pid}/actions/{body['pending_action']['id']}/confirm", headers=h)
    assert c.get(f"/api/v1/projects/{pid}", headers=h).json()["context"]["architecture"] == "Микросервисы"
    assert c.patch(f"/api/v1/projects/{pid}", headers=h, json={"context": {"bogus": "x"}}).status_code == 422


def test_step_errors_are_reported(env):
    c, h = env["client"], env["h"]
    pid = _project(env)
    tid = c.post(f"/api/v1/projects/{pid}/tasks", headers=h, json={"problem": "Возврат"}).json()["id"]
    job = c.post(f"/api/v1/projects/{pid}/tasks/{tid}/steps/final_review", headers=h).json()
    assert job["status"] == "failed" and "после формирования требований" in job["error"]
    assert c.get(f"/api/v1/jobs/{job['id']}", headers=h).json()["status"] == "failed"
    task = c.get(f"/api/v1/projects/{pid}/tasks/{tid}", headers=h).json()
    assert task["messages"][-1]["meta"]["kind"] == "error"
    assert c.post(f"/api/v1/projects/{pid}/tasks/{tid}/steps/unknown", headers=h).status_code == 422
    assert c.post(f"/api/v1/projects/{pid}/documents", headers=h,
                  files={"file": ("a.exe", b"x")}).status_code == 422
