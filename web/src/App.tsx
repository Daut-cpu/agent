import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  Action, ApiError, Artifact, DocumentInfo, HistoryItem, Job, Project, Task, api, getToken, setToken, waitForJob,
} from "./api";
import { Markdown } from "./Markdown";

const STAGES: Record<string, string> = {
  interview: "AI Interview",
  ready: "Контекст собран",
  requirements: "Требования",
  review: "Ревью",
  architecture: "Архитектура",
  api: "API",
  tests: "Тест-кейсы",
  done: "Готово",
};

const JOB_LABELS: Record<string, string> = {
  discovery: "Анализирую задачу и готовлю вопросы",
  message: "Агент думает",
  requirements: "Формирую требования",
  review: "Проверяю требования",
  architecture: "Проектирую архитектуру",
  architecture_review: "Проверяю архитектурное решение",
  api: "Проектирую API",
  sequence: "Строю sequence diagram",
  activity: "Строю activity / BPMN",
  erd: "Строю ERD",
  tests: "Генерирую тест-кейсы",
  final_review: "Финальная проверка",
  pipeline: "Полный цикл: требования → ревью → архитектура → API → диаграмма → тесты",
};

const QUICK_STEPS: { step: string; label: string }[] = [
  { step: "requirements", label: "Требования" },
  { step: "review", label: "Ревью" },
  { step: "architecture", label: "Архитектура" },
  { step: "api", label: "API" },
  { step: "sequence", label: "Sequence" },
  { step: "tests", label: "Тест-кейсы" },
  { step: "final_review", label: "Финальная проверка" },
  { step: "pipeline", label: "Полный цикл ▶" },
];

const TABS: { key: string; label: string; kinds: string[] }[] = [
  { key: "requirements", label: "Requirements", kinds: ["requirements"] },
  { key: "review", label: "Review", kinds: ["review", "final_review"] },
  { key: "api", label: "API", kinds: ["api"] },
  { key: "diagrams", label: "Diagrams", kinds: ["sequence", "activity", "erd"] },
  { key: "architecture", label: "Architecture", kinds: ["architecture", "architecture_review"] },
  { key: "tests", label: "Test Cases", kinds: ["test_cases"] },
  { key: "risks", label: "Risks", kinds: [] },
  { key: "interview", label: "Interview", kinds: ["discovery"] },
  { key: "history", label: "History", kinds: [] },
];

const KIND_TITLES: Record<string, string> = {
  discovery: "AI Interview",
  requirements: "Требования",
  review: "Ревью требований",
  final_review: "Финальная проверка",
  api: "API-контракт",
  sequence: "Sequence Diagram",
  activity: "Activity / BPMN",
  erd: "ERD",
  architecture: "Архитектура",
  architecture_review: "Review архитектуры",
  test_cases: "Test Cases",
};

const CONTEXT_FIELDS: [string, string][] = [
  ["domain", "Домен"],
  ["tech_stack", "Технологический стек"],
  ["architecture", "Архитектура"],
  ["business_rules", "Бизнес-правила"],
  ["glossary", "Глоссарий"],
  ["integrations", "Интеграции"],
  ["existing_apis", "Существующие API"],
  ["database", "База данных"],
  ["nfr", "NFR"],
];

const EXPORTS: [string, string][] = [
  ["md", "Markdown"],
  ["pdf", "PDF"],
  ["docx", "DOCX"],
  ["openapi", "OpenAPI YAML"],
  ["json", "JSON"],
  ["mermaid", "Mermaid"],
  ["plantuml", "PlantUML"],
  ["jira", "Черновик Jira"],
];

function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

export default function App() {
  const [token, setTok] = useState(getToken());
  const [user, setUser] = useState<string>("");

  useEffect(() => {
    if (!token) return;
    api.me().then((u) => setUser(u.username)).catch(() => {
      setToken("");
      setTok("");
    });
  }, [token]);

  if (!token || !user) {
    return (
      <Login
        onLogin={(t) => {
          setToken(t);
          setTok(t);
        }}
      />
    );
  }
  return (
    <Workspace
      user={user}
      onLogout={() => {
        setToken("");
        setTok("");
        setUser("");
      }}
    />
  );
}

