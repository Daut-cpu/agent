export type Role = "viewer" | "editor" | "owner";

export interface Project {
  id: string;
  name: string;
  description: string;
  context: Record<string, string>;
  role?: Role;
}

export interface DocumentInfo {
  id: string;
  filename: string;
  category: string;
  size: number;
  meta: { endpoints?: string[]; chunks?: number };
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  author: string;
  content: string;
  meta: { kind?: string; ready?: boolean; actions?: string[]; artifact_id?: string };
  created_at: number;
}

export interface Artifact {
  id: string;
  kind: string;
  version: number;
  status: "draft" | "published";
  model: string;
  created_at: number;
  content: any;
  markdown: string;
  diagrams?: { mermaid: string; plantuml: string };
}

export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed";
  error: string;
}

export interface Task {
  id: string;
  title: string;
  problem: string;
  stage: string;
  role?: Role;
  messages?: Message[];
  artifacts?: Record<string, Artifact>;
  job?: Job | null;
}

export interface Action {
  id: string;
  action: string;
  summary: string;
  status: string;
  requested_by: string;
}

export interface HistoryItem {
  id: string;
  kind: string;
  version: number;
  status: string;
  model: string;
  created_at: number;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const TOKEN_KEY = "saa_token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* приватный режим: токен живёт до перезагрузки */
  }
  memoryToken = token;
}

let memoryToken = getToken();

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${memoryToken}` };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(`/api/v1${path}`, { method, headers, body: payload });
  if (!res.ok) {
    let message = res.statusText;
    try {
      message = (await res.json()).error?.message ?? message;
    } catch {
      /* не JSON */
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  me: () => request<{ username: string }>("GET", "/me"),
  projects: () => request<Project[]>("GET", "/projects"),
  project: (id: string) => request<Project & { members: { username: string; role: Role }[] }>("GET", `/projects/${id}`),
  createProject: (name: string, description: string, context: Record<string, string>) =>
    request<Project>("POST", "/projects", { name, description, context }),
  updateProject: (id: string, context: Record<string, string>, description: string) =>
    request<Project & { pending_action: Action | null }>("PATCH", `/projects/${id}`, { context, description }),
  documents: (pid: string) => request<DocumentInfo[]>("GET", `/projects/${pid}/documents`),
  upload: (pid: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<DocumentInfo>("POST", `/projects/${pid}/documents`, form);
  },
  deleteDocument: (pid: string, did: string) => request<Action>("DELETE", `/projects/${pid}/documents/${did}`),
  tasks: (pid: string) => request<Task[]>("GET", `/projects/${pid}/tasks`),
  task: (pid: string, tid: string) => request<Task>("GET", `/projects/${pid}/tasks/${tid}`),
  createTask: (pid: string, problem: string) => request<Task & { job: Job }>("POST", `/projects/${pid}/tasks`, { problem }),
  send: (pid: string, tid: string, text: string) =>
    request<{ job: Job }>("POST", `/projects/${pid}/tasks/${tid}/messages`, { text }),
  step: (pid: string, tid: string, step: string, instruction = "") =>
    request<Job>("POST", `/projects/${pid}/tasks/${tid}/steps/${step}`, { instruction }),
  job: (id: string) => request<Job>("GET", `/jobs/${id}`),
  history: (pid: string, tid: string) => request<HistoryItem[]>("GET", `/projects/${pid}/tasks/${tid}/artifacts`),
  artifact: (pid: string, aid: string) => request<Artifact>("GET", `/projects/${pid}/artifacts/${aid}`),
  publish: (pid: string, aid: string) => request<Action>("POST", `/projects/${pid}/artifacts/${aid}/publish`),
  actions: (pid: string) => request<Action[]>("GET", `/projects/${pid}/actions`),
  decide: (pid: string, aid: string, approve: boolean) =>
    request<Action>("POST", `/projects/${pid}/actions/${aid}/${approve ? "confirm" : "reject"}`),

  async download(pid: string, tid: string, format: string): Promise<void> {
    const res = await fetch(`/api/v1/projects/${pid}/tasks/${tid}/export?format=${format}`, {
      headers: { Authorization: `Bearer ${memoryToken}` },
    });
    if (!res.ok) {
      const message = (await res.json().catch(() => null))?.error?.message ?? res.statusText;
      throw new ApiError(res.status, message);
    }
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const match = /filename\*=UTF-8''([^;]+)/.exec(disposition);
    const name = match ? decodeURIComponent(match[1]) : `export.${format}`;
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
};

export async function waitForJob(job: Job, onTick?: (job: Job) => void): Promise<Job> {
  let current = job;
  while (current.status === "queued" || current.status === "running") {
    await new Promise((r) => setTimeout(r, 1500));
    current = await api.job(current.id);
    onTick?.(current);
  }
  return current;
}
