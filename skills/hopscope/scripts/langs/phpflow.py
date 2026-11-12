"""PHP handler call flows (the adapter langs/scriptflow.py runs): classes, methods and their bodies by brace matching;
calls through $this->field->m(), injected and promoted constructor dependencies, Model::m() and $model->m(); external
calls: Eloquent (Model::where, $model->save, $user->articles()->create -> a sql step on the model), the DB facade
(DB::table('x')->insert -> INSERT x), Doctrine repositories and the EntityManager, PDO, Cache/Redis, AWS SDK clients
(SqsClient, SesClient, S3Client, SfnClient, KinesisClient), Kafka producers and HTTP clients (Http facade, Guzzle).
if/elseif/else, switch and match are conditional, catch blocks run on error (langs/flowparse.regions)."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from langs import flowkinds as fk
from langs import scriptflow as sf
from langs.common import match_close
from langs.flowparse import receiver_chain, regions
from langs.php import app_root, db_kind
from langs.phptext import blank, chained, first_literal, wordy
from langs.rubytext import camel, singular

CLASS = re.compile(r"\b(class|trait|interface|enum)\s+(\w+)([^{;]*)\{")
METHOD = re.compile(r"\b((?:(?:public|protected|private|static|final|abstract)\s+)*)function\s+&?\s*(\w+)\s*\(")
CLOSURE = re.compile(r"\b(?:static\s+)?function\s*\([^)]*\)\s*(?:use\s*\([^)]*\)\s*)?(?::\s*\??[\w\\]+\s*)?\{")
CALL = re.compile(r"\b(\w+)\s*\(")
PARAM = re.compile(r"((?:(?:private|protected|public|readonly)\s+)*)(?:\??([\w\\]+)\s+)?&?(?:\.\.\.)?\$(\w+)")
PROP = re.compile(r"(?:private|protected|public)\s+(?:readonly\s+)?\??([\w\\]+)\s+\$(\w+)\s*[;=]")
NEW = re.compile(r"\$(\w+)\s*=\s*new\s+\\?([\w\\]+)")
STATIC_MADE = re.compile(r"\$(\w+)\s*=\s*\\?([A-Z]\w*)::(?:create|find\w*|where\w*|first\w*|query|make|updateOrCreate|firstOrCreate)\b")
KEYWORDS = {"if", "elseif", "foreach", "for", "while", "switch", "match", "catch", "function", "fn", "array", "isset", "empty", "list", "return",
            "echo", "print", "unset", "include", "require", "include_once", "require_once", "new", "and", "or", "use", "compact", "die", "exit"}
MODEL_BASES = {"Model", "Authenticatable", "Pivot", "MorphPivot", "Eloquent"}
ELOQUENT = {"all", "get", "first", "firstOrFail", "find", "findOrFail", "findMany", "where", "whereIn", "whereHas", "create", "insert", "update",
            "delete", "destroy", "save", "firstOrCreate", "firstOrNew", "updateOrCreate", "upsert", "count", "exists", "paginate", "simplePaginate",
            "pluck", "increment", "decrement", "attach", "detach", "sync", "toggle", "forceDelete", "restore", "query", "with", "orderBy",
            "latest", "cursor", "chunk", "value", "max", "min", "sum", "avg", "push", "saveMany", "createMany", "refresh", "fresh", "load"}
CACHE_VERBS = {"get": "GET", "remember": "GET", "rememberForever": "GET", "pull": "GET", "has": "EXISTS", "exists": "EXISTS", "put": "SET",
               "set": "SET", "setex": "SET", "forever": "SET", "add": "SET", "hset": "SET", "hget": "GET", "forget": "DELETE", "del": "DELETE",
               "delete": "DELETE", "flush": "DELETE", "increment": "INCR", "incr": "INCR", "decrement": "INCR", "expire": "EXPIRE",
               "publish": "PUBLISH", "lpush": "PUSH", "rpush": "PUSH"}
HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head")
ACCESSOR = re.compile(r"^(get|set|is|has)[A-Z]|^__")


def _simple(t: str) -> str:
    return t.strip("\\?").split("\\")[-1]


def _params(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """(parameter -> type, promoted constructor property -> type)."""
    types, promoted = {}, {}
    for m in PARAM.finditer(text):
        if m.group(2):
            types[m.group(3)] = _simple(m.group(2))
            if m.group(1).strip():
                promoted[m.group(3)] = _simple(m.group(2))
    return types, promoted


class Php:
    def parse(self, text: str, path: str) -> sf.Unit:
        code, plain = blank(text), blank(text, True)
        u = sf.Unit(text, code, chained(plain))
        u.extra.update(plain=plain, params={})
        spans = []
        for m in CLASS.finditer(plain):
            brace = m.end() - 1
            end = match_close(plain, brace)
            header = m.group(3)
            bases = [_simple(b) for b in re.findall(r"\\?[\w\\]+", re.sub(r"^.*?\b(?:extends|implements)\b", "", header, flags=re.S))
                     if b not in ("extends", "implements")] if re.search(r"\b(extends|implements)\b", header) else []
            k = sf.Klass(m.group(2), path, bases)
            k.fields.update({n: _simple(t) for t, n in PROP.findall(code[brace:end])})
            u.classes.append(k)
            spans.append((brace, end, k))
        for m in METHOD.finditer(plain):
            open_ = m.end() - 1
            close = match_close(plain, open_, "(", ")")
            body = re.match(r"\s*(?::\s*\??[\w\\|]+\s*)?\{", plain[close + 1:close + 200])
            if not body:
                continue
            b0 = close + 1 + body.end() - 1
            owner = min((s for s in spans if s[0] < m.start() < s[1]), key=lambda s: s[1] - s[0], default=None)
            kl = owner[2] if owner else None
            fn = sf.Fn(m.group(2), path, text.count("\n", 0, m.start(2)) + 1, (b0, match_close(plain, b0) + 1), kl.name if kl else "",
                       "static" in m.group(1))
            params, promoted = _params(code[open_ + 1:close])
            u.extra["params"][(fn.line, fn.name)] = params
            u.fns.append(fn)
            if kl is not None:
                kl.fns.setdefault(fn.name, []).append(fn)
                kl.fields.update(promoted)
                if fn.name == "__construct":
                    for a in re.finditer(r"\$this->(\w+)\s*=\s*\$(\w+)\s*;", code[b0:fn.body[1]]):
                        if a.group(2) in params:
                            kl.fields.setdefault(a.group(1), params[a.group(2)])
        return u

    def _chain(self, bare: str, pos: int, depth: int = 0) -> list[str] | None:
        chain, broken = receiver_chain(bare, pos)
        if not broken:
            return chain
        k = pos - 1
        while k >= 0 and bare[k] in " \t\r\n!?.":
            k -= 1
        if k < 0 or bare[k] != ")" or depth > 6:
            return None
        d, j = 0, k
        while j >= 0:
            d += {")": 1, "(": -1}.get(bare[j], 0)
            if d == 0:
                break
            j -= 1
        name = re.search(r"(\w+)\s*$", bare[max(0, j - 80):j])
        if not name:
            return None
        before = self._chain(bare, j - len(bare[max(0, j - 80):j]) + name.start(1), depth + 1)
        return None if before is None else before + [name.group(1)]

    def calls(self, u: sf.Unit, fn: sf.Fn) -> list[sf.Call]:
        out = []
        plain = u.extra["plain"]
        b0, b1 = fn.body
        for m in CALL.finditer(u.bare, b0, b1):
            name, pos = m.group(1), m.start(1)
            if name in KEYWORDS or re.search(r"\bnew\s+[\w\\]*$|\bfunction\s+$", plain[max(0, pos - 80):pos]):
                continue
            chain = self._chain(u.bare, pos)
            if chain is None:
                continue
            if chain and chain[0] in ("self", "static", "parent"):
                chain = chain[1:]
            close = match_close(u.bare, m.end() - 1, "(", ")")
            out.append(sf.Call(pos, chain, name, u.code[m.end():close]))
        return out

    def regions(self, u: sf.Unit, fn: sf.Fn) -> list[sf.Region]:
        plain = re.sub(r"\belseif\b", "    if", u.extra["plain"])
        proxy = SimpleNamespace(bare=plain, code=wordy(u.code))
        out = regions(cast(Any, proxy), fn.body[0], fn.body[1], False)
        for m in re.finditer(r"\bmatch\s*\(", plain[fn.body[0]:fn.body[1]]):
            close = match_close(plain, fn.body[0] + m.end() - 1, "(", ")")
            brace = plain.find("{", close)
            if brace >= 0:
                out.append((brace, match_close(plain, brace) + 1, 1, ""))
        return out

    def local_types(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn) -> dict[str, str]:
        types: dict[str, str] = {}
        for cls in [fn.owner, *sorted(svc.bases_of(fn.owner))] if fn.owner else []:
            for k in svc.classes.get(cls, []):
                for n, t in k.fields.items():
                    types.setdefault(n, t)
        types.update(u.extra["params"].get((fn.line, fn.name), {}))
        body = u.code[fn.body[0]:fn.body[1]]
        types.update({v: _simple(t) for v, t in NEW.findall(body)})
        types.update(dict(STATIC_MADE.findall(body)))
        return types

    def models(self, svc: sf.Service) -> set[str]:
        if "models" not in svc.extra:
            svc.extra["models"] = {n for n in svc.classes if svc.bases_of(n) & MODEL_BASES}
        return svc.extra["models"]

    def model_of(self, svc: sf.Service, chain: list[str], types: dict[str, str]) -> str | None:
        models = self.models(svc)
        if not chain:
            return None
        if chain[0] in models:
            return chain[0]
        if len(chain) == 1 and types.get(chain[0]) in models:
            return types[chain[0]]
        rel = camel(singular(chain[-1]))
        return rel if rel in models and chain[-1] not in types else None

    def external(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn, c: sf.Call, types: dict[str, str]) -> tuple[str, str] | None:
        name, chain = c.name, c.chain
        root = chain[0] if chain else ""
        t = _simple(types.get(root, root if root[:1].isupper() else ""))
        db = svc.db
        if root == "DB":
            return self._db_facade(u, c, db)
        if root in ("Cache", "Redis") or (re.search(r"redis|cache", root, re.I) and name in CACHE_VERBS):
            return ("cache", f"Cache: {CACHE_VERBS[name]}") if name in CACHE_VERBS else None
        if root == "Http" and name in HTTP_VERBS or (t in ("Client", "GuzzleClient", "HttpClientInterface", "HttpClient")
                                                    and re.search(r"GuzzleHttp|HttpClient", u.text) and name in HTTP_VERBS + ("request",)):
            args = [a for a in re.findall(r"['\"]([^'\"]*)['\"]", c.args)]
            verb = (args.pop(0).upper() if args else "SEND") if name == "request" else name.upper()
            return "http", f"HTTP {verb} {args[0][:60] if args else '(other service)'}"
        aws = fk.aws_service(t) if t else None
        if aws:
            return fk.STEP_KIND.get(aws, "aws"), f"{aws}: {fk.api_name(name)}"
        if (root == "Kafka" and name.startswith("publish")) or (name == "produce" and (t in ("ProducerTopic", "Producer") or re.search(r"kafka|topic", root, re.I))):
            topic = first_literal(c.args) if root == "Kafka" else (re.search(r"newTopic\(\s*['\"]([^'\"]+)", u.code) or [None, None])[1]
            return "kafka", f"Kafka: send to {topic[:60]}" if topic else "Kafka: send"
        model = self.model_of(svc, chain, types)
        if model and name in ELOQUENT and svc.method_in(model, name) is None:
            return "sql", f"{db}: {_verb(name)} {model}"
        if t.endswith("Repository") and svc.method_in(t, name) is None and fk.sql_verb(name):
            return "sql", f"{db}: {fk.sql_verb(name)} {re.sub(r'Repository$', '', t)}"
        if t in ("EntityManagerInterface", "EntityManager", "ObjectManager") and name in ("persist", "remove", "find"):
            what = re.match(r"\s*\\?([\w\\]+)::class", c.args)
            return "sql", f"{db}: {_verb(name)} {_simple(what.group(1)) if what else ''}".rstrip()
        if t == "PDO" and name in ("query", "prepare", "exec"):
            return "sql", f"{db}: {sf.sql_text_label(first_literal(c.args) or '') or 'query'}"
        return None

    @staticmethod
    def _db_facade(u: sf.Unit, c: sf.Call, db: str) -> tuple[str, str]:
        if len(c.chain) > 1 or c.name in ("transaction", "beginTransaction", "commit", "rollBack", "raw", "connection"):
            return "skip", ""
        if c.name == "table":
            end = u.bare.find(";", c.pos)
            last = re.findall(r"\.\s*(\w+)\s*\(", u.bare[c.pos:end if end >= 0 else c.pos + 400])
            return "sql", f"{db}: {_verb(last[-1]) if last else 'query'} {first_literal(c.args) or ''}".rstrip()
        return "sql", f"{db}: {sf.sql_text_label(first_literal(c.args) or '') or _verb(c.name)}"

    def resolve(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn, c: sf.Call, types: dict[str, str]) -> sf.Fn | None:
        chain, name, models = c.chain, c.name, self.models(svc)
        if not chain:
            hit = svc.method_in(fn.owner, name) if fn.owner else None
            free = svc.free.get(name, [])
            target = hit or (free[0] if free else None)
        elif len(chain) == 1:
            cls = types.get(chain[0]) or (chain[0] if chain[0][:1].isupper() else "")
            target = svc.method_in(_simple(cls), name) if cls else None
        else:
            target = None
        return None if target is None or target.owner in models else target

    def inline(self, u: sf.Unit, path: str, line: int) -> sf.Fn | None:
        plain = u.extra["plain"]
        start = 0
        for _ in range(line - 1):
            start = plain.find("\n", start) + 1
        eol = plain.find("\n", start)
        m = CLOSURE.search(plain, start, eol if eol >= 0 else len(plain))
        if not m:
            return None
        return sf.Fn("handler", path, line, (m.end() - 1, match_close(plain, m.end() - 1) + 1))

    def accessor(self, name: str) -> bool:
        return bool(ACCESSOR.match(name))


def _verb(name: str) -> str:
    low = name.lower()
    if low in ("find", "findorfail", "findmany"):
        return "SELECT by id"
    if low in ("save", "updateorcreate", "firstorcreate", "upsert", "sync", "push", "savemany"):
        return "UPSERT"
    if low in ("persist", "create", "insert", "attach", "createmany"):
        return "INSERT"
    if low in ("update", "increment", "decrement", "toggle", "restore"):
        return "UPDATE"
    if low in ("delete", "destroy", "detach", "remove", "forcedelete"):
        return "DELETE"
    return "SELECT"


ADAPTER = Php()


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    files = [f["p"] for f in data.get("files", [])]
    path = base / files[data["routes"][route_index]["f"]]
    kind = db_kind(str(app_root(path, base)))
    return sf.flows(ADAPTER, (".php",), data, base, route_index, {"postgres": "Postgres", "mysql": "MySQL", "mssql": "SQL Server"}.get(kind, "SQLite"))
