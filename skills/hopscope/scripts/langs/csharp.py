"""C# / ASP.NET Core: attribute-routed controllers ([Route] + [HttpGet]..., the [controller] and [action] tokens, routes
inherited from a base controller), minimal APIs (app.MapGet, MapGroup prefixes), imports through namespaces and
`using`, and the external systems a file uses (EF Core, SqlClient/Npgsql, Redis, AWS SDK, Kafka, HttpClient)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from langs.common import (blanked, literal_paths, string_consts, class_span, db_kind, humanize, javadoc_before, join_path, line_of, match_close, named_args,
                          shared_externals, split_top, strings_in)
from langs.typeindex import TypeIndex, declared_types

NAME = "csharp"
EXTENSIONS = (".cs",)
SERVICE_MARKERS = ("*.csproj", "*.sln")
TEST_FILE = re.compile(r"Tests?\.cs$")
BUILD_DIRS = {"bin": "*.csproj", "obj": "*.csproj"}
BUILD_FILES = ("*.csproj", "appsettings*.json")

MODS = (r"(?:(?:public|private|protected|internal|static|abstract|sealed|virtual|override|async|new|partial|readonly|extern|unsafe|"
        r"file|required)\s+)*")
CLASS_AT = re.compile(r"\s*" + MODS + r"(class|record|struct|interface)\s+(?:class\s+|struct\s+)?(\w+)")
METHOD_AT = re.compile(r"\s*" + MODS + r"[\w.<>\[\]?,\s]*?\b(\w+)\s*(?:<[^<>()]*>)?\s*\(")
CLASS_DECL = re.compile(r"\b(?:class|record|struct|interface)\s+(?:class\s+|struct\s+)?(\w+)")
BASE = re.compile(r"\b(?:class|record)\s+(\w+)\s*(?:<[^>{]*>)?\s*(?:\([^)]*\))?\s*:\s*([\w.]+)")
ATTR_START = re.compile(r"(?:^|(?<=[{};]))[ \t]*\[", re.M)  # at a line start, or after { } ; on the same line
HTTP = {"HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT", "HttpDelete": "DELETE", "HttpPatch": "PATCH", "HttpHead": "HEAD", "HttpOptions": "OPTIONS"}
MAP_CALL = re.compile(r"\.\s*Map(Get|Post|Put|Delete|Patch|Methods|HealthChecks|Group)?\(\s*\"([^\"]*)\"")
NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
USING = re.compile(r"^\s*(global\s+)?using\s+(static\s+)?(?:(\w+)\s*=\s*)?([\w.]+)\s*;", re.M)

EXT = {
    "sql": r"\bMicrosoft\.EntityFrameworkCore\b|\bDbContext\b|\bDbSet<|\bDapper\b|\bSqlConnection\b|\bNpgsql|SqlClient\b|UseSqlServer|UseMySql|MySqlConnector",
    "mongo": r"MongoDB\.Driver|IMongoCollection",
    "cache": r"StackExchange\.Redis|IConnectionMultiplexer|AddStackExchangeRedisCache",
    "sqs": r"Amazon\.SQS\b|IAmazonSQS|AmazonSQSClient|UsingAmazonSqs",
    "ses": r"Amazon\.SimpleEmail(V2)?\b|IAmazonSimpleEmailService(V2)?",
    "s3": r"Amazon\.S3\b|IAmazonS3|AmazonS3Client",
    "kafka": r"Confluent\.Kafka|\.UsingKafka|\bAddRider\b",
    "http": r"\bHttpClient\b|IHttpClientFactory|\bRefit\b|RestSharp|\bFlurl\b",
    "llm": r"Azure\.AI\.OpenAI|\bOpenAI\.|Microsoft\.SemanticKernel|\bAnthropic\b",
    "tracing": r"OpenTelemetry|\bSentry\b|ApplicationInsights",
}
EXT_RX = {k: re.compile(v) for k, v in EXT.items()}
DIRECT_DB = (("postgres", re.compile(r"Npgsql|UseNpgsql")), ("mssql", re.compile(r"SqlClient|UseSqlServer|SqlConnection\b")),
             ("mysql", re.compile(r"MySqlConnector|UseMySql")))


@dataclass
class State:
    types: TypeIndex = field(default_factory=TypeIndex)
    class_route: dict[str, str] = field(default_factory=dict)  # class name -> its [Route] template
    base_of: dict[str, str] = field(default_factory=dict)      # class name -> its base class
    global_usings: list[str] = field(default_factory=list)
    consts: dict[str, str] = field(default_factory=dict)       # Type.NAME -> const string (route templates)


@dataclass
class Decl:
    kind: str
    name: str
    pos: int
    attrs: list[tuple[str, str]]
    doc_at: int


def _attrs(code: str, bare: str, i: int) -> tuple[list[tuple[str, str]], int]:
    """The attributes of consecutive [..] sections starting at i, and where the declaration after them starts."""
    out: list[tuple[str, str]] = []
    while i < len(bare) and bare[i] == "[":
        close = match_close(bare, i, "[", "]")
        content = code[i + 1:close]
        if not re.match(r"\s*(assembly|module|return)\s*:", content):
            for part in split_top(content):
                m = re.match(r"\s*([\w.]+)", part)
                if not m:
                    continue
                name = re.sub(r"Attribute$", "", m.group(1).split(".")[-1])
                k = part.find("(")
                out.append((name, part[k + 1:part.rfind(")")] if k >= 0 else ""))
        i = close + 1
        while i < len(bare) and bare[i].isspace():
            i += 1
    return out, i


def declarations(text: str) -> list[Decl]:
    code, bare = blanked(text), blanked(text, True)
    out: list[Decl] = []
    end = 0
    for m in ATTR_START.finditer(bare):
        start = m.end() - 1
        if start < end:
            continue
        attrs, j = _attrs(code, bare, start)
        end = j
        for kind, rx in (("class", CLASS_AT), ("method", METHOD_AT)):
            d = rx.match(bare, j)
            if d and attrs:
                g = d.lastindex or 1
                out.append(Decl(kind, d.group(g), d.start(g), attrs, start))
                break
    return out


def _template(args: str, key: str = "template", consts: dict[str, str] | None = None) -> str | None:
    """The route template an attribute names: a string, or a constant (Routes.List) resolved through the file's and the
    repo's `const string`s; one that can't be resolved is {?}."""
    named = named_args(args)
    value = named.get("", "") if "" in named else named.get(key, named.get(key.capitalize(), ""))
    found = literal_paths(value, consts or {}) if value else []
    return found[0] if found else None


def _class_template(name: str, own: str | None, state: State | None) -> str:
    """The class's [Route] template, else the nearest base class's (attribute routes are inherited)."""
    if own is not None:
        return own
    seen = {name}
    while state is not None and name in state.base_of:
        name = state.base_of[name]
        if name in seen:
            break
        seen.add(name)
        if name in state.class_route:
            return state.class_route[name]
    return ""


def _verbs(d: Decl, consts: dict[str, str]) -> list[tuple[str, str | None]]:
    """(verb, template or None) pairs the method's attributes declare."""
    route = next((_template(a, consts=consts) for n, a in d.attrs if n == "Route"), None)
    out: list[tuple[str, str | None]] = []
    for n, a in d.attrs:
        if n in HTTP:
            own = _template(a, consts=consts)
            out.append((HTTP[n], own if own is not None else route))
        elif n == "AcceptVerbs":
            named = named_args(a)
            tmpl = strings_in(named.get("Route", ""))
            verbs = [s.upper() for part in split_top(a) if not re.match(r"\s*\w+\s*=", part) for s in strings_in(part)]
            out += [(v, tmpl[0] if tmpl else route) for v in verbs]
    if not out and any(n == "Route" for n, _ in d.attrs):
        out.append(("ANY", route))
    return out


