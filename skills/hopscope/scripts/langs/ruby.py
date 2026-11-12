"""Ruby: Rails config/routes.rb (langs/rails.py), Sinatra and Grape route blocks (langs/rubydsl.py), imports through
require_relative and Rails autoloading (a constant names the file that defines it), the external systems a file uses
(ActiveRecord, Redis, AWS SDK, Kafka, HTTP clients), and handler call flows (langs/rubyflow.py).

A Rails route points at its controller action (app/controllers/<namespace>/<name>_controller.rb, the line of
`def <action>`) when that file and method exist, so call flows start there; otherwise at its line in routes.rb.
ActiveRecord's database comes from the Gemfile: pg -> postgres, mysql2/trilogy -> mysql; sqlite3 alone is a file
inside the app, so no database external is drawn (flows still label its queries SQLite)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from langs import rails, rubydsl
from langs.common import humanize, line_of, read_contained, shared_externals
from langs.rubytext import blank, camel

NAME = "ruby"
EXTENSIONS = (".rb",)
SERVICE_MARKERS = ("Gemfile",)
TEST_FILE = re.compile(r"_(spec|test)\.rb$|^(spec|test)_helper\.rb$")
BUILD_DIRS: dict[str, str] = {}

DEF_LINE = re.compile(r"^[ \t]*def\s+(?:self\.)?([\w?!=]+)", re.M)
CLASS_DECL = re.compile(r"^[ \t]*(?:class|module)\s+([A-Z]\w*(?:::[A-Z]\w*)*)(?:\s*<\s*([A-Z][\w:]*))?", re.M)
REQUIRE_REL = re.compile(r"^[ \t]*require_relative\s*\(?\s*['\"]([^'\"]+)['\"]", re.M)
REQUIRE = re.compile(r"^[ \t]*require\s*\(?\s*['\"]([^'\"]+)['\"]", re.M)
CONST = re.compile(r"(?<![\w:@$.])([A-Z]\w*(?:::[A-Z]\w*)*)")
AR_BASES = {"ApplicationRecord", "ActiveRecord::Base", "Base"}
GEM_DB = (("postgres", re.compile(r"^\s*gem\s+['\"](pg|activerecord-postgis-adapter)['\"]", re.M)),
          ("mysql", re.compile(r"^\s*gem\s+['\"](mysql2|trilogy)['\"]", re.M)))

EXT = {
    "cache": r"\$redis\b|\bREDIS\b|\bRedis\.(new|current)\b|\bRedis::|require\s+['\"]redis['\"]|\bRails\.cache\b|\bKredis\b",
    "sqs": r"\bAws::SQS\b|aws-sdk-sqs|\bShoryuken\b",
    "ses": r"\bAws::SESV?2?\b|aws-sdk-sesv?2?",
    "s3": r"\bAws::S3\b|aws-sdk-s3|\bActiveStorage\b",
    "sfn": r"\bAws::States\b|aws-sdk-states",
    "kinesis": r"\bAws::Kinesis\b|aws-sdk-kinesis",
    "kafka": r"\bKafka\.new\b|require\s+['\"](kafka|rdkafka|waterdrop|karafka)['\"]|\bKarafka\b|\bWaterDrop\b|\bRdkafka\b",
    "http": r"\bNet::HTTP\b|\bHTTParty\b|\bFaraday\b|\bRestClient\b|\bTyphoeus\b|\bExcon\b",
    "llm": r"\bOpenAI::Client\b|\bAnthropic::Client\b|ruby-openai",
    "tracing": r"\bSentry\b|\bOpenTelemetry\b|\bDatadog\b|\bNewRelic\b",
    "mongo": r"\bMongoid\b|\bMongo::Client\b",
}
EXT_RX = {k: re.compile(v) for k, v in EXT.items()}


@dataclass
class State:
    consts: dict[str, dict[str, int]] = field(default_factory=dict)   # app root -> {Constant: file index}
    models: dict[str, set[str]] = field(default_factory=dict)         # app root -> ActiveRecord model names
    defs: dict[int, dict[str, int]] = field(default_factory=dict)     # file index -> {method: line}


@lru_cache(maxsize=4096)
def app_root(path: Path, stop: Path | None = None) -> Path:
    """The folder holding the nearest Gemfile (else the file's own folder), never above stop."""
    for d in list(path.parents)[:8]:
        if stop is not None and d != stop and stop not in d.parents:
            break
        if (d / "Gemfile").is_file():
            return d
    return path.parent


def _const_of(path: Path, root: Path) -> str:
    """The constant Rails' autoloader expects a file to define: app/services/billing/charge.rb -> Billing::Charge."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if len(parts) >= 3 and parts[0] == "app":
        parts = parts[2:]
        if parts and parts[0] == "concerns":
            parts = parts[1:]
    elif parts and parts[0] == "lib":
        parts = parts[1:]
    else:
        parts = parts[-1:]
    return "::".join(camel(p) for p in parts)


def prepare(files: list[tuple[int, Path, str]]) -> State:
    st = State()
    bases: dict[str, dict[str, str]] = {}
    for i, path, text in files:
        root = str(app_root(path))
        consts = st.consts.setdefault(root, {})
        try:
            full = _const_of(path, Path(root))
        except ValueError:
            full = camel(path.stem)
        names = [full, full.split("::")[-1]]
        code = blank(text)
        for m in CLASS_DECL.finditer(code):
            names += [m.group(1), m.group(1).split("::")[-1]]
            if m.group(2):
                bases.setdefault(root, {})[m.group(1).split("::")[-1]] = m.group(2)
        for n in names:
            consts.setdefault(n, i)
        st.defs[i] = {}
        for m in DEF_LINE.finditer(code):
            st.defs[i].setdefault(m.group(1), line_of(text, m.start(1)))
    for root, bs in bases.items():
        models = {c for c, b in bs.items() if b in AR_BASES}
        grew = True
        while grew:
            more = {c for c, b in bs.items() if b.split("::")[-1] in models} - models
            grew = bool(more)
            models |= more
        st.models[root] = models - {"ApplicationRecord"}
    return st


def _is_routes_file(path: Path) -> bool:
    parts = path.parts
    return len(parts) >= 2 and parts[-2] == "config" and parts[-1] == "routes.rb" or (len(parts) >= 3 and parts[-3:-1] == ("config", "routes"))


def _rails_routes(path: Path, text: str, ctx) -> list[dict]:
    st: State | None = ctx.state if ctx is not None else None
    root = app_root(path)
    out = []
    for r in rails.parse(text):
        item = {"m": r.verb, "p": r.path, "t": humanize(f"{r.action} {r.controller.split('/')[-1]}".strip()), "d": "", "l": r.line}
        if r.controller and ctx is not None and st is not None:
            idx = _resolve_file([root / "app" / "controllers" / f"{r.controller}_controller.rb"], ctx)
            line = st.defs.get(idx, {}).get(r.action) if idx is not None else None
            if idx is not None and line:
                item.update({"f": idx, "l": line})
        out.append(item)
    return out


def routes(path: Path, text: str, ctx=None) -> list[dict]:
    if _is_routes_file(path) and ".draw" in text or (path.parent.name == "routes" and path.parent.parent.name == "config"):
        return _rails_routes(path, text, ctx)
    if rubydsl.is_dsl(text):
        return rubydsl.routes(text)
    return []


def _resolve_file(cands: list[Path], ctx) -> int | None:
    for c in cands:
        hit = ctx.index.get(str(c.resolve()))
        if hit is not None:
            return hit
    return None


def imports(path: Path, text: str, ctx) -> list[int]:
    own = ctx.index.get(str(path.resolve()))
    st: State | None = ctx.state
    code = blank(text)
    out: list[int] = []
    root = app_root(path)
    for m in REQUIRE_REL.finditer(code):
        rel = m.group(1) if m.group(1).endswith(".rb") else m.group(1) + ".rb"
        hit = _resolve_file([path.parent / rel], ctx)
        if hit is not None:
            out.append(hit)
    for m in REQUIRE.finditer(code):
        rel = m.group(1) if m.group(1).endswith(".rb") else m.group(1) + ".rb"
        hit = _resolve_file([root / "lib" / rel, root / rel], ctx)
        if hit is not None:
            out.append(hit)
    if st is not None:
        consts = st.consts.get(str(root), {})
        for m in CONST.finditer(blank(text, True)):
            name = m.group(1)
            hit = consts.get(name, consts.get(name.split("::")[-1]))
            if hit is not None:
                out.append(hit)
        if _is_routes_file(path):
            out += [r["f"] for r in _rails_routes(path, text, ctx) if "f" in r]
    return sorted(set(out) - {own})


def gem_db(root: Path) -> str | None:
    """postgres or mysql from the app's Gemfile, '' for sqlite3 only, None when it names no database gem."""
    text = read_contained(root.resolve(), "Gemfile") or ""
    hit = next((k for k, rx in GEM_DB if rx.search(text)), None)
    if hit:
        return hit
    return "" if re.search(r"^\s*gem\s+['\"]sqlite3['\"]", text, re.M) else None


AR_CALL = re.compile(r"(?<![\w:])([A-Z]\w*(?:::[A-Z]\w*)*)\s*\.\s*(?:where|find\w*|create!?|all|first|last|count|joins|includes|order|update\w*|destroy\w*|delete\w*|exists\?|pluck|new)\b")


def _uses_ar(text: str, models: set[str]) -> bool:
    if re.search(r"<\s*(ApplicationRecord|ActiveRecord::Base)\b|\bActiveRecord::|\bActiveRecord\b", text):
        return True
    return bool(models) and any(m in models for m in AR_CALL.findall(blank(text, True)))  # one pass per file, then a set lookup


def externals(text: str, ctx=None) -> set[str]:
    found = shared_externals(text) | {k for k, rx in EXT_RX.items() if rx.search(text)}
    st: State | None = ctx.state if ctx is not None else None
    if ctx is not None:
        root = Path(ctx.root)
        models = st.models.get(str(app_root(root / "Gemfile")), set()) if st is not None else set()
        if _uses_ar(text, models):
            db = gem_db(root)
            if db:
                found.add(db)
            elif db is None:
                found.add("postgres")
    return found


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    """The handler's call sequence (see langs/rubyflow.py)."""
    from langs import rubyflow
    return rubyflow.flows(data, base, route_index)
