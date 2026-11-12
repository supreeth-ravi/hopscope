"""Rust handler call flows (the adapter langs/scriptflow.py runs): fns and impl blocks by brace matching; receivers
typed from parameters (axum State/Extension and actix web::Data unwrapped), `let` bindings and struct fields; external
calls: sqlx query!/query_as!/query (the table named in the SQL), diesel and sea-orm, redis, AWS SDK fluent calls (SQS,
SES, S3, Step Functions, Kinesis), rdkafka producers (FutureRecord::to("topic")) and reqwest. if/else and match are
conditional; `Err(_) =>` arms and `if let Err(_)` run on error."""
from __future__ import annotations

import re
from pathlib import Path

from langs import flowkinds as fk
from langs import scriptflow as sf
from langs.common import match_close, split_top
from langs.flowparse import receiver_chain
from langs.rust import crate_root, db_kind
from langs.rusttext import blank, chained, fn_spans, impl_spans, literals, snake_words

CALL = re.compile(r"\b(\w+)\s*(!)?\s*(?:\.\s*<[^()]*?>\s*)?\(")
STRUCT = re.compile(r"\bstruct\s+(\w+)\s*(?:<[^{;]*>)?\s*\{")
FIELD = re.compile(r"(?:pub(?:\([^)]*\))?\s+)?(\w+)\s*:\s*([^,}]+)")
USE = re.compile(r"\buse\s+([\w:]+?)(?:::\{([^}]*)\}|(?:\s+as\s+(\w+))?)\s*;")
LET_TYPED = re.compile(r"\blet\s+(?:mut\s+)?(\w+)\s*:\s*([^=;]+)=")
LET_MADE = re.compile(r"\blet\s+(?:mut\s+)?(\w+)\s*=\s*([\w:]+)::(?:new|builder|connect|from_env|from_url|default|open)\b")
WRAPPERS = re.compile(r"^(?:State|Extension|web::Data|Data|Arc|Rc|Box|Mutex|RwLock)\s*<(.*)>$")
KEYWORDS = {"if", "while", "for", "match", "loop", "return", "fn", "move", "async", "await", "impl", "as", "in", "let", "unsafe", "where", "use"}
SQLX = {"query", "query_as", "query_scalar", "query_unchecked", "query_as_unchecked", "query_scalar_unchecked", "query_with", "query_as_with"}
DIESEL_READ = {"filter", "find", "load", "first", "get_result", "get_results", "select", "order", "order_by", "limit", "count", "inner_join", "left_join"}
CACHE_VERBS = {"get": "GET", "mget": "GET", "hget": "GET", "set": "SET", "set_ex": "SET", "set_nx": "SET", "hset": "SET", "del": "DELETE",
               "incr": "INCR", "expire": "EXPIRE", "publish": "PUBLISH", "lpush": "PUSH", "rpush": "PUSH", "exists": "EXISTS"}
AWS_CALLS = {"send_message": ("SQS", "aws"), "send_message_batch": ("SQS", "aws"), "send_email": ("SES", "aws"), "put_object": ("S3", "aws"),
             "get_object": ("S3", "aws"), "delete_object": ("S3", "aws"), "copy_object": ("S3", "aws"),
             "start_execution": ("Step Functions", "workflow"), "start_sync_execution": ("Step Functions", "workflow"),
             "put_record": ("Kinesis", "stream"), "put_records": ("Kinesis", "stream")}
AWS_CRATE = {"SQS": "sqs", "SES": "ses", "S3": "s3", "Step Functions": "sfn", "Kinesis": "kinesis"}
HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head")


def _type(t: str, uses: dict[str, str]) -> str:
    t = re.sub(r"'\w+\s*", "", t).strip().lstrip("&").strip()
    t = re.sub(r"^(?:mut|dyn|impl)\s+", "", t).strip()
    for _ in range(4):
        m = WRAPPERS.match(t)
        if not m:
            break
        t = m.group(1).strip()
    head = t.split("::")[0].split("<")[0]
    return uses[head] + t[len(head):] if head in uses else t


def _simple(t: str) -> str:
    return re.sub(r"<.*", "", t).split("::")[-1].strip()


def _uses(code: str) -> dict[str, str]:
    out = {}
    for m in USE.finditer(code):
        if m.group(2) is not None:
            for item in m.group(2).split(","):
                parts = re.split(r"\s+as\s+", item.strip())
                if parts[0] and parts[0] != "self":
                    out[parts[-1].split("::")[-1]] = f"{m.group(1)}::{parts[0]}"
        else:
            out[m.group(3) or m.group(1).split("::")[-1]] = m.group(1)
    return out


def _params(text: str, uses: dict[str, str]) -> dict[str, str]:
    out = {}
    for part in split_top(re.sub(r"'\w+", "", text)):
        m = re.match(r"\s*(?:mut\s+)?(?:[\w:]+\s*\(\s*(?:mut\s+)?(\w+)\s*\)|(\w+))\s*:\s*(.+)$", part, re.S)
        if m:
            out[m.group(1) or m.group(2)] = _type(m.group(3), uses)
    return out


class Rust:
    def parse(self, text: str, path: str) -> sf.Unit:
        code, plain = blank(text), blank(text, True)
        u = sf.Unit(text, code, chained(plain))
        uses = _uses(code)
        u.extra.update(plain=plain, params={})
        classes: dict[str, sf.Klass] = {}
        for m in STRUCT.finditer(plain):
            end = match_close(plain, m.end() - 1)
            k = classes.setdefault(m.group(1), sf.Klass(m.group(1), path))
            k.fields.update({f: _type(t, uses) for f, t in FIELD.findall(code[m.end():end])})
        impls = impl_spans(plain)
        for name, pos, b0, b1 in fn_spans(plain):
            owner = min((s for s in impls if s[1] < pos < s[2]), key=lambda s: s[2] - s[1], default=None)
            fn = sf.Fn(name, path, text.count("\n", 0, pos) + 1, (b0, b1), owner[0] if owner else "")
            open_ = plain.find("(", pos)
            u.extra["params"][(fn.line, name)] = _params(code[open_ + 1:match_close(plain, open_, "(", ")")], uses)
            u.fns.append(fn)
            if owner:
                classes.setdefault(owner[0], sf.Klass(owner[0], path)).fns.setdefault(name, []).append(fn)
        u.classes = list(classes.values())
        return u

    def calls(self, u: sf.Unit, fn: sf.Fn) -> list[sf.Call]:
        out = []
        b0, b1 = fn.body
        for m in CALL.finditer(u.bare, b0 + 1, b1):
            name, macro, pos = m.group(1), bool(m.group(2)), m.start(1)
            if name in KEYWORDS or re.search(r"\bfn\s+$", u.bare[max(0, pos - 4):pos]) or (macro and name not in SQLX):
                continue
            chain, broken = receiver_chain(u.bare, pos)
            if broken or (not chain and name[:1].isupper()):
                continue
            close = match_close(u.bare, m.end() - 1, "(", ")")
            out.append(sf.Call(pos, chain, name + ("!" if macro else ""), u.code[m.end():close]))
        return out

    def regions(self, u: sf.Unit, fn: sf.Fn) -> list[sf.Region]:
        plain, code = u.extra["plain"], u.code
        b0, b1 = fn.body
        out: list[sf.Region] = []
        for m in re.finditer(r"\b(if|match)\b", plain[b0:b1]):
            start = b0 + m.end()
            k, depth = start, 0
            while k < b1 and not (plain[k] == "{" and depth == 0):
                depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(plain[k], 0)
                k += 1
            if k >= b1:
                continue
            end = match_close(plain, k) + 1
            cond = code[start:k].strip()
            if m.group(1) == "match":
                out.append((k, end, 1, ""))
                out += self._err_arms(plain, k, end)
                continue
            if re.match(r"let\s+Err\b", cond):
                out.append((k, end, 2, "it fails"))
                continue
            rhs = cond.split("=", 1)[1] if cond.startswith("let ") and "=" in cond else cond
            why = fk.phrase(snake_words(rhs))
            out.append((k, end, 1, why))
            nxt = re.match(r"\s*else\s*\{", plain[end:end + 40])
            if nxt:
                brace = end + nxt.end() - 1
                other = why[:-4] + " missing" if why.endswith(" is set") else "otherwise"
                out.append((brace, match_close(plain, brace) + 1, 1, other))
        return out

    @staticmethod
    def _err_arms(plain: str, b0: int, b1: int) -> list[sf.Region]:
        out: list[sf.Region] = []
        for a in re.finditer(r"\bErr\s*\([^)]*\)\s*(?:if\s+[^=]*)?=>\s*", plain[b0:b1]):
            s = b0 + a.end()
            if plain[s:s + 1] == "{":
                out.append((b0 + a.start(), match_close(plain, s) + 1, 2, "it fails"))
                continue
            k, depth = s, 0
            while k < b1 and not (plain[k] == "," and depth == 0):
                depth += {"(": 1, "[": 1, "{": 1, ")": -1, "]": -1, "}": -1}.get(plain[k], 0)
                if depth < 0:
                    break
                k += 1
            out.append((b0 + a.start(), k, 2, "it fails"))
        return out

    def local_types(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn) -> dict[str, str]:
        types = dict(u.extra["params"].get((fn.line, fn.name), {}))
        body = u.code[fn.body[0]:fn.body[1]]
        uses = _uses(u.code)
        types.update({v: _type(t, uses) for v, t in LET_TYPED.findall(body)})
        types.update({v: _type(t, uses) for v, t in LET_MADE.findall(body)})
        return types

    def type_of(self, svc: sf.Service, chain: list[str], types: dict[str, str], owner: str) -> str:
        if not chain:
            return ""
        t = owner if chain[0] == "self" else types.get(chain[0], "")
        for seg in chain[1:]:
            k = next((k for k in svc.classes.get(_simple(t), []) if seg in k.fields), None)
            t = k.fields[seg] if k else ""
        return t

    def external(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn, c: sf.Call, types: dict[str, str]) -> tuple[str, str] | None:
        name, chain, db = c.name, c.chain, svc.db
        base = name.rstrip("!")
        t = self.type_of(svc, chain, types, fn.owner)
        tokens = " ".join(chain)
        if (name.endswith("!") or chain[-1:] == ["sqlx"]) and base in SQLX:
            label = next((lab for lab in (sf.sql_text_label(s) for s in literals(c.args)) if lab), None)
            return "sql", f"{db}: {label or 'query'}"
        if chain[:1] == ["diesel"] and base in ("insert_into", "update", "delete"):
            table = re.search(r"(\w+)\s*::\s*table|(\w+)", c.args)
            verb = {"insert_into": "INSERT", "update": "UPDATE", "delete": "DELETE"}[base]
            return "sql", f"{db}: {verb} {(table.group(1) or table.group(2)) if table else ''}".rstrip()
        if chain[-1:] == ["table"] and base in DIESEL_READ:
            return "sql", f"{db}: {'SELECT by id' if base == 'find' else 'SELECT'} {chain[-2] if len(chain) > 1 else ''}".rstrip()
        if chain[-1:] == ["Entity"] and base.startswith(("find", "insert", "update", "delete")):
            return "sql", f"{db}: {fk.sql_verb(base) or 'query'} {chain[-2] if len(chain) > 1 else ''}".rstrip()
        if chain == ["redis"] and base == "cmd":
            lit = literals(c.args)
            return "cache", f"Cache: {lit[0].upper() if lit else 'command'}"
        if base in CACHE_VERBS and (re.search(r"redis|cache", tokens, re.I) or "redis" in t):
            return "cache", f"Cache: {CACHE_VERBS[base]}"
        if base in AWS_CALLS:
            svc_name, kind = AWS_CALLS[base]
            crate = "aws_sdk_" + AWS_CRATE[svc_name]
            if crate in t or crate in u.text or re.search(AWS_CRATE[svc_name], tokens, re.I):
                return kind, f"{svc_name}: {fk.api_name(base)}"
        if base in ("send", "send_result") and (re.search(r"producer", tokens, re.I) or "Producer" in t):
            topic = re.search(r"(?:Future|Base)Record\s*::\s*to\s*\(\s*\"([^\"]+)\"", c.args)
            return "kafka", f"Kafka: send to {topic.group(1)[:60]}" if topic else "Kafka: send"
        if base in HTTP_VERBS and (chain == ["reqwest"] or "reqwest" in t or (re.search(r"client|http", tokens, re.I) and "reqwest" in u.text)):
            lit = literals(c.args)
            return "http", f"HTTP {base.upper()} {lit[0][:60] if lit else '(other service)'}"
        return None

    def resolve(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn, c: sf.Call, types: dict[str, str]) -> sf.Fn | None:
        chain, name = c.chain, c.name
        if name.endswith("!"):
            return None
        if not chain:
            here = [f for f in u.fns if f.name == name and not f.owner]
            free = svc.free.get(name, [])
            return here[0] if here else (free[0] if len(free) == 1 else None)
        if chain[0] == "self" and len(chain) == 1:
            return svc.method_in(fn.owner, name)
        if chain[0] in types and len(chain) == 1:
            return svc.method_in(_simple(types[chain[0]]), name)
        if chain[-1][:1].isupper():
            return svc.method_in(chain[-1], name)
        if all(s[:1].islower() for s in chain) and chain[0] not in types:
            mod = "/".join(s for s in chain if s not in ("crate", "self", "super"))
            cands = [f for f in svc.free.get(name, []) if f.file.endswith((f"{mod}.rs", f"{mod}/mod.rs"))] or svc.free.get(name, [])
            return cands[0] if len(cands) == 1 else None
        return None

    def inline(self, u: sf.Unit, path: str, line: int) -> sf.Fn | None:
        return None

    def accessor(self, name: str) -> bool:
        return name.startswith(("get_", "is_", "has_", "set_")) or name in ("new", "default", "clone")


ADAPTER = Rust()


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    files = [f["p"] for f in data.get("files", [])]
    kind = db_kind(crate_root(base / files[data["routes"][route_index]["f"]], base))
    return sf.flows(ADAPTER, (".rs",), data, base, route_index, {"postgres": "Postgres", "mysql": "MySQL"}.get(kind, "SQLite"))