def _full(class_tmpl: str, tmpl: str | None, controller: str, action: str) -> str:
    t = tmpl or ""
    path = t.lstrip("~") if t.startswith(("/", "~/")) else join_path(class_tmpl, t)
    path = re.sub(r"\[controller\]", controller, path, flags=re.I)
    return join_path("", re.sub(r"\[action\]", action, path, flags=re.I))


def _controller_routes(text: str, state: State | None) -> list[dict]:
    bare = blanked(text, True)
    consts = {**(state.consts if state is not None else {}), **string_consts(blanked(text))}
    decls = declarations(text)
    attrs_at = {d.pos: d for d in decls if d.kind == "class"}
    classes = []
    for m in CLASS_DECL.finditer(bare):
        s, e = class_span(bare, m.start(1))
        own = attrs_at.get(m.start(1))
        tmpl = next((_template(a, consts=consts) for n, a in own.attrs if n == "Route"), None) if own else None
        classes.append((s, e, m.group(1), _class_template(m.group(1), tmpl, state)))
    out = []
    for d in decls:
        if d.kind != "method":
            continue
        s, e, cname, ctmpl = min((c for c in classes if c[0] < d.pos < c[1]), key=lambda c: c[1] - c[0], default=(0, 0, "", ""))
        controller, action = re.sub(r"Controller$", "", cname), re.sub(r"Async$", "", d.name)
        doc = javadoc_before(text, d.doc_at)
        for verb, tmpl in _verbs(d, consts):
            out.append({"m": verb, "p": _full(ctmpl, tmpl, controller, action), "t": humanize(action), "d": doc, "l": line_of(text, d.pos)})
    return out


def _open_paren(code: str, close: int) -> int:
    depth = 0
    for k in range(close, -1, -1):
        depth += {")": 1, "(": -1}.get(code[k], 0)
        if depth == 0:
            return k
    return -1


