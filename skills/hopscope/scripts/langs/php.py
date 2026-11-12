"""PHP: Laravel route files, Symfony route attributes/annotations and Slim/Lumen routes (langs/phproutes.py), imports
through `use` + namespace declarations (falling back to composer.json's PSR-4 map) and literal include/require paths,
the external systems a file uses (Eloquent/DB facade/Doctrine/PDO, Redis/Cache, AWS SDK, Kafka, HTTP clients), and
controller call flows (langs/phpflow.py).

A Laravel route points at its controller method when the class and method are found, so call flows start there.
The SQL database comes from config/database.php's default connection (pgsql -> postgres, mysql/mariadb -> mysql,
sqlsrv -> mssql; sqlite draws none), else from composer.json, else postgres. .env files are never read. Files that only
return a config array (Laravel's config/*.php name every driver, used or not) get no externals."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from langs import phproutes
from langs.common import humanize, line_of, read_contained, shared_externals
from langs.phptext import blank
from langs.typeindex import TypeIndex

NAME = "php"
EXTENSIONS = (".php",)
SERVICE_MARKERS = ("composer.json",)
TEST_FILE = re.compile(r"Test\.php$|^test_.*\.php$")
BUILD_DIRS: dict[str, str] = {}

NAMESPACE = re.compile(r"^\s*namespace\s+([\w\\]+)\s*[;{]", re.M)
USE = re.compile(r"^\s*use\s+(?:function\s+|const\s+)?\\?([\w\\]+)(?:\s+as\s+(\w+))?\s*;", re.M)
USE_GROUP = re.compile(r"^\s*use\s+\\?([\w\\]+)\\\{([^}]*)\}\s*;", re.M)
TYPE_DECL = re.compile(r"\b(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)")
EXTENDS = re.compile(r"\bclass\s+(\w+)(?:\s+extends\s+\\?([\w\\]+))?")
METHOD = re.compile(r"\bfunction\s+&?\s*(\w+)\s*\(")
INCLUDE = re.compile(r"\b(?:require|include)(?:_once)?\s*\(?\s*(dirname\s*\(\s*__DIR__\s*\)\s*\.\s*|__DIR__\s*\.\s*)?['\"]([^'\"]+\.php)['\"]")
MODEL_BASES = {"Model", "Authenticatable", "Pivot", "MorphPivot", "Eloquent"}
DEFAULT_CONTROLLERS = "App\\Http\\Controllers"

EXT = {
    "sql": r"\bIlluminate\\Database\b|\bDB::|\bEloquent\b|\bDoctrine\\(ORM|DBAL|Persistence|Bundle\\DoctrineBundle)|\bEntityManager(Interface)?\b|"
           r"\bServiceEntityRepository\b|\bnew\s+\\?PDO\b|\bPDO::",
    "cache": r"Facades\\(Cache|Redis)\b|\bCache::|\bRedis::|\bPredis\\|\bnew\s+\\?Redis\s*\(|Symfony\\Component\\Cache\b",
    "sqs": r"\bAws\\Sqs\b|\bSqsClient\b",
    "ses": r"\bAws\\SesV?2?\b|\bSesV?2?Client\b",
    "s3": r"\bAws\\S3\b|\bS3Client\b|Storage::disk\(\s*['\"]s3['\"]",
    "sfn": r"\bAws\\Sfn\b|\bSfnClient\b",
    "kinesis": r"\bAws\\Kinesis\b|\bKinesisClient\b",
    "kafka": r"\bRdKafka\\|\bJunges\\Kafka\b|\bKafka::",
    "http": r"\bGuzzleHttp\\|\bHttp::|Symfony\\Contracts\\HttpClient|\bHttpClientInterface\b|\bcurl_init\s*\(",
    "llm": r"\bOpenAI::|\bOpenAI\\|\bAnthropic\\",
    "tracing": r"\bSentry\\|\bOpenTelemetry\\",
    "mongo": r"\bMongoDB\\",
}
EXT_RX = {k: re.compile(v) for k, v in EXT.items()}
DB_DEFAULT = re.compile(r"['\"]default['\"]\s*=>\s*(?:env\(\s*['\"]DB_CONNECTION['\"]\s*,\s*)?['\"](\w+)['\"]")
DB_NAMES = {"pgsql": "postgres", "postgres": "postgres", "mysql": "mysql", "mariadb": "mysql", "sqlsrv": "mssql", "sqlite": ""}


@dataclass
class State:
    types: TypeIndex = field(default_factory=TypeIndex)
    methods: dict[int, dict[str, int]] = field(default_factory=dict)   # file index -> {method: line}
    simple: dict[str, list[int]] = field(default_factory=dict)        # class name -> files declaring it
    models: set[str] = field(default_factory=set)                     # Eloquent model class names


@lru_cache(maxsize=4096)
def app_root(path: Path, stop: Path | None = None) -> Path:
    """The folder holding the nearest composer.json (else the file's own folder), never above stop."""
    for d in list(path.parents)[:8]:
        if stop is not None and d != stop and stop not in d.parents:
            break
        if (d / "composer.json").is_file():
            return d
    return path.parent


def _pkg(ns: str) -> str:
    return ns.strip("\\").replace("\\", ".")


def prepare(files: list[tuple[int, Path, str]]) -> State:
    st = State()
    bases: dict[str, str] = {}
    for i, path, text in files:
        bare = blank(text, True)
        ns = NAMESPACE.search(bare)
        types = list(dict.fromkeys(TYPE_DECL.findall(bare))) or [path.stem]
        st.types.add(i, _pkg(ns.group(1)) if ns else "", types)
        for t in types:
            st.simple.setdefault(t, []).append(i)
        st.methods[i] = {}
        for m in METHOD.finditer(bare):
            st.methods[i].setdefault(m.group(1), line_of(text, m.start(1)))
        for m in EXTENDS.finditer(bare):
            if m.group(2):
                bases[m.group(1)] = m.group(2).split("\\")[-1]
    st.models = {c for c, b in bases.items() if b in MODEL_BASES}
    grew = True
    while grew:
        more = {c for c, b in bases.items() if b in st.models} - st.models
        grew = bool(more)
        st.models |= more
    return st


def uses(bare: str) -> dict[str, str]:
    """alias -> fully qualified name, from a file's `use` statements."""
    out = {}
    for m in USE.finditer(bare):
        out[m.group(2) or m.group(1).split("\\")[-1]] = m.group(1)
    for m in USE_GROUP.finditer(bare):
        for part in m.group(2).split(","):
            g = re.match(r"\s*([\w\\]+)(?:\s+as\s+(\w+))?\s*$", part)
            if g:
                out[g.group(2) or g.group(1).split("\\")[-1]] = f"{m.group(1)}\\{g.group(1)}"
    return out


def _psr4(root: Path, fqn: str) -> list[Path]:
    try:
        spec = json.loads(read_contained(root.resolve(), "composer.json") or "{}")
    except ValueError:
        return []
    out = []
    for section in ("autoload", "autoload-dev"):
        for prefix, dirs in (spec.get(section, {}).get("psr-4", {}) or {}).items():
            if isinstance(prefix, str) and fqn.startswith(prefix):
                for d in dirs if isinstance(dirs, list) else [dirs]:
                    if isinstance(d, str):
                        out.append(root / d / (fqn[len(prefix):].replace("\\", "/") + ".php"))
    return out


def _class_file(fqn: str, st: State, ctx, root: Path) -> int | None:
    fqn = fqn.strip("\\")
    hit = st.types.fq.get(_pkg(fqn))
    if hit is not None:
        return hit
    for p in _psr4(root, fqn):
        hit = ctx.index.get(str(p.resolve()))
        if hit is not None:
            return hit
    return None


def _resolve_route(r: phproutes.Route, aliases: dict[str, str], st: State, ctx, root: Path) -> int | None:
    cls = r.cls.lstrip("\\")
    head = cls.split("\\")[0]
    if r.cls.startswith("\\"):
        cands = [cls]
    elif head in aliases:
        cands = [aliases[head] + cls[len(head):]]
    else:
        cands = ["\\".join(p for p in (DEFAULT_CONTROLLERS, r.ns, cls) if p)]
    for fqn in cands:
        hit = _class_file(fqn, st, ctx, root)
        if hit is not None:
            return hit
    same = st.simple.get(cls.split("\\")[-1], [])
    return same[0] if len(same) == 1 else None


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    found: list[phproutes.Route] = []
    laravel = path.parent.name == "routes" and "Route::" in text
    if laravel:
        found += phproutes.laravel(text, api=path.name == "api.php")
    elif re.search(r"Slim\\|AppFactory|\$router\s*->|Laravel\\Lumen", text):
        found += phproutes.slim(text)
    if "Route" in text and ("#[" in text or "@Route" in text):
        found += phproutes.symfony(text)
    st: State | None = ctx.state if ctx is not None else None
    aliases = uses(blank(text, True))
    out = []
    for r in found:
        name = re.sub(r"Controller$", "", r.cls.split("\\")[-1])
        item = {"m": r.verb, "p": r.path, "t": humanize(f"{r.action} {name}".strip()) if r.action else "", "d": "", "l": r.line}
        if r.cls and st is not None:
            idx = _resolve_route(r, aliases, st, ctx, app_root(path, Path(ctx.repo)))
            line = st.methods.get(idx, {}).get(r.action) if idx is not None else None
            if idx is not None and line:
                item.update({"f": idx, "l": line})
        out.append(item)
    return out


def imports(path: Path, text: str, ctx) -> list[int]:
    st: State | None = ctx.state
    own = ctx.index.get(str(path.resolve()))
    if st is None:
        return []
    bare = blank(text, True)
    root = app_root(path, Path(ctx.repo))
    out: list[int] = []
    for fqn in uses(bare).values():
        hit = _class_file(fqn, st, ctx, root)
        if hit is not None:
            out.append(hit)
    ns = NAMESPACE.search(bare)
    if own is not None and ns:
        out += st.types.visible(USE.sub("", bare), own, [_pkg(ns.group(1))])
    for m in INCLUDE.finditer(blank(text)):
        rel = m.group(2)
        base = path.parent.parent if m.group(1) and m.group(1).startswith("dirname") else path.parent
        for cand in ([base / rel.lstrip("/")] if m.group(1) else [path.parent / rel, root / rel]):
            hit = ctx.index.get(str(cand.resolve()))
            if hit is not None:
                out.append(hit)
                break
    if path.parent.name == "routes" or "Route" in text:
        out += [r["f"] for r in routes(path, text, ctx) if "f" in r]
    return sorted(set(out) - {own})


@lru_cache(maxsize=64)
def db_kind(root: str) -> str:
    """postgres / mysql / mssql from config/database.php's default or composer.json, '' for sqlite, else postgres."""
    base = Path(root).resolve()
    m = DB_DEFAULT.search(read_contained(base, "config/database.php") or "")
    if m and m.group(1) in DB_NAMES:
        return DB_NAMES[m.group(1)]
    composer = (read_contained(base, "composer.json") or "").lower()
    if re.search(r"pdo_pgsql|pgsql|postgres", composer):
        return "postgres"
    if re.search(r"pdo_mysql|mysql|mariadb", composer):
        return "mysql"
    return "postgres"


STATIC_CALL = re.compile(r"(?<![\w\\])([A-Za-z_]\w*)::(?!class\b)\w+\s*\(")


def _uses_models(bare: str, models: set[str]) -> bool:
    return bool(models) and any(m in models for m in STATIC_CALL.findall(bare))  # one pass per file, then a set lookup


CONFIG_ARRAY = re.compile(r"^\s*<\?php\s*(?:declare\s*\([^)]*\)\s*;\s*|use\s+[^;]+;\s*)*return\s*(?:\[|array\s*\()")


def externals(text: str, ctx=None) -> set[str]:
    if CONFIG_ARRAY.match(blank(text)):
        return set()  # a config file (config/queue.php lists every driver, used or not)
    found = shared_externals(text) | {k for k, rx in EXT_RX.items() if rx.search(text)}
    st: State | None = ctx.state if ctx is not None else None
    model_file = re.search(r"\bextends\s+\\?(?:[\w\\]*\\)?(Model|Authenticatable|Pivot)\b", text)
    if "sql" in found or model_file or (st is not None and _uses_models(blank(text, True), st.models)):
        found.discard("sql")
        db = db_kind(str(ctx.root)) if ctx is not None else "postgres"
        if db:
            found.add(db)
    return found


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    """The handler's call sequence (see langs/phpflow.py)."""
    from langs import phpflow
    return phpflow.flows(data, base, route_index)
