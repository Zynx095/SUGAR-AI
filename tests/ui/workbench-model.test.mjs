// The workbench never invents a project, a task, a cost or a token count.
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildWorkbench, NO_PROJECT, formatDuration, formatCost, formatTokens, pickSession } from "../../sugar/ui/web/workbench-model.js";

const linked = { status: "ready", detail: "Claude Code 2.1.326" };
const session = (over = {}) => ({
  project_name: "shop", project_path: "D:\\work\\shop", status: "completed", task: "add a health endpoint", mode: "work",
  last_activity: 10, activity: ["Reading app.py"], checks: {}, history: [], claude_session_id: "abc-123",
  last_cost_usd: null, last_usage: null, last_duration_ms: 42000, ...over,
});

test("no project: NO ACTIVE PROJECT and no task", () => {
  const view = buildWorkbench({ activeProject: null, sessions: [], claude: linked });
  assert.equal(view.projectLabel, NO_PROJECT);
  assert.equal(view.projectPath, null);
  assert.equal(view.session, null);
  assert.equal(view.expanded, false);
});

test("a stale session from another repository is never shown", () => {
  const stale = session({ project_name: "Q-Shield", project_path: "D:\\old\\Q-Shield", status: "completed" });
  assert.equal(buildWorkbench({ activeProject: null, sessions: [stale], claude: linked }).session, null);
  const other = buildWorkbench({ activeProject: { name: "shop", path: "D:\\work\\shop" }, sessions: [stale], claude: linked });
  assert.equal(other.session, null);
  assert.equal(other.projectLabel, "shop");
});

test("a running task anywhere is shown and expands the bench", () => {
  const live = session({ status: "running", task_started_at: 100, last_duration_ms: null });
  const view = buildWorkbench({ activeProject: null, sessions: [live], claude: linked, now: 130000 });
  assert.equal(view.expanded, true);
  assert.equal(view.session.statusText, "Running");
  assert.equal(view.session.duration, "30 s");
  assert.equal(view.projectLabel, NO_PROJECT, "a running task does not invent an active project");
});

test("the active project's own session wins over an older live one", () => {
  const mine = session({ status: "running", last_activity: 5 });
  const theirs = session({ project_name: "blog", project_path: "D:\\work\\blog", status: "running", last_activity: 9 });
  assert.equal(pickSession([theirs, mine], { name: "shop", path: "D:/work/shop/" }).project_name, "shop");
});

test("cost and tokens only when reported", () => {
  const active = { name: "shop", path: "D:\\work\\shop" };
  const none = buildWorkbench({ activeProject: active, sessions: [session()], claude: linked });
  assert.equal(none.session.cost, null);
  assert.equal(none.session.tokens, null);
  const reported = buildWorkbench({ activeProject: active, claude: linked,
    sessions: [session({ last_cost_usd: 0.0123, last_usage: { input_tokens: 1500, output_tokens: 320 } })] });
  assert.equal(reported.session.cost, "$0.0123");
  assert.equal(reported.session.tokens, "1,500 input, 320 output");
});

test("checks reflect the reported command outcome", () => {
  const view = buildWorkbench({ activeProject: { name: "shop", path: "D:\\work\\shop" }, claude: linked,
    sessions: [session({ checks: { tests: { status: "failed", command: "pytest -q" } } })] });
  assert.deepEqual(view.session.checks.map((c) => [c.label, c.status]), [["Tests", "failed"]]);
});

test("Claude Code availability", () => {
  assert.equal(buildWorkbench({ claude: null }).claude.state, "checking");
  assert.equal(buildWorkbench({ claude: { status: "unavailable", detail: "claude CLI not found" } }).claude.text, "claude CLI not found");
  assert.equal(buildWorkbench({ claude: linked }).claude.state, "linked");
});

test("formatting", () => {
  assert.equal(formatDuration(null), null);
  assert.equal(formatDuration(65000), "1 min 05 s");
  assert.equal(formatCost(1.5), "$1.50");
  assert.equal(formatTokens({}), null);
});