def _chain_prefix(code: str, dot: int, groups: dict[str, str], depth: int = 0) -> str:
    """The MapGroup prefix of whatever precedes the '.' at dot: a group variable, or a chain of
    .MapGroup("/a").MapGroup("/b") calls."""
    k = dot - 1
    while k >= 0 and code[k].isspace():
        k -= 1
    if k < 0 or depth > 8:
        return ""
    if code[k] == ")":
        o = _open_paren(code, k)
        head = re.search(r"\.\s*MapGroup\s*$", code[max(0, o - 40):o])
        if o < 0 or not head:
            return ""
        arg = strings_in(code[o + 1:k])
        start = max(0, o - 40) + head.start()
        return join_path(_chain_prefix(code, start, groups, depth + 1), arg[0] if arg else "")
    word = re.search(r"(\w+)$", code[:k + 1])
    return groups.get(word.group(1), "") if word else ""


def _minimal_routes(text: str) -> list[dict]:
    code = blanked(text)
    groups: dict[str, str] = {}
    out = []
    for m in MAP_CALL.finditer(code):
        kind, path = m.group(1) or "ANY", m.group(2)
        if kind == "Group":
            stmt_end = code.find(";", m.end())
            after_call = match_close(code, code.find("(", m.start()), "(", ")")
            assign = re.search(r"\b(\w+)\s*=\s*[^;=]*$", code[max(0, m.start() - 300):m.start()])
            if assign and re.match(r"\s*;", code[after_call + 1:]) and stmt_end > after_call:
                groups[assign.group(1)] = join_path(_chain_prefix(code, m.start(), groups), path)
            continue
        verbs = [kind.upper()]
        if kind == "Methods":
            close = match_close(code, code.find("(", m.start()), "(", ")")
            verbs = [s.upper() for s in strings_in(code[m.end():close])] or ["ANY"]
        elif kind == "HealthChecks":
            verbs = ["GET"]
        handler = re.match(r"\s*,\s*([A-Z]\w*(?:\.\w+)?)\s*\)", code[m.end():])
        title = humanize(handler.group(1).split(".")[-1]) if handler else ""
        for v in verbs:
            out.append({"m": v, "p": join_path(_chain_prefix(code, m.start(), groups), path), "t": title, "d": "", "l": line_of(text, m.start())})
    return out


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    state = ctx.state if ctx is not None else None
    out = _controller_routes(text, state) if "[" in text else []
    return out + (_minimal_routes(text) if ".Map" in text else [])


def prepare(files: list[tuple[int, Path, str]]) -> State:
    st = State()
    for i, path, text in files:
        bare = blanked(text, True)
        ns = NAMESPACE.search(bare)
        types = declared_types(bare) or [path.stem]
        st.types.add(i, ns.group(1) if ns else "", types)
        for name, v in string_consts(blanked(text)).items():
            for t in types:
                st.consts.setdefault(f"{t}.{name}", v)
        for m in BASE.finditer(bare):
            st.base_of.setdefault(m.group(1), m.group(2).split(".")[-1])
        for d in declarations(text):
            tmpl = next((_template(a) for n, a in d.attrs if n == "Route"), None) if d.kind == "class" else None
            if tmpl is not None:
                st.class_route.setdefault(d.name, tmpl)
        st.global_usings += [m.group(4) for m in USING.finditer(bare) if m.group(1) and not m.group(3)]
    return st


def imports(path: Path, text: str, ctx) -> list[int]:
    st: State | None = ctx.state
    own = ctx.index.get(str(path.resolve()))
    if st is None or own is None:
        return []
    bare = blanked(text, True)
    ns = st.types.pkg_of.get(own, "")
    parts = ns.split(".") if ns else []
    pkgs = [".".join(parts[:k]) for k in range(len(parts), 0, -1)] + [""] + st.global_usings
    out: list[int] = []
    for m in USING.finditer(bare):
        name = m.group(4)
        if m.group(2) or m.group(3):
            hit = st.types.fq.get(name)
            if hit is not None:
                out.append(hit)
        else:
            pkgs.append(name)
    out += st.types.visible(USING.sub("", bare), own, list(dict.fromkeys(pkgs)))
    return sorted(set(out) - {own})


def externals(text: str, ctx=None) -> set[str]:
    found = shared_externals(text) | {k for k, rx in EXT_RX.items() if rx.search(text)}
    if "sql" in found:
        found.discard("sql")
        direct = next((k for k, rx in DIRECT_DB if rx.search(text)), None)
        found.add(direct or (db_kind(str(ctx.root), BUILD_FILES, default="mssql") if ctx is not None else "mssql"))
    return found


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    """The handler's call sequence (see langs/braceflow.py)."""
    from langs import braceflow
    return braceflow.flows(data, base, route_index)
