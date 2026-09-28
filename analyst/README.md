# System Analyst Agent

AI-агент для системного и бизнес-аналитика на базе Claude API. Пользователь описывает бизнес-задачу, а агент:

1. собирает контекст из базы знаний проекта;
2. проводит интервью с уточняющими вопросами (**AI Interview**);
3. формирует требования: BR, FR, NFR, бизнес-правила, критерии приёмки, user stories, use cases;
4. проверяет их на пропуски и противоречия и считает **Requirements Score**;
5. предлагает архитектуру, API-контракт (с OpenAPI 3.1), диаграммы и тест-кейсы;
6. выполняет финальную проверку: трассировку требований к тестам и оставшиеся замечания;
7. экспортирует результат в Markdown, PDF, DOCX, OpenAPI YAML, JSON, Mermaid, PlantUML и черновик Jira.

Главный принцип: агент не просто генерирует текст, а помогает аналитику думать. Его основные функции — AI Discovery, Requirements Review и Technical Design.

## Быстрый старт

Нужны **Python 3.10+** и **Node.js 18+**. Версии проверяются командами `python3 --version` и `node --version`.

> **macOS.** В системе нет команды `python`, есть только `python3`. Если его нет или версия ниже 3.10 (встроенный Python обычно 3.9), поставьте свежий через Homebrew: `brew install python@3.12 node`.
> Поэтому окружение создаётся через `python3`. В активированном окружении команда `python` работает.

```bash
python3 -m venv .venv                  # с brew-версией: python3.12 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...            # или `ant auth login`

cd web && npm install && npm run build && cd ..   # веб-интерфейс
python -m analyst serve                           # http://127.0.0.1:8000
```

В каждом новом окне терминала сначала выполните `source .venv/bin/activate`. Без этого возникнет `command not found: python` или `No module named ...`.

При первом запуске создаётся пользователь `admin`, его токен печатается в консоль. Этим токеном нужно войти в веб-интерфейс.
Документация API (Swagger) доступна по адресу `/docs`.

```bash
python -m analyst create-user david          # новый пользователь и его токен
python -m analyst rotate-token david         # перевыпуск токена
python -m analyst gen-key                    # ключ для ANALYST_ENCRYPTION_KEY
```

Для разработки фронтенда: `cd web && npm run dev`. Vite проксирует `/api` на `127.0.0.1:8000`.

### Настройки (переменные окружения)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | ключ Claude API |
| `ANALYST_MODEL` | `claude-opus-5-5` | модель для всех агентов |
| `ANALYST_DB` | `data/analyst.db` | файл SQLite |
| `ANALYST_ENCRYPTION_KEY` | — | Fernet-ключ: шифрует документы, чанки, сообщения и артефакты |
| `ANALYST_FULL_KB_CHARS` | `150000` | если база знаний меньше этого объёма, агент получает её целиком; иначе работает поиск BM25 |
| `ANALYST_PDF_FONT` | DejaVuSans | TTF-шрифт с кириллицей для экспорта в PDF |
| `ANALYST_WEB_DIST` | `web/dist` | собранный фронтенд |

## Workflow

```text
Создание проекта → загрузка документов → индексация базы знаний
  → описание бизнес-задачи → AI Interview → «Контекст собран. Сформировать требования?»
  → требования → ревью + score → архитектура → API → sequence diagram → тест-кейсы
  → финальная проверка → экспорт / публикация (с подтверждением)
```

В чате можно писать свободно. Оркестратор сам определяет намерение: ответ на интервью, «проверь требование…», «проверь мою архитектуру: A → B → C», «добавь endpoint для статуса возврата», «нарисуй ERD», вопрос по документам.
Кнопки под чатом запускают отдельные шаги или весь цикл («Полный цикл ▶»).

## Архитектура

```text
web/ (React + TS)  ──►  FastAPI (analyst/api.py)
                           │
                    AnalystService (service.py): роли, задания, подтверждения, аудит
                           │
            ┌──────────────┼──────────────────┐
            ▼              ▼                  ▼
      Orchestrator    Knowledge Base       Exporters
      (agents/)       (knowledge.py)       (exporters.py, renderers.py)
            │              │
            ▼              ▼
      Claude API      SQLite + BM25
      (llm.py)        (storage.py)
```

**Агенты** (`analyst/agents/`). Каждый агент — это системный промпт и строгая JSON-схема ответа (structured outputs), а не один общий промпт.

| Агент | Результат |
|---|---|
| Orchestrator (router) | намерение пользователя и параметры шага |
| Discovery (AI Interview) | контекст, известные факты, отвеченные и открытые вопросы, готовность |
| Requirements | полный комплект требований со сквозными ID |
| Review | замечания по категориям (неоднозначность, timeout/retry, идемпотентность…) и риски |
| Architecture / Architecture Review | компоненты, взаимодействия, Kafka topics, БД, ADR; review чужого решения без «утверждения» |
| API | endpoints, заголовки, валидация, коды, error model, идемпотентность, примеры |
| Diagram | sequence, activity/BPMN, ERD (component строится из архитектуры) |
| QA | тест-кейсы с приоритетами и ссылками на требования |

