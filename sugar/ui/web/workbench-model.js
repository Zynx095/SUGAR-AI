// What the Engineering Workbench shows, derived from real session data only.
//
// Rules (tested in tests/ui/workbench-model.test.mjs):
//   * no active project → "NO ACTIVE PROJECT", and no task unless Claude Code is actually running;
//   * a session is shown only if it belongs to the active project or is live (running / awaiting
//     permission) — sessions from earlier runs on other repositories never appear;
//   * cost and tokens appear only when Claude Code reported them; otherwise they read "Not reported";
//   * the bench expands only while a task is live.

export const NO_PROJECT = "NO ACTIVE PROJECT";

const STATUS = {
  idle: ["Linked, idle", "idle"],
  running: ["Running", "live"],
  paused: ["Paused", "paused"],
  completed: ["Completed", "ok"],
  failed: ["Failed", "bad"],
  cancelled: ["Stopped", "paused"],
  waiting_permission: ["Needs your confirmation", "attention"],
};
const LIVE = new Set(["running", "waiting_permission"]);

export function formatDuration(ms) {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  const total = Math.round(ms / 1000);
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  if (minutes < 60) return `${minutes} min ${seconds} s`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min`;
}

export function formatCost(usd) {
  if (usd == null || !Number.isFinite(usd)) return null;
  return usd < 1 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

export function formatTokens(usage) {
  if (!usage) return null;
  const n = (v) => (v == null ? null : Number(v).toLocaleString("en-US"));
  const parts = [];
  if (usage.input_tokens != null) parts.push(`${n(usage.input_tokens)} input`);
  if (usage.output_tokens != null) parts.push(`${n(usage.output_tokens)} output`);
  if (usage.cache_read_input_tokens) parts.push(`${n(usage.cache_read_input_tokens)} cached`);
  return parts.length ? parts.join(", ") : null;
}

function normalisePath(path) {
  return path.split("\\").join("/").replace(/\/+$/, "").toLowerCase();
}

function samePath(a, b) {
  return !!a && !!b && normalisePath(a) === normalisePath(b);
}

/** Pick the session to show: a live one first, then the active project's. Never a stale foreign one. */
export function pickSession(sessions, activeProject) {
  const list = [...sessions].filter(Boolean);
  const live = list.filter((s) => LIVE.has(s.status)).sort((a, b) => b.last_activity - a.last_activity);
  if (activeProject) {
    const own = live.find((s) => samePath(s.project_path, activeProject.path));
    if (own) return own;
  }
  if (live.length) return live[0];
  if (!activeProject) return null;
  return list.find((s) => samePath(s.project_path, activeProject.path)) || null;
}

export function buildWorkbench({ activeProject = null, sessions = [], claude = null, now = Date.now() }) {
  const session = pickSession(sessions, activeProject);
  const claudeState = !claude || claude.status === "loading" ? "checking"
    : claude.status === "ready" ? "linked" : "unavailable";
  const view = {
    hasProject: !!activeProject,
    projectLabel: activeProject ? activeProject.name : NO_PROJECT,
    projectPath: activeProject ? activeProject.path : null,
    claude: {
      state: claudeState,
      text: claudeState === "linked" ? (claude.detail || "Claude Code") : claudeState === "checking"
        ? "Checking for Claude Code" : (claude?.detail || "Claude Code unavailable"),
    },
    expanded: !!session && LIVE.has(session.status),
    session: null,
  };
  if (!session) return view;

  const [statusText, statusKind] = STATUS[session.status] || [session.status, "idle"];
  const running = session.status === "running";
  const startedMs = session.task_started_at ? session.task_started_at * 1000 : null;
  const elapsed = running && startedMs ? now - startedMs : session.last_duration_ms;
  const checks = [];
  for (const [kind, label] of [["tests", "Tests"], ["build", "Build"]]) {
    const check = session.checks && session.checks[kind];
    if (check) checks.push({ kind, label, status: check.status, command: check.command });
  }
  view.session = {
    project: session.project_name,
    projectPath: session.project_path,
    foreign: !!activeProject && !samePath(session.project_path, activeProject.path),
    task: session.task || null,
    mode: session.mode === "analyze" ? "Read-only analysis" : "Implementation",
    status: session.status,
    statusText,
    statusKind,
    duration: formatDuration(elapsed),
    sessionId: session.claude_session_id || null,
    steps: (session.activity || []).slice(-8),
    checks,
    cost: formatCost(session.last_cost_usd),
    tokens: formatTokens(session.last_usage),
    summary: session.status === "completed" ? session.last_summary || null : null,
    error: session.status === "failed" ? session.last_error || null : null,
    history: (session.history || []).slice(-5).reverse().map((h) => ({
      task: h.task, status: h.status, duration: formatDuration(h.duration_ms), cost: formatCost(h.cost_usd),
    })),
  };
  return view;
}