function Login({ onLogin }: { onLogin: (token: string) => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setToken(value.trim());
    try {
      await api.me();
      onLogin(value.trim());
    } catch {
      setToken("");
      setError("Токен не подошёл. Получите его командой python -m analyst create-user <имя>.");
    }
  };
  return (
    <div className="login">
      <form onSubmit={submit} className="card">
        <h1>System Analyst Agent</h1>
        <p className="muted">
          Агент помогает найти пропуски в требованиях и превратить бизнес-идею в техническое решение.
        </p>
        <label>
          Токен доступа
          <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="saa_…" autoFocus />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={!value.trim()}>
          Войти
        </button>
      </form>
    </div>
  );
}

function Workspace({ user, onLogout }: { user: string; onLogout: () => void }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [project, setProject] = useState<Project | null>(null);
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [taskId, setTaskId] = useState("");
  const [task, setTask] = useState<Task | null>(null);
  const [actions, setActions] = useState<Action[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("requirements");
  const [showContext, setShowContext] = useState(false);
  const [newProject, setNewProject] = useState(false);

  const fail = useCallback((e: unknown) => setError(e instanceof ApiError ? e.message : String(e)), []);

  const loadProjects = useCallback(async () => {
    const list = await api.projects();
    setProjects(list);
    setProjectId((current) => current || list[0]?.id || "");
  }, []);

  const loadProject = useCallback(async (pid: string) => {
    const [p, docs, ts, acts] = await Promise.all([api.project(pid), api.documents(pid), api.tasks(pid), api.actions(pid)]);
    setProject(p);
    setDocuments(docs);
    setTasks(ts);
    setActions(acts);
  }, []);

  const loadTask = useCallback(async (pid: string, tid: string) => {
    const t = await api.task(pid, tid);
    setTask(t);
    return t;
  }, []);

  useEffect(() => {
    loadProjects().catch(fail);
  }, [loadProjects, fail]);

  useEffect(() => {
    setTaskId("");
    setTask(null);
    if (projectId) loadProject(projectId).catch(fail);
  }, [projectId, loadProject, fail]);

  useEffect(() => {
    if (!projectId || !taskId) return;
    loadTask(projectId, taskId)
      .then((t) => {
        if (t.job) track(t.job);
      })
      .catch(fail);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  const track = async (started: Job, tid = taskId) => {
    setJob(started);
    try {
      const done = await waitForJob(started, setJob);
      if (done.status === "failed") setError(done.error);
    } catch (e) {
      fail(e);
    } finally {
      setJob(null);
      if (projectId && tid) await loadTask(projectId, tid).catch(fail);
      if (projectId) setTasks(await api.tasks(projectId));
    }
  };

  const refreshActions = async () => setActions(await api.actions(projectId));

  const startTask = async (problem: string) => {
    const created = await api.createTask(projectId, problem);
    setTasks(await api.tasks(projectId));
    setTaskId(created.id);
    setTask({ ...created, messages: [], artifacts: {} });
    await track(created.job, created.id);
  };

  const send = async (text: string) => {
    if (!taskId) return startTask(text);
    const res = await api.send(projectId, taskId, text);
    await loadTask(projectId, taskId);
    await track(res.job);
  };

  const runStep = async (step: string, instruction = "") => {
    const started = await api.step(projectId, taskId, step, instruction);
    await track(started);
  };

  const canEdit = project?.role === "editor" || project?.role === "owner";

  return (
    <div className="app">
      <header className="topbar">
        <strong>System Analyst Agent</strong>
        {project && <span className="muted">/ {project.name}</span>}
        <span className="spacer" />
        {task && Object.keys(task.artifacts ?? {}).length > 0 && (
          <select
            className="export"
            value=""
            onChange={(e) => e.target.value && api.download(projectId, taskId, e.target.value).catch(fail)}
          >
            <option value="">Экспорт…</option>
            {EXPORTS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        )}
        <span className="muted">{user}</span>
        <button className="link" onClick={onLogout}>
          Выйти
        </button>
      </header>

      {error && (
        <div className="toast" role="alert" onClick={() => setError("")}>
          {error} <span className="muted">(закрыть)</span>
        </div>
      )}

      <aside className="sidebar">
        <section>
          <h3>
            Projects{" "}
            <button className="link" onClick={() => setNewProject(true)}>
              + новый
            </button>
          </h3>
          {newProject && (
            <NewProject
              onCancel={() => setNewProject(false)}
              onCreate={async (name, description) => {
                const p = await api.createProject(name, description, {}).catch((e) => {
                  fail(e);
                  return null;
                });
                if (!p) return;
                setNewProject(false);
                await loadProjects();
                setProjectId(p.id);
              }}
            />
          )}
          <ul className="list">
            {projects.map((p) => (
              <li key={p.id} className={p.id === projectId ? "active" : ""} onClick={() => setProjectId(p.id)}>
                {p.name}
              </li>
            ))}
            {!projects.length && !newProject && <li className="muted">Создайте первый проект</li>}
          </ul>
          {project && (
            <button className="link" onClick={() => setShowContext(true)}>
              Контекст проекта
            </button>
          )}
        </section>

        {project && (
          <>
            <section>
              <h3>
                Tasks{" "}
                <button
                  className="link"
                  onClick={() => {
                    setTaskId("");
                    setTask(null);
                  }}
                >
                  + новая
                </button>
              </h3>
              <ul className="list">
                {tasks.map((t) => (
                  <li key={t.id} className={t.id === taskId ? "active" : ""} onClick={() => setTaskId(t.id)}>
                    <span className="ellipsis">{t.title}</span>
                    <span className="badge">{STAGES[t.stage] ?? t.stage}</span>
                  </li>
                ))}
              </ul>
            </section>
            <Documents
              projectId={projectId}
              documents={documents}
              canEdit={canEdit}
              onChange={async () => {
                setDocuments(await api.documents(projectId));
                await refreshActions();
              }}
              onError={fail}
            />
          </>
        )}
      </aside>

      <main className="chat">
        {actions.length > 0 && (
          <div className="actions">
            <strong>Требуется подтверждение</strong>
            {actions.map((a) => (
              <div key={a.id} className="action">
                <span>{a.summary}</span>
                <button
                  className="primary small"
                  onClick={async () => {
                    await api.decide(projectId, a.id, true).catch(fail);
                    await loadProject(projectId);
                    if (taskId) await loadTask(projectId, taskId);
                  }}
                >
                  Подтвердить
                </button>
                <button
                  className="small"
                  onClick={async () => {
                    await api.decide(projectId, a.id, false).catch(fail);
                    await refreshActions();
                  }}
                >
                  Отклонить
                </button>
              </div>
            ))}
          </div>
        )}
        {project ? (
          <Chat
            task={task}
            job={job}
            canEdit={canEdit}
            onSend={(t) => send(t).catch(fail)}
            onStep={(s) => runStep(s).catch(fail)}
          />
        ) : (
          <div className="empty">Выберите или создайте проект.</div>
        )}
      </main>

      <aside className="artifacts">
        <ArtifactsPanel
          projectId={projectId}
          task={task}
          tab={tab}
          onTab={setTab}
          canEdit={canEdit}
          busy={!!job}
          onPublish={async (a) => {
            await api.publish(projectId, a.id).catch(fail);
            await refreshActions();
          }}
          onStep={(s, instruction) => runStep(s, instruction).catch(fail)}
          onError={fail}
        />
      </aside>

      {showContext && project && (
        <ContextEditor
          project={project}
          canEdit={canEdit}
          onClose={() => setShowContext(false)}
          onSave={async (context, description) => {
            try {
              const res = await api.updateProject(projectId, context, description);
              if (res.pending_action) setError("Изменение архитектуры ждёт подтверждения (см. панель в чате).");
              setShowContext(false);
              await loadProject(projectId);
            } catch (e) {
              fail(e);
            }
          }}
        />
      )}
    </div>
  );
}

function NewProject({ onCreate, onCancel }: { onCreate: (name: string, d: string) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  return (
    <form
      className="stack"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) onCreate(name.trim(), description);
      }}
    >
      <input placeholder="Название" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      <input placeholder="Описание" value={description} onChange={(e) => setDescription(e.target.value)} />
      <div className="row">
        <button className="primary small" disabled={!name.trim()}>
          Создать
        </button>
        <button type="button" className="small" onClick={onCancel}>
          Отмена
        </button>
      </div>
    </form>
  );
}

function Documents(props: {
  projectId: string;
  documents: DocumentInfo[];
  canEdit: boolean;
  onChange: () => Promise<void>;
  onError: (e: unknown) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const upload = async (files: FileList | null) => {
    if (!files) return;
    setUploading(true);
    for (const file of Array.from(files)) {
      await api.upload(props.projectId, file).catch(props.onError);
    }
    setUploading(false);
    if (input.current) input.current.value = "";
    await props.onChange();
  };
  const byCategory = props.documents.reduce<Record<string, DocumentInfo[]>>((acc, d) => {
    (acc[d.category] ??= []).push(d);
    return acc;
  }, {});
  return (
    <section>
      <h3>
        Knowledge Base{" "}
        {props.canEdit && (
          <button className="link" onClick={() => input.current?.click()} disabled={uploading}>
            {uploading ? "загрузка…" : "+ документ"}
          </button>
        )}
      </h3>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        accept=".pdf,.docx,.xlsx,.txt,.md,.json,.yaml,.yml,.xml,.csv"
        onChange={(e) => upload(e.target.files)}
      />
      {Object.entries(byCategory).map(([category, docs]) => (
        <div key={category} className="kb-group">
          <div className="kb-category">{category}</div>
          <ul className="list">
            {docs.map((d) => (
              <li key={d.id} title={d.meta.endpoints?.join("\n") ?? ""}>
                <span className="ellipsis">{d.filename}</span>
                {props.canEdit && (
                  <button
                    className="link danger"
                    title="Удалить (потребует подтверждения)"
                    onClick={async () => {
                      await api.deleteDocument(props.projectId, d.id).catch(props.onError);
                      await props.onChange();
                    }}
                  >
                    ×
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {!props.documents.length && <p className="muted small">PDF, DOCX, XLSX, TXT, MD, JSON, OpenAPI, XML</p>}
    </section>
  );
}

function Chat(props: {
  task: Task | null;
  job: Job | null;
  canEdit: boolean;
  onSend: (text: string) => void;
  onStep: (step: string) => void;
}) {
  const [text, setText] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const messages = props.task?.messages ?? [];
  useEffect(() => end.current?.scrollIntoView({ behavior: "smooth" }), [messages.length, props.job?.status]);
  const busy = !!props.job;
  const last = messages[messages.length - 1];

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || busy) return;
    props.onSend(text.trim());
    setText("");
  };

  return (
    <>
      <div className="chat-head">
        {props.task ? (
          <>
            <strong className="ellipsis">{props.task.title}</strong>
            <span className="badge">{STAGES[props.task.stage] ?? props.task.stage}</span>
          </>
        ) : (
          <strong>Новая задача</strong>
        )}
      </div>
      <div className="messages">
        {!props.task && (
          <div className="empty">
            <p>Опишите бизнес-задачу. Агент не станет сразу писать ТЗ: сначала он найдёт пробелы и задаст вопросы.</p>
            <p className="muted">Например: «Нужно добавить возможность вернуть деньги клиенту на другую банковскую карту».</p>
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role} ${m.meta.kind === "error" ? "error" : ""}`}>
            <div className="author">{m.role === "user" ? m.author : "Agent"}</div>
            <Markdown text={m.content} />
          </div>
        ))}
        {busy && (
          <div className="msg assistant pending">
            <div className="author">Agent</div>
            <span className="spinner" /> {JOB_LABELS[props.job!.kind] ?? "Работаю"}…
          </div>
        )}
        <div ref={end} />
      </div>
      {props.task && props.canEdit && (
        <div className="quick">
          {last?.meta.ready && (
            <button className="primary small" disabled={busy} onClick={() => props.onStep("requirements")}>
              Да, сформировать требования
            </button>
          )}
          {QUICK_STEPS.map((q) => (
            <button key={q.step} className="small" disabled={busy} onClick={() => props.onStep(q.step)}>
              {q.label}
            </button>
          ))}
        </div>
      )}
      {props.canEdit ? (
        <form className="composer" onSubmit={submit}>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={props.task ? "Ответьте на вопросы или попросите: «добавь endpoint для статуса возврата»" : "Опишите бизнес-задачу…"}
            rows={3}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
            }}
          />
          <button className="primary" disabled={busy || !text.trim()}>
            Отправить
          </button>
        </form>
      ) : (
        <p className="muted composer">Роль viewer: только просмотр.</p>
      )}
    </>
  );
}

function risksMarkdown(artifacts: Record<string, Artifact>): string {
  const risks = [
    ...(artifacts.review?.content.review?.risks ?? []),
    ...(artifacts.architecture?.content.risks ?? []),
  ];
  if (!risks.length) return "Риски пока не выявлены. Запустите ревью требований или архитектуры.";
  const rows = risks.map(
    (r: any) => `| ${r.id} | **${r.title}**: ${r.description} | ${r.probability} | ${r.impact} | ${r.mitigation} |`,
  );
  return ["| ID | Риск | Вероятность | Влияние | Митигация |", "|---|---|---|---|---|", ...rows].join("\n");
}

function ArtifactsPanel(props: {
  projectId: string;
  task: Task | null;
  tab: string;
  onTab: (tab: string) => void;
  canEdit: boolean;
  busy: boolean;
  onPublish: (a: Artifact) => void;
  onStep: (step: string, instruction?: string) => void;
  onError: (e: unknown) => void;
}) {
  const artifacts = props.task?.artifacts ?? {};
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [viewing, setViewing] = useState<Artifact | null>(null);
  const [archText, setArchText] = useState("");

  useEffect(() => {
    setViewing(null);
    if (props.tab === "history" && props.task) {
      api.history(props.projectId, props.task.id).then(setHistory).catch(props.onError);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.tab, props.task?.id, props.task?.artifacts]);

  const current = TABS.find((t) => t.key === props.tab)!;
  const present = current.kinds.filter((k) => artifacts[k]);

  return (
    <>
      <div className="tabs">
        {TABS.map((t) => {
          const has = t.kinds.some((k) => artifacts[k]) || (t.key === "risks" && (artifacts.review || artifacts.architecture));
          return (
            <button key={t.key} className={`tab ${props.tab === t.key ? "active" : ""} ${has ? "has" : ""}`} onClick={() => props.onTab(t.key)}>
              {t.label}
            </button>
          );
        })}
      </div>
      <div className="artifact-body">
        {!props.task && <p className="muted">Артефакты задачи появятся здесь.</p>}
        {props.task && props.tab === "risks" && <Markdown text={risksMarkdown(artifacts)} />}
        {props.task && props.tab === "history" && (
          <>
            {viewing ? (
              <>
                <button className="link" onClick={() => setViewing(null)}>
                  ← к истории
                </button>
                <h3>
                  {KIND_TITLES[viewing.kind] ?? viewing.kind} v{viewing.version}
                </h3>
                <Markdown text={viewing.markdown} />
              </>
            ) : (
              <table className="history">
                <tbody>
                  {history.map((h) => (
                    <tr
                      key={h.id}
                      onClick={() => api.artifact(props.projectId, h.id).then(setViewing).catch(props.onError)}
                    >
                      <td>{KIND_TITLES[h.kind] ?? h.kind}</td>
                      <td>v{h.version}</td>
                      <td>{h.status === "published" ? "опубликовано" : "черновик"}</td>
                      <td className="muted">{fmtDate(h.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
        {props.task && props.tab === "architecture" && props.canEdit && (
          <details className="arch-review">
            <summary>Проверить своё архитектурное решение</summary>
            <textarea
              rows={3}
              value={archText}
              onChange={(e) => setArchText(e.target.value)}
              placeholder="Frontend → API Gateway → Service A → Service B → PostgreSQL"
            />
            <button
              className="small"
              disabled={props.busy || !archText.trim()}
              onClick={() => props.onStep("architecture_review", archText)}
            >
              Review
            </button>
          </details>
        )}
        {props.task && props.tab === "diagrams" && props.canEdit && (
          <div className="row">
            {["sequence", "activity", "erd"].map((k) => (
              <button key={k} className="small" disabled={props.busy} onClick={() => props.onStep(k)}>
                {artifacts[k] ? "Обновить" : "Построить"} {KIND_TITLES[k]}
              </button>
            ))}
          </div>
        )}
        {props.task && current.kinds.length > 0 && !present.length && (
          <p className="muted">Артефакт ещё не сформирован.</p>
        )}
        {present.map((k) => {
          const a = artifacts[k];
          return (
            <article key={k} className="artifact">
              <div className="artifact-head">
                <strong>{KIND_TITLES[k] ?? k}</strong>
                <span className="badge">v{a.version}</span>
                <span className={`badge ${a.status}`}>{a.status === "published" ? "опубликовано" : "черновик"}</span>
                <span className="spacer" />
                {props.canEdit && a.status !== "published" && ["requirements", "api", "architecture"].includes(k) && (
                  <button className="link" onClick={() => props.onPublish(a)}>
                    Опубликовать
                  </button>
                )}
              </div>
              <Markdown text={a.markdown} />
              {a.diagrams && (
                <details>
                  <summary>Исходник Mermaid</summary>
                  <pre>{a.diagrams.mermaid}</pre>
                </details>
              )}
            </article>
          );
        })}
      </div>
    </>
  );
}

function ContextEditor(props: {
  project: Project;
  canEdit: boolean;
  onClose: () => void;
  onSave: (context: Record<string, string>, description: string) => void;
}) {
  const [context, setContext] = useState<Record<string, string>>({ ...props.project.context });
  const [description, setDescription] = useState(props.project.description);
  return (
    <div className="modal-backdrop" onClick={props.onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>Контекст проекта «{props.project.name}»</h2>
        <p className="muted small">
          Агент учитывает этот контекст во всех запросах. Изменение существующей архитектуры требует подтверждения.
        </p>
        <label>
          Описание
          <textarea rows={2} value={description} disabled={!props.canEdit} onChange={(e) => setDescription(e.target.value)} />
        </label>
        {CONTEXT_FIELDS.map(([key, label]) => (
          <label key={key}>
            {label}
            <textarea
              rows={2}
              value={context[key] ?? ""}
              disabled={!props.canEdit}
              onChange={(e) => setContext({ ...context, [key]: e.target.value })}
            />
          </label>
        ))}
        <div className="row">
          {props.canEdit && (
            <button className="primary" onClick={() => props.onSave(context, description)}>
              Сохранить
            </button>
          )}
          <button onClick={props.onClose}>Закрыть</button>
        </div>
      </div>
    </div>
  );
}
