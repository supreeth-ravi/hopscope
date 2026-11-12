"""Java and Kotlin: Spring MVC / Spring Boot, Micronaut and JAX-RS controllers, Ktor routing blocks, imports resolved
through package declarations, and the external systems a file uses (JDBC/JPA, Redis, AWS SDK, Kafka, HTTP clients)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from langs import openapi
from langs.common import (blanked, class_span, db_kind, humanize, javadoc_before, join_path, line_of, literal_paths, match_close, named_args,
                          shared_externals, string_consts)
from langs.typeindex import TypeIndex, declared_types

NAME = "java"
EXTENSIONS = (".java", ".kt")
SERVICE_MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
TEST_FILE = re.compile(r"(Tests?|IT|Spec)\.(java|kt)$")
BUILD_DIRS = {"target": "pom.xml", "out": "build.gradle*"}
BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "src/main/resources/application*.properties", "src/main/resources/application*.y*ml")

ANNOT = re.compile(r"@([A-Za-z_][\w.]*)")
MODS = (r"(?:(?:public|private|protected|internal|abstract|final|open|override|static|synchronized|default|suspend|inline|operator|data|"
        r"sealed|inner|value|annotation|enum|native|transient|volatile|const|lateinit|external|tailrec|infix)\s+)*")
MODS_RX = re.compile(MODS)
CLASS_AT = re.compile(r"\s*" + MODS + r"(class|interface|object|record|enum)\s+(\w+)")
FUN_AT = re.compile(r"\s*" + MODS + r"fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(\w+)\s*\(")
METHOD_AT = re.compile(r"\s*" + MODS + r"(?:<[^>]*>\s*)?[\w.<>\[\]?,\s]*?\b(\w+)\s*\(")
SPRING = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": ""}
MICRONAUT = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "Head": "HEAD", "Options": "OPTIONS"}
JAXRS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
CLIENTS = {"FeignClient", "Client", "HttpExchange", "RegisterRestClient"}  # interfaces that call other services
KTOR = re.compile(r"\b(route|get|post|put|delete|patch|head|options)\s*(?:\(\s*\"([^\"]*)\"\s*\))?\s*\{")
PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*(?:as\s+\w+)?\s*;?\s*$", re.M)

EXT = {
    "sql": r"\b(javax|jakarta)\.persistence\b|org\.springframework\.data\.(jpa|jdbc|repository)\b|org\.springframework\.jdbc\b|\bjava\.sql\.|"
           r"\bJdbcTemplate\b|\bEntityManager\b|\b(Jpa|Crud|PagingAndSorting|ListCrud)Repository\b|org\.jooq|org\.hibernate|io\.r2dbc|exposed\.sql",
    "mongo": r"springframework\.data\.mongodb|com\.mongodb|\bMongo(Repository|Template)\b",
    "cache": r"redis\.clients\.jedis|io\.lettuce|springframework\.data\.redis|\b(String)?RedisTemplate\b|org\.redisson|\bJedis(Pool)?\b",
    "sqs": r"awssdk\.services\.sqs|amazonaws\.services\.sqs|\bSqs(Async)?Client\b|\bAmazonSQS(Async)?\b|\bSqsTemplate\b|awspring\.cloud\.sqs|@SqsListener",
    "ses": r"awssdk\.services\.sesv?2?\b|amazonaws\.services\.simpleemail|\bSesV?2?(Async)?Client\b|\bAmazonSimpleEmailService\b",
    "s3": r"awssdk\.services\.s3\b|amazonaws\.services\.s3\b|\bS3(Async)?Client\b|\bAmazonS3\b|\bS3Template\b",
    "cognito": r"cognitoidentityprovider",
    "kafka": r"org\.apache\.kafka|\bKafkaTemplate\b|@KafkaListener|springframework\.kafka",
    "http": r"\bRestTemplate\b|\bWebClient\b|\bRestClient\b|@FeignClient|java\.net\.http\.HttpClient|\bOkHttpClient\b|io\.ktor\.client|retrofit2",
    "llm": r"\bcom\.openai\b|\bcom\.anthropic\b|langchain4j|springframework\.ai\b",
    "tracing": r"io\.opentelemetry|io\.sentry|micrometer\.tracing",
}
EXT_RX = {k: re.compile(v) for k, v in EXT.items()}
DIRECT_DB = (("postgres", re.compile(r"jdbc:postgresql|org\.postgresql")), ("mysql", re.compile(r"jdbc:mysql|com\.mysql")))


@dataclass
class Decl:
    kind: str           # "class" or "method"
    name: str
    pos: int            # where the name is
    annots: list[tuple[str, str]]  # (simple name, argument text)
    doc_at: int         # where the annotation group starts


def declarations(text: str) -> list[Decl]:
    """Annotated classes and methods in source order, with their annotations."""
    code, bare = blanked(text), blanked(text, True)
    out: list[Decl] = []
    end = 0
    for m in ANNOT.finditer(bare):
        if m.start() < end or m.group(1) == "interface":
            continue
        annots, j = [], m.start()
        while True:
            a = ANNOT.match(bare, j)
            if not a or a.group(1) == "interface":
                break
            k = a.end()
            args = ""
            while k < len(bare) and bare[k] in " \t":
                k += 1
            if k < len(bare) and bare[k] == "(":
                close = match_close(bare, k, "(", ")")
                args, k = code[k + 1:close], close + 1
            annots.append((a.group(1).split(".")[-1], args))
            j = k
            while j < len(bare) and bare[j].isspace():
                j += 1
            mods = MODS_RX.match(bare, j)  # public @ResponseBody Vets list()
            if mods and mods.end() > j and bare.startswith("@", mods.end()):
                j = mods.end()
        end = j
        for kind, rx in (("class", CLASS_AT), ("method", FUN_AT), ("method", METHOD_AT)):
            d = rx.match(bare, j)
            if d:
                out.append(Decl(kind, d.group(d.lastindex or 1), d.start(d.lastindex or 1), annots, m.start()))
                break
    return out


def _paths(args: str, keys: tuple[str, ...], consts: dict[str, str]) -> list[str]:
    named = named_args(args)
    for k in keys:
        if k in named:
            return literal_paths(named[k], consts) or [""]
    return [""]


@dataclass
class Owner:
    start: int
    end: int
    prefixes: tuple[str, ...] = ("",)
    client: bool = False      # an interface that calls other services (Feign, Micronaut @Client)
    controller: bool = False  # @RestController / @Controller


NO_OWNER = Owner(0, 0)


def _class_info(text: str, decls: list[Decl], micronaut: bool, consts: dict[str, str]) -> list[Owner]:
    """The body span, path prefix and role of each annotated class."""
    bare = blanked(text, True)
    out = []
    for d in decls:
        if d.kind != "class":
            continue
        names = {a for a, _ in d.annots}
        prefixes: list[str] = [""]
        for a, args in d.annots:
            if a == "RequestMapping" or (a == "Path") or (a == "Controller" and micronaut):
                prefixes = _paths(args, ("value", "path", "uri", ""), consts)
        start, stop = class_span(bare, d.pos)
        out.append(Owner(start, stop, tuple(prefixes), bool(names & CLIENTS), bool(names & {"RestController", "Controller"})))
    return out


def _method_routes(d: Decl, micronaut: bool, consts: dict[str, str]) -> list[tuple[str, str]]:
    """(method, path) pairs one annotated method serves, before the class prefix."""
    out = []
    sub = next((_paths(args, ("value", ""), consts)[0] for a, args in d.annots if a == "Path"), "")
    for a, args in d.annots:
        if a in SPRING:
            named = named_args(args)
            verbs = [SPRING[a]] if SPRING[a] else re.findall(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b", named.get("method", "")) or ["ANY"]
            out += [(v, p) for p in _paths(args, ("value", "path", ""), consts) for v in verbs]
        elif micronaut and a in MICRONAUT:
            out += [(MICRONAUT[a], p) for p in _paths(args, ("value", "uri", ""), consts)]
        elif a in JAXRS:
            out.append((a, sub))
    return out


def _ktor_routes(text: str) -> list[dict]:
    code, bare = blanked(text), blanked(text, True)
    spans: list[tuple[int, int, str]] = []
    out = []
    for m in KTOR.finditer(code):
        brace = m.end() - 1
        prefix = "".join("/" + p.strip("/") for s, e, p in spans if s < m.start() < e and p.strip("/"))
        if m.group(1) == "route":
            spans.append((brace, match_close(bare, brace), m.group(2) or ""))
        else:
            out.append({"m": m.group(1).upper(), "p": join_path(prefix, m.group(2) or ""), "t": "", "d": "", "l": line_of(text, m.start())})
    return out


def _generated(d: Decl, owner: Owner, ctx) -> list[dict]:
    """A controller method implementing an interface generated from an OpenAPI spec: the spec names its route."""
    if ctx is None or not owner.controller or "Override" not in {a for a, _ in d.annots}:
        return []
    op = openapi.operations(str(ctx.root)).get(d.name)
    if not op:
        return []
    return [{"m": op[0], "p": join_path(pre, op[1]), "t": humanize(d.name), "d": op[2]} for pre in owner.prefixes]


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    if "@" not in text and "io.ktor" not in text:
        return []
    micronaut = "io.micronaut" in text
    consts = {**(ctx.state.consts if ctx is not None and ctx.state is not None else {}), **string_consts(blanked(text))}
    decls = declarations(text)
    classes = _class_info(text, decls, micronaut, consts)
    out = []
    for d in decls:
        if d.kind != "method":
            continue
        owner = min((c for c in classes if c.start < d.pos < c.end), key=lambda c: c.end - c.start, default=NO_OWNER)
        if owner.client:
            continue
        doc = javadoc_before(text, d.doc_at)
        found = [{"m": verb, "p": join_path(pre, sub), "t": humanize(d.name), "d": doc}
                 for verb, sub in _method_routes(d, micronaut, consts) for pre in owner.prefixes]
        for r in found or _generated(d, owner, ctx):
            out.append({**r, "l": line_of(text, d.pos)})
    if "io.ktor" in text:
        out += _ktor_routes(text)
    return out


def prepare(files: list[tuple[int, Path, str]]) -> TypeIndex:
    ti = TypeIndex()
    for i, path, text in files:
        bare = blanked(text, True)
        pkg = PACKAGE.search(bare)
        types = declared_types(bare) or [path.stem]
        ti.add(i, pkg.group(1) if pkg else "", types)
        ti.add_consts(types, string_consts(blanked(text)))
    return ti


def imports(path: Path, text: str, ctx) -> list[int]:
    ti: TypeIndex | None = ctx.state
    own = ctx.index.get(str(path.resolve()))
    if ti is None or own is None:
        return []
    bare = blanked(text, True)
    out: list[int] = []
    pkgs = [ti.pkg_of.get(own, "")]
    for m in IMPORT.finditer(bare):
        name = m.group(2)
        if m.group(3):
            pkgs.append(name)
            continue
        hit = ti.fq.get(name)
        if hit is None and "." in name:
            hit = ti.fq.get(name.rsplit(".", 1)[0])  # static import or nested class
        if hit is not None:
            out.append(hit)
    body = IMPORT.sub("", bare)
    out += ti.visible(body, own, pkgs)
    return sorted(set(out) - {own})


def externals(text: str, ctx=None) -> set[str]:
    found = shared_externals(text) | {k for k, rx in EXT_RX.items() if rx.search(text)}
    if "sql" in found:
        found.discard("sql")
        direct = next((k for k, rx in DIRECT_DB if rx.search(text)), None)
        found.add(direct or (db_kind(str(ctx.root), BUILD_FILES) if ctx is not None else "postgres"))
    return found


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    """The handler's call sequence (see langs/braceflow.py)."""
    from langs import braceflow
    return braceflow.flows(data, base, route_index)