Диаграммы и OpenAPI агент возвращает как **структуру**. Тексты Mermaid, PlantUML и OpenAPI строятся из неё детерминированно, поэтому синтаксис всегда корректен и одинаков в обоих форматах.

**Детерминированные проверки** (`checklist.py`) работают без LLM:

- чек-лист аспектов: business logic, API, ошибки, timeout/retry, идемпотентность, безопасность, логирование, мониторинг, NFR, отказоустойчивость, edge cases;
- поиск размытых формулировок («быстро», «удобно», «и т.д.»);
- Requirements Score: упоминания аспектов в требованиях минус штрафы за замечания ревью. Это вспомогательный индикатор, а не формальная оценка качества;
- матрица трассировки: какие FR не покрыты критериями приёмки и тестами;
- сверка новых endpoints с существующими API проекта (из загруженных OpenAPI и контекста).

## Безопасность

- **Аутентификация**: bearer-токены. В БД хранится только SHA-256 токена.
- **RBAC на уровне проекта**: `viewer` (чтение и экспорт) < `editor` (документы, чат, генерация) < `owner` (участники, аудит, удаление документов).
- **Изоляция проектов**: каждый запрос к данным фильтруется по `project_id`, контекст агента собирается только из документов своего проекта. Для чужого проекта API отвечает 404.
- **Шифрование**: при заданном `ANALYST_ENCRYPTION_KEY` шифруются содержимое документов, чанки, сообщения и артефакты.
- **Защита от prompt injection**: документы передаются модели внутри тегов как данные, промпты запрещают исполнять найденные в них инструкции. XML с DOCTYPE/ENTITY отклоняется.
- **Audit log**: пользователь, время, проект, действие, вход, артефакт, модель, версия. Доступен владельцу: `GET /projects/{id}/audit`.
- **Human-in-the-loop**: удаление документов, публикация требований и изменение существующей архитектуры проекта выполняются только после подтверждения (`/actions/{id}/confirm`).

## API (кратко)

| Метод | Путь | Назначение |
|---|---|---|
| GET/POST | `/api/v1/projects` | проекты пользователя / создать |
| GET/PATCH | `/api/v1/projects/{pid}` | проект и его контекст |
| POST | `/api/v1/projects/{pid}/members` | выдать роль |
| GET/POST | `/api/v1/projects/{pid}/documents` | документы / загрузить (multipart) |
| DELETE | `/api/v1/projects/{pid}/documents/{did}` | запрос на удаление (202, требует подтверждения) |
| GET | `/api/v1/projects/{pid}/knowledge?q=` | дерево базы знаний или поиск |
| POST | `/api/v1/projects/{pid}/tasks` | новая задача → AI Interview (202 + job) |
| GET | `/api/v1/projects/{pid}/tasks/{tid}` | сообщения, последние артефакты, активное задание |
| POST | `/api/v1/projects/{pid}/tasks/{tid}/messages` | сообщение в чат (202 + job) |
| POST | `/api/v1/projects/{pid}/tasks/{tid}/steps/{step}` | `discovery`, `requirements`, `review`, `architecture`, `architecture_review`, `api`, `sequence`, `activity`, `erd`, `component`, `tests`, `final_review`, `pipeline` |
| GET | `/api/v1/jobs/{id}` | статус задания |
| GET | `/api/v1/projects/{pid}/tasks/{tid}/artifacts` | история версий |
| POST | `/api/v1/projects/{pid}/artifacts/{aid}/publish` | запрос на публикацию |
| GET | `/api/v1/projects/{pid}/tasks/{tid}/export?format=` | `md`, `pdf`, `docx`, `openapi`, `json`, `mermaid`, `plantuml`, `jira` |
| GET | `/api/v1/projects/{pid}/actions` | действия, ожидающие подтверждения |
| POST | `/api/v1/projects/{pid}/actions/{aid}/confirm` \| `reject` | подтвердить / отклонить |
| GET | `/api/v1/projects/{pid}/audit` | журнал аудита |

Ошибки возвращаются в едином формате: `{"error": {"code": "...", "message": "..."}}`.

## Тесты

```bash
python -m pytest -q
```

Тесты используют фейковый LLM и не обращаются к API. Они покрывают:

- полный сценарий «возврат на другую карту»: интервью → требования → ревью → pipeline → экспорт;
- RBAC и изоляцию проектов;
- подтверждение действий;
- разбор форматов документов и поиск;
- рендеринг Mermaid, PlantUML и OpenAPI;
- все форматы экспорта;
- параметры запроса к Claude.

## Ограничения MVP и следующий этап

- Поиск по базе знаний лексический (BM25). Интерфейс `Retriever` позволяет подключить embeddings и векторную БД.
- Хранилище — SQLite, фоновые задания выполняются в пуле потоков процесса. Для продакшена: PostgreSQL, Redis и Celery.
- Jira и Confluence не входят в MVP. Сейчас доступен экспорт черновика Jira Issue (Summary, Description, BR, FR, API Contract, AC, Dependencies, Technical Notes). Создание задач в Jira будет требовать подтверждения, как и остальные критичные действия.
- PDF-сканы без текстового слоя не распознаются (нужен OCR).
