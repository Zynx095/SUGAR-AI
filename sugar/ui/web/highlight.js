// Minimal, safe syntax highlighting for code blocks (no third-party code under the page's CSP).
// One pass of a sticky tokenizer: comments, strings, numbers, keywords, function calls. Every
// token is HTML-escaped; unknown languages get the generic C-family rules.

const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const KEYWORDS = {
  python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield match case self",
  js: "async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while with yield interface type enum implements private public protected readonly as",
  shell: "if then else elif fi for in do done while case esac function return export local echo cd set unset source exit",
  powershell: "function param if else elseif foreach for while return try catch finally throw switch begin process end",
  sql: "select from where insert into values update set delete create table drop alter join left right inner outer on group by order having limit as and or not null primary key foreign references index distinct union",
  generic: "if else for while return class struct public private protected static void int float double bool char new delete true false null nil fn let mut const var func package import use pub impl trait match enum interface go defer",
};
const ALIASES = { py: "python", python: "python", js: "js", javascript: "js", ts: "js", typescript: "js", jsx: "js",
  tsx: "js", mjs: "js", json: "js", sh: "shell", bash: "shell", zsh: "shell", shell: "shell", ps1: "powershell",
  powershell: "powershell", pwsh: "powershell", sql: "sql" };

function rules(lang) {
  const family = ALIASES[(lang || "").toLowerCase()] || "generic";
  const hashComments = family === "python" || family === "shell" || family === "powershell";
  const comment = family === "sql" ? String.raw`--[^\n]*` : hashComments ? String.raw`#[^\n]*`
    : String.raw`\/\/[^\n]*|\/\*[\s\S]*?\*\/`;
  const strings = family === "python" ? String.raw`"""[\s\S]*?"""|'''[\s\S]*?'''|[rbfu]*"(?:\\.|[^"\\\n])*"|[rbfu]*'(?:\\.|[^'\\\n])*'`
    : String.raw`"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|\x60(?:\\.|[^\x60\\])*\x60`;
  const words = new Set(KEYWORDS[family].split(" "));
  const pattern = new RegExp(`(${comment})|(${strings})|(\\b\\d[\\d_]*(?:\\.\\d+)?(?:e[+-]?\\d+)?\\b|\\b0x[0-9a-f]+\\b)|([A-Za-z_$][\\w$]*)(?=\\s*\\()|([A-Za-z_$][\\w$]*)`, "gi");
  return { pattern, words, caseless: family === "sql" };
}

export function highlight(code, lang) {
  const { pattern, words, caseless } = rules(lang);
  let out = "";
  let last = 0;
  for (const match of code.matchAll(pattern)) {
    out += escapeHtml(code.slice(last, match.index));
    const [token, comment, string, number, call, word] = match;
    let cls = null;
    if (comment) cls = "tok-c";
    else if (string) cls = "tok-s";
    else if (number) cls = "tok-n";
    else if (call) cls = words.has(caseless ? call.toLowerCase() : call) ? "tok-k" : "tok-f";
    else if (word && words.has(caseless ? word.toLowerCase() : word)) cls = "tok-k";
    out += cls ? `<span class="${cls}">${escapeHtml(token)}</span>` : escapeHtml(token);
    last = match.index + token.length;
  }
  return out + escapeHtml(code.slice(last));
}
