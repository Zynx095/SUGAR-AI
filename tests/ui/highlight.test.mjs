import { test } from "node:test";
import assert from "node:assert/strict";
import { highlight } from "../../sugar/ui/web/highlight.js";
import { renderMarkdown } from "../../sugar/ui/web/markdown.js";

test("python tokens are classified and everything is escaped", () => {
  const html = highlight('def f(x):\n    return "<b>" # done', "python");
  assert.match(html, /<span class="tok-k">def<\/span>/);
  assert.match(html, /<span class="tok-s">"&lt;b&gt;"<\/span>/);
  assert.match(html, /<span class="tok-c"># done<\/span>/);
  assert.doesNotMatch(html, /<b>/);
});

test("code blocks get copy controls and long ones start collapsed", () => {
  const long = "```js\n" + Array.from({ length: 30 }, (_, i) => `const a${i} = ${i};`).join("\n") + "\n```";
  const html = renderMarkdown(long);
  assert.match(html, /class="code-block collapsed"/);
  assert.match(html, /class="copy"/);
  assert.match(html, /Show all 30 lines/);
  assert.doesNotMatch(renderMarkdown("```\nx\n```"), /collapsed/);
});

test("markdown never passes raw html through", () => {
  assert.doesNotMatch(renderMarkdown('<img src=x onerror="alert(1)">'), /<img/);
});
