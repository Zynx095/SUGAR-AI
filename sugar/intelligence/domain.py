"""SID's engineering boundary: classify every request before anything acts on it.

SID is a software engineer, not a general assistant. Every request — typed or
spoken — passes through :func:`classify` and lands in exactly one category:

    CODING_QA         programming questions, explanations, debugging guidance
    PROJECT_ANALYSIS  inspecting a real repository (architecture, audit, failures)
    BUILD_EXECUTION   changing a real repository (create, implement, fix, refactor, test)
    NON_ENGINEERING   everything else — answered with a fixed refusal, no model, no tools

The classifier is deterministic and local (no model call), so the boundary
cannot be talked around by a prompt: a request that is not engineering never
reaches a language model, Claude Code or a tool.

It classifies *intent*, not keywords. A programming language in the sentence
is evidence, not proof:

    "What is Python?"                  → CODING_QA
    "What is the weather in Python?"   → NON_ENGINEERING  (asks for the weather)
    "Create a Python application"      → BUILD_EXECUTION
    "Tell me a joke about Java"        → NON_ENGINEERING  (asks for entertainment)
    "Build a weather app in React"     → BUILD_EXECUTION  (the weather is the app's subject)

Anything that *could* be execution but lacks a clear imperative and a clear
target is marked ``ambiguous``; ambiguous requests are never executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from sugar.intelligence.intents import normalize

REFUSAL = "I'm designed for software engineering and development. I can't assist with that request."


class EngineeringIntent(StrEnum):
    CODING_QA = "coding_qa"
    PROJECT_ANALYSIS = "project_analysis"
    BUILD_EXECUTION = "build_execution"
    NON_ENGINEERING = "non_engineering"


@dataclass(frozen=True)
class Classification:
    intent: EngineeringIntent
    reason: str
    ambiguous: bool = False  # never execute; ask what is meant instead
    courtesy: bool = False  # "thanks", "hello" — acknowledged without a model
    signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_engineering(self) -> bool:
        return self.intent != EngineeringIntent.NON_ENGINEERING

    @property
    def needs_repository(self) -> bool:
        return self.intent in (EngineeringIntent.PROJECT_ANALYSIS, EngineeringIntent.BUILD_EXECUTION)


@dataclass(frozen=True)
class RequestContext:
    """What the classifier may know beyond the words (never used to *widen* the boundary)."""

    has_project: bool = False  # the user selected a project in this session
    recent_coding: bool = False  # SID ran or discussed engineering work in the last few minutes
    project_named: bool = False  # the text names a known project ("…in Jiva")


def _words(*items: str) -> re.Pattern[str]:
    # Boundaries that also respect "c++", "c#" and "f#".
    return re.compile(r"(?<![\w#+])(?:" + "|".join(items) + r")(?![\w#+])")


# --------------------------------------------------------------------------- vocabulary

_LANGUAGES = _words(
    r"python", r"java", r"javascript", r"typescript", r"js", r"ts", r"c\+\+", r"cpp", r"c#", r"csharp", r"c sharp",
    r"golang", r"rust", r"kotlin", r"swift", r"ruby", r"php", r"sql", r"html", r"css", r"scss", r"bash", r"powershell",
    r"haskell", r"scala", r"dart", r"lua", r"perl", r"elixir", r"erlang", r"clojure", r"julia", r"matlab", r"fortran",
    r"cobol", r"assembly", r"solidity", r"zig", r"ocaml", r"f#", r"objective-c", r"vba", r"wasm", r"webassembly",
)
_FRAMEWORKS = _words(
    r"react", r"react native", r"vue", r"angular", r"svelte", r"next\.?js", r"nuxt", r"remix", r"astro", r"django",
    r"flask", r"fastapi", r"express", r"node(?:\.?js)?", r"deno", r"bun", r"spring(?: boot)?", r"rails", r"laravel",
    r"flutter", r"tailwind", r"bootstrap", r"pytorch", r"tensorflow", r"keras", r"numpy", r"pandas", r"scikit-learn",
    r"sklearn", r"jquery", r"redux", r"graphql", r"prisma", r"sqlalchemy", r"pydantic", r"electron", r"tauri",
    r"vite", r"webpack", r"babel", r"jest", r"vitest", r"pytest", r"junit", r"selenium", r"playwright", r"three\.?js",
    r"webgl", r"opengl", r"vulkan", r"unity", r"godot", r"pygame", r"qt", r"tkinter", r"\.net", r"dotnet", r"asp\.net",
    r"docker", r"kubernetes", r"k8s", r"terraform", r"ansible", r"nginx", r"postgres(?:ql)?", r"mysql", r"sqlite",
    r"mongodb", r"mongo", r"redis", r"kafka", r"rabbitmq", r"elasticsearch", r"supabase", r"firebase", r"aws",
    r"azure", r"gcp", r"vercel", r"netlify", r"heroku", r"linux", r"git", r"github", r"gitlab", r"npm", r"pnpm",
    r"yarn", r"pip", r"poetry", r"conda", r"cargo", r"maven", r"gradle", r"cmake", r"llvm", r"gcc", r"clang",
    r"vs code", r"vscode", r"claude code", r"langchain", r"openai api", r"hugging ?face", r"cuda", r"onnx",
)
_CONCEPTS = _words(
    r"code", r"coding", r"programming", r"program", r"programmer", r"software", r"source code", r"codebase",
    r"code base", r"repo", r"repository", r"function", r"functions", r"method", r"methods", r"class", r"classes",
    r"object-oriented", r"oop", r"variable", r"variables", r"constant", r"loop", r"for loop", r"while loop",
    r"recursion", r"recursive", r"algorithm", r"algorithms", r"data structure", r"data structures", r"array",
    r"arrays", r"linked list", r"hash ?map", r"hash table", r"binary tree", r"binary search", r"heap", r"stack",
    r"queue", r"graph traversal", r"dfs", r"bfs", r"dynamic programming", r"big o", r"time complexity",
    r"space complexity", r"pointer", r"pointers", r"reference", r"closure", r"closures", r"callback", r"promise",
    r"promises", r"async", r"await", r"asynchronous", r"concurrency", r"thread", r"threads", r"multithreading",
    r"mutex", r"deadlock", r"race condition", r"coroutine", r"generator", r"iterator", r"decorator", r"lambda",
    r"interface", r"inheritance", r"polymorphism", r"encapsulation", r"abstraction", r"generic", r"generics",
    r"type system", r"typing", r"type hint", r"type error", r"typeerror", r"syntax", r"syntax error", r"compiler",
    r"compile", r"compiling", r"interpreter", r"runtime", r"garbage collection", r"memory leak", r"segfault",
    r"segmentation fault", r"null pointer", r"nullpointerexception", r"undefined", r"exception", r"exceptions",
    r"stack ?trace", r"traceback", r"debug", r"debugging", r"debugger", r"breakpoint", r"bug", r"bugs", r"api",
    r"apis", r"rest(?:ful)? api", r"endpoint", r"endpoints", r"http", r"https", r"websocket", r"grpc", r"json",
    r"yaml", r"xml", r"regex", r"regular expression", r"database", r"databases", r"schema", r"query", r"queries",
    r"index(?:es)?", r"orm", r"migration", r"migrations", r"unit test", r"unit tests", r"integration test",
    r"test suite", r"test case", r"tests", r"tdd", r"mock", r"mocking", r"refactor", r"refactoring",
    r"design pattern", r"design patterns", r"singleton", r"factory pattern", r"dependency injection",
    r"microservice", r"microservices", r"monolith", r"architecture", r"software architecture", r"frontend",
    r"front end", r"backend", r"back end", r"full stack", r"fullstack", r"component", r"components", r"props",
    r"state management", r"hooks?", r"usestate", r"useeffect", r"dom", r"virtual dom", r"flexbox", r"css grid",
    r"responsive design", r"build", r"builds", r"build system", r"bundler", r"linter", r"lint", r"formatter",
    r"ci", r"ci/cd", r"pipeline", r"deployment", r"deploy", r"container", r"containers", r"virtual environment",
    r"venv", r"package", r"packages", r"dependency", r"dependencies", r"library", r"libraries", r"framework",
    r"frameworks", r"sdk", r"cli", r"command line", r"terminal command", r"shell script", r"script", r"scripts",
    r"oauth", r"jwt", r"authentication", r"authorization", r"hashing", r"encryption", r"cors", r"csrf",
    r"xss", r"sql injection", r"cache", r"caching", r"latency", r"throughput", r"load balancer", r"scalability",
    r"commit", r"commits", r"branch", r"branches", r"merge conflict", r"rebase", r"pull request", r"pr",
    r"version control", r"semver", r"null", r"none", r"nan", r"boolean", r"integer", r"string", r"strings",
    r"list comprehension", r"dictionary", r"tuple", r"enum", r"struct", r"module", r"modules", r"import",
    r"namespace", r"scope", r"stdout", r"stderr", r"exit code", r"stack overflow", r"buffer overflow",
    r"big-o", r"sorting", r"quicksort", r"merge sort", r"bubble sort", r"hashing", r"tokenizer", r"parser",
    r"lexer", r"ast", r"bytecode", r"jit", r"wasm", r"gpu kernel", r"shader", r"shaders", r"vertex",
    r"machine learning model", r"neural network", r"gradient descent", r"backpropagation", r"tensor",
    r"web app", r"webapp", r"web application", r"mobile app", r"desktop app", r"localhost", r"port",
    r"environment variable", r"env var", r"config file", r"dockerfile", r"makefile", r"package\.json",
    r"requirements\.txt", r"pyproject", r"tsconfig", r"eslint", r"prettier", r"typescript error", r"lint error",
    r"node_modules", r"__pycache__", r"dist folder", r"build folder", r"const", r"let and const", r"var and let",
    r"===", r"typeof", r"this keyword", r"null and undefined", r"malloc", r"printf", r"println", r"console\.log",
    r"stdin", r"main method", r"constructor", r"destructor", r"getter", r"setter", r"overloading", r"overriding",
    r"big o notation", r"hoisting", r"event loop", r"memoization", r"mutable", r"immutable", r"serialization",
)
_WEAK_TECH = _words(r"error", r"errors", r"crash", r"crashes", r"crashing", r"failing", r"fails", r"broken",
                    r"slow", r"performance", r"optimi[sz]e", r"server", r"app", r"application", r"website",
                    r"page", r"button", r"form", r"login", r"logs?", r"file", r"files", r"feature", r"features")
_FILE_REF = re.compile(r"\b[\w./\\-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|java|kt|go|rs|rb|php|cs|cpp|cc|c|h|hpp|swift|"
                       r"dart|lua|sql|html|css|scss|json|ya?ml|toml|ini|md|sh|ps1|bat|vue|svelte|gradle|xml|ipynb|"
                       r"lock|env)\b", re.IGNORECASE)
_CODE_SHAPE = re.compile(
    r"```|\bdef \w+\(|\bfunction \w*\(|=>|\bconst \w+ =|\blet \w+ =|\bvar \w+ =|\bpublic (?:static )?\w+|"
    r"#include|\bimport \w+|\bfrom \w+ import\b|\breturn\b.*;|\w+\(\)\s*[{:]|\bTraceback \(most recent call last\)|"
    r"\b\w+Error: |\bException in thread\b|\bat \w+\.\w+\(|\bnpm ERR!|\berror\[E\d+\]|\bSyntaxError\b|\{\s*\n"
)

# Activities that are not engineering, whatever language or tool is mentioned alongside.
_NON_ENGINEERING_TOPIC = _words(
    r"weather", r"forecast", r"temperature outside", r"rain(?:ing)?", r"umbrella", r"jokes?", r"funny", r"riddle",
    r"songs?", r"music", r"playlist", r"album", r"lyrics", r"spotify", r"movies?", r"films?", r"tv shows?",
    r"series to watch", r"netflix", r"anime", r"recipes?", r"cook(?:ing)?", r"bake", r"restaurants?", r"dinner",
    r"lunch", r"breakfast", r"sports?", r"cricket", r"football", r"soccer", r"basketball", r"ipl", r"world cup",
    r"news", r"headlines?", r"stock price", r"share price", r"bitcoin price", r"horoscope", r"zodiac", r"astrology",
    r"poems?", r"poetry", r"short story", r"bedtime story", r"novels?", r"travel", r"flights?", r"hotels?",
    r"vacation", r"holiday", r"remind me", r"reminders?", r"alarm", r"wake me", r"appointment", r"birthday",
    r"gift ideas?", r"girlfriend", r"boyfriend", r"relationship", r"dating", r"crush", r"diet", r"workout",
    r"exercise", r"gym", r"calories", r"doctor", r"medicine", r"symptoms?", r"headache", r"celebrity",
    r"capital of", r"president of", r"prime minister", r"who won", r"how old is", r"how tall is", r"translate",
    r"shopping", r"buy me", r"order (?:me )?(?:food|pizza)", r"pizza", r"wallpaper", r"volume", r"screenshot",
    r"meme", r"memes", r"dance", r"sing", r"meaning of life", r"philosophy", r"religion", r"politics?",
    r"election", r"game of thrones", r"marvel", r"superhero", r"youtube video", r"watch (?:a |the )?video",
    r"play (?:a |some |the )?(?:game|song|music|video)", r"time is it", r"what day is it", r"today's date",
)
_COURTESY = re.compile(
    r"^(?:hi|hello|hey|yo|good (?:morning|afternoon|evening)|thanks|thank you|thanks a lot|thank you so much|"
    r"cheers|great|nice|cool|awesome|ok|okay|got it|perfect|sid)$"
)
_SMALL_TALK = re.compile(
    r"^(?:how are you(?: doing)?|how's it going|what's up|whats up|who are you|what are you|what can you do|"
    r"are you there|you there|tell me about yourself|what is your name|what's your name|do you like .+|"
    r"how was your day|i'm bored|im bored|i am bored|let's chat|lets chat|talk to me)$"
)
_CAPABILITY_QUESTION = re.compile(r"^(?:what can you do|who are you|what are you|tell me about yourself|"
                                  r"what is your name|what's your name)$")

# --------------------------------------------------------------------------- intent structure

_BUILD_VERB = re.compile(
    r"^(?:(?:please|now|then|also|and)\s+)*(?:create|build|make|implement|add|write|generate|scaffold|set ?up|"
    r"initiali[sz]e|init|bootstrap|spin up|start (?:a|an|the|my) (?:new )?(?:project|app|application)|fix|repair|"
    r"patch|resolve|refactor|rewrite|restructure|rename|migrate|upgrade|update|bump|change|modify|edit|remove|"
    r"delete|drop|convert|port|optimi[sz]e|speed up|clean up|tidy|install|configure|deploy|ship|run|execute|"
    r"re-?run|test|commit|revert|undo|roll back|integrate|wire up|hook up|connect|extend|improve|redesign|"
    r"replace|move|split|extract|merge|document|containeri[sz]e|dockeri[sz]e|lint|format|debug|finish|complete|"
    r"continue|resume|apply)\b"
)
_WANT_BUILD = re.compile(
    r"^(?:i (?:want|need|would like)(?: you)? to|i'd like(?: you)? to|help me|we need to|let's|lets|"
    r"go ahead and|i want|i need)\s+(?:create|build|make|implement|add|write|fix|refactor|set ?up|deploy|"
    r"develop|code|program|migrate|scaffold)\b"
)
_BUILD_TARGET = _words(
    r"apps?", r"applications?", r"websites?", r"web ?sites?", r"site", r"web ?app", r"pages?", r"landing page",
    r"features?", r"components?", r"endpoints?", r"apis?", r"services?", r"server", r"backend", r"frontend", r"ui",
    r"cli", r"tool", r"script", r"bot", r"game", r"module", r"package", r"library", r"class", r"tests?",
    r"test suite", r"unit tests?", r"bugs?", r"issues?", r"errors?", r"warnings?", r"build", r"project", r"repo",
    r"repository", r"codebase", r"code", r"files?", r"database", r"schema", r"migrations?", r"auth",
    r"authentication", r"login", r"signup", r"sign-up", r"dashboard", r"form", r"button", r"routes?", r"router",
    r"pipeline", r"workflow", r"dockerfile", r"container", r"extension", r"plugin", r"layout", r"navbar",
    r"header", r"footer", r"modal", r"styles?", r"stylesheet", r"theme", r"animation", r"dependencies",
    r"dependency", r"types?", r"interface", r"readme", r"docs", r"documentation", r"changes", r"branch", r"commit",
    r"pr", r"pull request", r"crash", r"leak", r"hook", r"store", r"reducer", r"controller", r"model", r"view",
    r"middleware", r"handler", r"validation", r"logging", r"config", r"configuration", r"ci", r"workflow",
    r"function", r"method", r"implementation", r"logic", r"algorithm", r"query", r"queries", r"table", r"index",
    r"folder", r"directory", r"node_modules",
)
# Targets small enough to answer in the chat instead of editing a repository.
_SNIPPET_TARGET = re.compile(
    r"^(?:write|generate|create|make|give me|show me|implement)\s+(?:me\s+)?(?:a|an|the|some)?\s*"
    r"(?:(?:simple|quick|small|short|basic|recursive|iterative|generic|python|javascript|typescript|java|c\+\+|c#|"
    r"go|rust|sql|bash|regex|async)\s+)*"
    r"(?:function|method|snippet|example|one-liner|regex|regular expression|query|sql query|lambda|loop|"
    r"class|algorithm|code snippet|helper|type|interface|decorator|comprehension)\b"
)
_ANALYSIS_VERB = re.compile(
    r"\b(?:analy[sz]e|analysis|review|audit|inspect|examine|investigate|assess|evaluate|look (?:at|into|through)|"
    r"go through|explore|map out|walk me through|summari[sz]e|explain|understand|trace|diagnose|profile|"
    r"check|find out|figure out|what does|how does|how is .+ (?:structured|organi[sz]ed|wired)|plan|outline|"
    r"what(?:'s| is) (?:wrong|broken|failing)|why (?:is|are|does|did) .+ (?:fail|failing|crash|crashing|break|"
    r"broken|slow)|identify|list (?:the|all)|tell me (?:about|what))\b"
)
_REPO_REFERENCE = re.compile(
    r"\bthe (?:project|repo|repository|codebase|code ?base|build|tests|test suite|dependencies|app|application|"
    r"frontend|backend|branch|diff|readme|dockerfile|pipeline|ci)\b|"
    r"\b(?:this|my|our|your|that) (?:project|repo|repository|codebase|code ?base|code|app|application|"
    r"frontend|backend|server|build|tests?|test suite|dependencies|dependency tree|architecture|branch|module|"
    r"modules|api|endpoints?|database schema|schema|components?|source code|implementation|pipeline|ci|"
    r"config(?:uration)?|readme|diff|commits?|pr|pull request|bug|bugs|stack trace|migrations?|controllers?|"
    r"hooks?|state management|dockerfile)\b|"
    r"\bin (?:the|this|my|our) (?:project|repo|repository|codebase|code|app)\b"
)
# Nouns that mean a repository only when engineering is already in play ("this page" may be a web page).
_WEAK_REPO_REFERENCE = re.compile(
    r"\b(?:this|my|our|your|that|the) (?:package|packages|services?|pages?|website|site|source|workflow|setup|"
    r"structure|folder|directory|files?|changes|issue|issues|error|errors|logs?|login|auth(?:entication)?|ui|"
    r"layout|hero section|animation|styles?|routes?|models?|views?|store|deployment|navbar|header|footer|"
    r"form|button|modal|dashboard)\b"
)
_FOLLOW_UP = re.compile(
    r"^(?:fix (?:it|that|this|them|all|both|all of them|the rest|those)|do it|do that|run it|run them|"
    r"ship it|build it|test it|fix that too|go ahead|proceed|continue|carry on|keep going|try again|"
    r"retry|apply (?:it|that|the fix|the changes)|commit (?:it|that|them|the changes|everything)|"
    r"undo (?:that|it|the changes?)|revert (?:that|it)|fix (?:all|both) (?:of them|three|two))\b"
)
_FAILURE_REPORT = re.compile(
    r"^(?:the |my |our )?(?:build|tests?|test suite|ci|pipeline|deploy(?:ment)?|app|server|compile|"
    r"compilation|install|npm install|pip install|dev server)\s+(?:is |are |has |have |keeps |just )?"
    r"(?:failed|failing|fails|broke|broken|crashed|crashing|crashes|erroring|red|not working|doesn't work|"
    r"won't (?:start|run|build|compile))"
)
_QUESTION = re.compile(
    r"^(?:what|why|how|when|where|which|who|is|are|does|do|did|can|could|should|would|will|explain|describe|"
    r"tell me|define|compare|difference|differences|whats|what's|hows|how's)\b|\?$"
)
_HOW_TO_CODE = re.compile(
    r"\bhow (?:do|can|would|should|to) (?:i |you |we )?.+\b(?:in|with|using|via) (?:a |an |the )?"
    r"[\w#+.\- ]+$"
)
_BUILD_ARTIFACT_FOR_TOPIC = re.compile(  # "weather app", "music player", "joke generator", "recipe api"
    r"\b(?:app|application|api|website|site|bot|script|cli|service|server|dashboard|widget|player|generator|"
    r"scraper|tracker|clone|game|extension|plugin|backend|frontend|endpoint|database|model|program|tool|"
    r"component|library|sdk|parser|client|function|class|module)\b"
)
_PLAY_GAME = re.compile(r"\b(?:play|let's play|lets play) (?:a |some |the )?(?:game|games|chess|trivia)\b")


def _hits(pattern: re.Pattern[str] | str, text: str) -> list[str]:
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    return [m.group(0) for m in compiled.finditer(text)]


def _tech_signals(text: str, raw: str) -> tuple[list[str], list[str]]:
    strong = _hits(_LANGUAGES, text) + _hits(_FRAMEWORKS, text) + _hits(_CONCEPTS, text)
    strong += _FILE_REF.findall(raw)
    if _CODE_SHAPE.search(raw):
        strong.append("code")
    weak = _hits(_WEAK_TECH, text)
    return strong, weak


def classify(raw_text: str, context: RequestContext | None = None) -> Classification:
    """Decide which engineering category a request belongs to (or that it isn't engineering)."""
    context = context or RequestContext()
    raw = raw_text.strip()
    text = normalize(raw)
    if not text:
        return Classification(EngineeringIntent.NON_ENGINEERING, "empty request")

    if _COURTESY.match(text):
        return Classification(EngineeringIntent.NON_ENGINEERING, "courtesy", courtesy=True)
    if _CAPABILITY_QUESTION.match(text):
        return Classification(EngineeringIntent.NON_ENGINEERING, "question about SID", courtesy=True)
    if _SMALL_TALK.match(text):
        return Classification(EngineeringIntent.NON_ENGINEERING, "casual conversation")

    strong, weak = _tech_signals(text, raw)
    topic = _hits(_NON_ENGINEERING_TOPIC, text)
    has_code = "code" in strong and bool(_CODE_SHAPE.search(raw))
    strong_repo_ref = bool(_REPO_REFERENCE.search(text)) or bool(_FILE_REF.search(raw)) or context.project_named
    weak_repo_ref = bool(_WEAK_REPO_REFERENCE.search(text))
    build_verb = bool(_BUILD_VERB.match(text)) or bool(_WANT_BUILD.match(text))
    build_target = bool(_BUILD_TARGET.search(text))
    signals = tuple(dict.fromkeys(strong[:6] + topic[:3]))

    # 1. A non-engineering activity stays non-engineering unless the request is clearly *software about it*.
    if topic:
        software_about_topic = (
            (build_verb and _BUILD_ARTIFACT_FOR_TOPIC.search(text))
            or (bool(_BUILD_ARTIFACT_FOR_TOPIC.search(text)) and bool(strong) and not _PLAY_GAME.search(text)
                and not re.search(r"\b(?:jokes?|poems?|story|riddle)\b", text))
            or (_HOW_TO_CODE.search(text) and bool(strong))
            or has_code
        )
        if not software_about_topic:
            return Classification(EngineeringIntent.NON_ENGINEERING,
                                  f"non-engineering subject: {topic[0]}", signals=signals)

    # 2. Follow-ups ("fix it", "do it") only mean engineering when engineering work is in context.
    if _FOLLOW_UP.match(text):
        if context.recent_coding or context.has_project:
            return Classification(EngineeringIntent.BUILD_EXECUTION, "follow-up to engineering work",
                                  ambiguous=not context.recent_coding, signals=signals)
        return Classification(EngineeringIntent.BUILD_EXECUTION, "follow-up without engineering context",
                              ambiguous=True, signals=signals)

    # 3. Reports of failures in a project ("the build failed") ask SID to inspect the repository.
    if _FAILURE_REPORT.match(text) and not has_code:
        return Classification(EngineeringIntent.PROJECT_ANALYSIS, "failure report", signals=signals)

    in_context = context.has_project or context.recent_coding
    repo_ref = strong_repo_ref or (weak_repo_ref and (bool(strong) or build_verb or in_context))
    engineering = bool(strong) or repo_ref or (bool(weak) and (build_verb or in_context))
    if not engineering:
        return Classification(EngineeringIntent.NON_ENGINEERING, "no engineering subject", signals=signals)

    question = bool(_QUESTION.search(text)) and not build_verb

    # 4. Imperatives that change software.
    if build_verb and not question:
        snippet = bool(_SNIPPET_TARGET.match(text)) and not repo_ref
        if snippet:
            return Classification(EngineeringIntent.CODING_QA, "self-contained code snippet", signals=signals)
        if has_code and not repo_ref:
            return Classification(EngineeringIntent.CODING_QA, "code supplied in the message", signals=signals)
        analysis_only = re.match(r"^(?:check|test)\b", text) and not re.search(r"\b(?:run|fix)\b", text)
        if analysis_only and repo_ref and not re.match(r"^test\b", text):
            return Classification(EngineeringIntent.PROJECT_ANALYSIS, "inspection request", signals=signals)
        if build_target or repo_ref:
            return Classification(EngineeringIntent.BUILD_EXECUTION, "engineering change requested", signals=signals)
        return Classification(EngineeringIntent.BUILD_EXECUTION, "change requested without a clear target",
                              ambiguous=True, signals=signals)

    # 5. Repository inspection.
    if repo_ref and _ANALYSIS_VERB.search(text) and not has_code:
        return Classification(EngineeringIntent.PROJECT_ANALYSIS, "repository inspection", signals=signals)
    if repo_ref and question and not has_code and re.search(
            r"\b(?:the|this|my|our) (?:project|repo|repository|codebase|code ?base|app|application|build|tests?|"
            r"dependencies|architecture)\b", text):
        return Classification(EngineeringIntent.PROJECT_ANALYSIS, "question about a repository", signals=signals)

    # 6. Everything else with an engineering subject is a question to answer, never to execute.
    return Classification(EngineeringIntent.CODING_QA, "engineering question", signals=signals)


# --------------------------------------------------------------------------- secret redaction

_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"),  # Anthropic/OpenAI/Stripe style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
_ASSIGNMENT = re.compile(
    r"(?im)^(\s*(?:export\s+|set\s+|\$env:)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
    r"[A-Z0-9_]*\s*[=:]\s*)(['\"]?)([^\s'\"]{6,})\2"
)


def redact_secrets(text: str) -> str:
    """Mask credentials before anything reaches the chat, the UI or speech."""
    if not text:
        return text
    redacted = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]{m.group(2)}", text)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted
