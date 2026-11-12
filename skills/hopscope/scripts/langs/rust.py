"""Rust: axum, actix-web and Rocket routes (langs/rustroutes.py), imports through `mod x;`, `use crate::a::b` and
module-qualified paths (src/a/b.rs or src/a/b/mod.rs), the external systems a file uses (sqlx/diesel/sea-orm,
redis, AWS SDK, rdkafka, reqwest), and handler call flows (langs/rustflow.py).

A route registered with a named handler (`get(routes::parcels::list)`, `web::get().to(h)`) points at that fn, so call
flows start there. Prefixes cross files: an actix `web::scope` / Rocket `mount` applies to the attribute-routed fns
it registers, and an axum `.nest("/p", module::router())` to every route in that module's file. The SQL database
comes from the code (PgPool, MySqlPool, diesel's Pg/Mysql) or Cargo.toml (postgres, mysql); sqlite draws none."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from langs import rustroutes
from langs.common import build_text, humanize, javadoc_before, join_path, shared_externals
from langs.rusttext import blank

NAME = "rust"
EXTENSIONS = (".rs",)
SERVICE_MARKERS = ("Cargo.toml",)
TEST_FILE = re.compile(r"(_test|_tests)\.rs$|^tests?\.rs$")
BUILD_DIRS = {"target": "Cargo.toml"}

MOD_DECL = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.M)
USE = re.compile(r"\buse\s+([\w:]+)(?:::\{([^}]*)\})?\s*(?:as\s+\w+\s*)?;")
QUALIFIED = re.compile(r"(?<![\w:])([a-z_]\w*)((?:::[a-z_]\w*)+)")
EXT = {
    "sql": r"\bsqlx::|\buse\s+sqlx\b|\bdiesel::|\buse\s+diesel\b|\bsea_orm\b|\btokio_postgres\b|\bmysql_async\b|\bpostgres::",
    "cache": r"\bredis::|\buse\s+redis\b|\bdeadpool_redis\b|\bbb8_redis\b|\bfred::",
    "sqs": r"\baws_sdk_sqs\b",
    "ses": r"\baws_sdk_sesv?2?\b",
    "s3": r"\baws_sdk_s3\b",
    "sfn": r"\baws_sdk_sfn\b",
    "kinesis": r"\baws_sdk_kinesis\b",
    "kafka": r"\brdkafka\b|\bkafka::",
    "http": r"\breqwest\b|\bhyper::Client\b|\bureq::|\bsurf::",
    "mongo": r"\bmongodb::",
    "llm": r"\basync_openai\b",
    "tracing": r"\bopentelemetry\b|\bsentry::",
}
EXT_RX = {k: re.compile(v) for k, v in EXT.items()}
DIRECT_DB = (("postgres", re.compile(r"\bPg(Pool|Connection|PoolOptions)\b|\bPostgres\b|diesel::pg\b|\bPgConnection\b|\btokio_postgres\b")),
             ("mysql", re.compile(r"\bMySql(Pool|Connection|PoolOptions)?\b|diesel::mysql\b|\bMysqlConnection\b|\bmysql_async\b")),
             ("", re.compile(r"\bSqlite(Pool|Connection)?\b|\brusqlite\b")))


@dataclass
class FnInfo:
    idx: int
    line: int
    doc: str
    module: str   # routes/parcels ("" for main.rs / lib.rs)


@dataclass
class State:
    fns: dict[tuple[str, str], list[FnInfo]] = field(default_factory=dict)      # (crate, fn name) -> where it is
    scoped: dict[tuple[str, str], list[str]] = field(default_factory=dict)      # (crate, fn name) -> scope/mount prefixes
    mounted: dict[str, list[tuple[str, str]]] = field(default_factory=dict)    # file -> [(file that nests it, prefix)]
    parsed: dict[str, rustroutes.FileRoutes] = field(default_factory=dict)


@lru_cache(maxsize=4096)
def crate_root(path: Path, stop: Path | None = None) -> Path:
    """The folder holding the nearest Cargo.toml (else the file's own folder), never above stop."""
    for d in list(path.parents)[:10]:
        if stop is not None and d != stop and stop not in d.parents:
            break
        if (d / "Cargo.toml").is_file():
            return d
    return path.parent


def module_dir(path: Path) -> Path:
    return path.parent if path.name in ("main.rs", "lib.rs", "mod.rs") else path.parent / path.stem


def module_of(path: Path) -> str:
    try:
        rel = path.relative_to(crate_root(path) / "src").with_suffix("")
    except ValueError:
        return path.stem
    parts = [p for p in rel.parts if p != "mod"]
    return "" if parts in (["main"], ["lib"]) else "/".join(parts)


def resolve_mod(path: Path, segs: list[str], known) -> str | None:
    """The file a module path names, seen from path: [crate|super|self, a, b, item] -> src/a/b.rs or src/a/b/mod.rs."""
    base = module_dir(path)
    segs = list(segs)
    if segs and segs[0] == "crate":
        base, segs = crate_root(path) / "src", segs[1:]
    while segs and segs[0] in ("super", "self"):
        base = base.parent if segs[0] == "super" else base
        segs = segs[1:]
    for k in range(len(segs), 0, -1):
        stem = base.joinpath(*segs[:k])
        for cand in (stem.with_suffix(".rs"), stem / "mod.rs"):
            if str(cand) in known:
                return str(cand)
    return None


def prepare(files: list[tuple[int, Path, str]]) -> State:
    st = State()
    known = {str(p): i for i, p, _ in files}
    for i, path, text in files:
        crate = str(crate_root(path))
        bare = blank(text, True)
        for m in re.finditer(r"\bfn\s+(\w+)", bare):
            line = text.count("\n", 0, m.start()) + 1
            head = text.rfind("\n", 0, m.start()) + 1
            st.fns.setdefault((crate, m.group(1)), []).append(FnInfo(i, line, javadoc_before(text, head), module_of(path)))
        fr = rustroutes.parse(text)
        st.parsed[str(path)] = fr
        for name, pres in fr.scoped.items():
            st.scoped.setdefault((crate, name), []).extend(pres)
        for nest in fr.nests:
            target = resolve_mod(path, nest.segs[:-1], known) if len(nest.segs) > 1 else None
            if target and target != str(path):
                st.mounted.setdefault(target, []).append((str(path), nest.prefix))
    return st


def _prefixes(st: State, path: str, seen: frozenset = frozenset()) -> list[str]:
    if path not in st.mounted or path in seen:
        return [""]
    return sorted({join_path(up, pre) for src, pre in st.mounted[path] for up in _prefixes(st, src, seen | {path})})


def _handler(st: State, crate: str, ref: str, own: int | None) -> FnInfo | None:
    segs = [s for s in ref.split("::") if s and s not in ("crate", "self", "super")]
    if not segs:
        return None
    cands = st.fns.get((crate, segs[-1]), [])
    if len(segs) > 1:
        mod = "/".join(segs[:-1])
        exact = [c for c in cands if c.module == mod or c.module.endswith("/" + mod)]
        cands = exact or cands
    same = [c for c in cands if c.idx == own]
    pick = same or cands
    return pick[0] if len(pick) == 1 else None


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    st: State | None = ctx.state if ctx is not None else None
    fr = st.parsed.get(str(path)) if st is not None else None
    fr = fr or rustroutes.parse(text)
    crate = str(crate_root(path))
    own = ctx.index.get(str(path.resolve())) if ctx is not None else None
    ups = _prefixes(st, str(path)) if st is not None else [""]
    out = []
    for r in fr.routes:
        info = _handler(st, crate, r.handler, own) if st is not None and r.handler else None
        name = r.handler.split("::")[-1]
        item = {"m": r.verb, "p": r.path, "t": humanize(name) if name else "", "d": info.doc if info else "", "l": r.line}
        if info is not None:
            item.update({"f": info.idx, "l": info.line})
        out += [{**item, "p": join_path(up, r.path)} for up in ups]
    for r in fr.attrs:
        pres = (st.scoped.get((crate, r.handler)) if st is not None else None) or [""]
        doc = javadoc_before(text, text.rfind("\n", 0, r.pos) + 1)
        for pre in sorted(set(pres)):
            for up in ups:
                out.append({"m": r.verb, "p": join_path(join_path(up, pre), r.path), "t": humanize(r.handler), "d": doc, "l": r.line})
    return out


def imports(path: Path, text: str, ctx) -> list[int]:
    own = ctx.index.get(str(path.resolve()))
    known = _known(ctx)
    code = blank(text, True)
    out: set[str] = set()
    mods = set()
    for m in MOD_DECL.finditer(code):
        mods.add(m.group(1))
        hit = resolve_mod(path, [m.group(1)], known)
        if hit:
            out.add(hit)
    for m in USE.finditer(code):
        segs = [s for s in m.group(1).split("::") if s]
        items = [re.split(r"\s+as\s+", i.strip())[0] for i in (m.group(2) or "").split(",") if i.strip()] or [""]
        for item in items:
            full = segs + [s for s in item.split("::") if s and s != "self"]
            if full and (full[0] in ("crate", "super", "self") or full[0] in mods):
                hit = resolve_mod(path, full, known)
                if hit:
                    out.add(hit)
    for m in QUALIFIED.finditer(code):
        if m.group(1) in mods:
            hit = resolve_mod(path, [m.group(1)] + m.group(2).split("::")[1:], known)
            if hit:
                out.add(hit)
    idx = [ctx.index.get(str(Path(p).resolve())) for p in out]
    return sorted({i for i in idx if i is not None} - {own})


def _known(ctx) -> set[str]:
    """Scanned file paths as the walker spelled them (resolve_mod compares unresolved paths)."""
    key = "rust-known"
    if key not in ctx.cache:
        ctx.cache[key] = set(ctx.state.parsed) if ctx.state is not None else set()
    return ctx.cache[key]


def db_kind(root: Path) -> str:
    """postgres / mysql from Cargo.toml's features, '' for sqlite only, postgres when it names none."""
    text = build_text(str(root), ("Cargo.toml",))
    if re.search(r"postgres|\bpg\b", text):
        return "postgres"
    if re.search(r"mysql|mariadb", text):
        return "mysql"
    return "" if "sqlite" in text else "postgres"


def externals(text: str, ctx=None) -> set[str]:
    found = shared_externals(text) | {k for k, rx in EXT_RX.items() if rx.search(text)}
    if "sql" in found:
        found.discard("sql")
        direct = next((k for k, rx in DIRECT_DB if rx.search(text)), None)
        db = direct if direct is not None else (db_kind(Path(ctx.root)) if ctx is not None else "postgres")
        if db:
            found.add(db)
    return found


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    """The handler's call sequence (see langs/rustflow.py)."""
    from langs import rustflow
    return rustflow.flows(data, base, route_index)
