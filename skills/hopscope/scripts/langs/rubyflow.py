"""Ruby handler call flows (the adapter langs/scriptflow.py runs): `def … end` methods found with the `end`-block
finder in rubytext.py, calls with or without parentheses, ActiveRecord (`Model.where`, `@model.save`,
`current_user.reviews.create` -> a sql step on the model), Redis / Rails.cache, AWS SDK calls (SQS, SES, S3, Step
Functions, Kinesis), Kafka producers and HTTP clients; branches of if/unless/case (and modifier if/unless) are
conditional, rescue clauses run on error."""
from __future__ import annotations

import re
from pathlib import Path

from langs import scriptflow as sf
from langs.common import match_close
from langs.ruby import gem_db
from langs.rubytext import blank, blocks, camel, is_modifier, phrase, rescue_text, singular

CLASS_HEAD = re.compile(r"(?:class|module)\s+([A-Z][\w:]*)(?:\s*<\s*([A-Z][\w:]*))?")
DEF_HEAD = re.compile(r"def\s+(self\.)?([\w?!=]+|\[\]=?|[+\-*/<=>!~%&|^]+)")
INCLUDE = re.compile(r"^\s*include\s+([A-Z][\w:]*)", re.M)
TOKEN = re.compile(r"(?<![\w@$:])((?:@@?|\$)?[A-Za-z_]\w*(?:::[A-Z]\w*)*[?!]?)")
TYPED = re.compile(r"(@?\w+)\s*=\s*([A-Z][\w:]*)\s*\.\s*(?:new|find\w*|where|create\w*|first|last|take)\b")
RUBY_WORDS = {"if", "unless", "while", "until", "case", "when", "then", "else", "elsif", "end", "do", "begin", "rescue", "ensure", "return",
              "def", "class", "module", "and", "or", "not", "yield", "self", "nil", "true", "false", "super", "in", "for", "break", "next",
              "redo", "retry", "lambda", "proc", "puts", "raise", "require", "require_relative", "attr_reader", "attr_accessor", "private"}
AR = {"all", "where", "find", "find_by", "find_by!", "first", "first!", "last", "take", "create", "create!", "save", "save!", "update",
      "update!", "update_attributes", "update_attributes!", "update_attribute", "update_all", "update_column", "update_columns", "destroy",
      "destroy!", "destroy_all", "delete", "delete_all", "count", "exists?", "pluck", "includes", "joins", "order", "limit", "offset",
      "find_or_create_by", "find_or_create_by!", "find_or_initialize_by", "first_or_create", "upsert", "upsert_all", "insert", "insert_all",
      "increment!", "decrement!", "toggle!", "touch", "reload", "sum", "average", "maximum", "minimum", "group", "find_each",
      "find_in_batches", "preload", "eager_load", "having", "distinct", "ids", "page", "lock", "not"}
AWS_CALLS = {"send_message": ("SQS", "aws"), "send_message_batch": ("SQS", "aws"), "send_email": ("SES", "aws"), "send_raw_email": ("SES", "aws"),
             "send_templated_email": ("SES", "aws"), "send_bulk_email": ("SES", "aws"), "put_object": ("S3", "aws"), "get_object": ("S3", "aws"),
             "delete_object": ("S3", "aws"), "upload_file": ("S3", "aws"), "copy_object": ("S3", "aws"), "presigned_url": ("S3", "aws"),
             "start_execution": ("Step Functions", "workflow"), "start_sync_execution": ("Step Functions", "workflow"),
             "put_record": ("Kinesis", "stream"), "put_records": ("Kinesis", "stream")}
CACHE_VERBS = {"get": "GET", "mget": "GET", "hget": "GET", "hgetall": "GET", "read": "GET", "fetch": "GET", "set": "SET", "setex": "SET",
               "mset": "SET", "hset": "SET", "write": "SET", "zadd": "SET", "sadd": "SET", "del": "DELETE", "delete": "DELETE", "incr": "INCR",
               "incrby": "INCR", "decr": "INCR", "expire": "EXPIRE", "publish": "PUBLISH", "lpush": "PUSH", "rpush": "PUSH", "exists": "EXISTS",
               "exists?": "EXISTS"}
KAFKA_CALLS = {"deliver_message", "produce", "produce_sync", "produce_async", "produce_many_sync", "produce_many_async"}
HTTP_CONSTS = {"Net::HTTP", "HTTParty", "Faraday", "RestClient", "Typhoeus", "Excon", "HTTP"}
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "request", "get_response", "post_form", "start"}


def _simple(name: str) -> str:
    return name.split("::")[-1]


def _innermost(blks: list, pos: int, kinds: tuple[str, ...]):
    inside = [b for b in blks if b.kw in kinds and b.pos < pos < b.end]
    return max(inside, key=lambda b: b.pos, default=None)


class Ruby:
    def parse(self, text: str, path: str) -> sf.Unit:
        u = sf.Unit(text, blank(text), blank(text, True))
        blks = blocks(u.bare)
        by_block = {}
        for b in blks:
            if b.kw in ("class", "module"):
                m = CLASS_HEAD.match(u.bare, b.pos)
                if m:
                    k = sf.Klass(_simple(m.group(1)), path, [_simple(m.group(2))] if m.group(2) else [])
                    k.bases += [_simple(i.group(1)) for i in INCLUDE.finditer(u.bare, b.head, b.end)]
                    if m.group(2) == "ActiveRecord::Base":
                        k.bases.append("ActiveRecord::Base")
                    by_block[b.pos] = k
                    u.classes.append(k)
        for b in blks:
            m = DEF_HEAD.match(u.bare, b.pos) if b.kw == "def" else None
            if not m:
                continue
            owner = _innermost(blks, b.pos, ("class", "module"))
            kl = by_block.get(owner.pos) if owner else None
            fn = sf.Fn(m.group(2), path, u.text.count("\n", 0, b.pos) + 1, (b.head, b.end), kl.name if kl else "", bool(m.group(1)))
            u.fns.append(fn)
            if kl is not None:
                kl.fns.setdefault(fn.name, []).append(fn)
        u.extra["blocks"] = blks
        return u

    def calls(self, u: sf.Unit, fn: sf.Fn) -> list[sf.Call]:
        out = []
        b0, b1 = fn.body
        for m in TOKEN.finditer(u.bare, b0, b1):
            name, pos, end = m.group(1), m.start(1), m.end(1)
            if name[0] in "@$" or name in RUBY_WORDS or re.match(r"\s*:(?!:)", u.bare[end:end + 2]) or re.match(r"\s*=(?![=~>])", u.bare[end:end + 3]):
                continue
            k = pos - 1
            while k >= b0 and u.bare[k] in " \t":
                k -= 1
            dotted = k >= b0 and u.bare[k] == "." and u.bare[k - 1:k] != "."
            if not dotted and (name[0].isupper() or u.bare[k:k + 1] == ":"):
                continue
            chain: list[str] | None = self._chain(u.bare, k, b0) if dotted else []
            if chain is None:
                continue
            nxt = end
            while nxt < b1 and u.bare[nxt] in " \t":
                nxt += 1
            if nxt < b1 and u.bare[nxt] == "(" and nxt == end:
                close = match_close(u.bare, nxt, "(", ")")
                args = u.code[nxt + 1:close]
            else:
                eol = u.bare.find("\n", end)
                args = u.code[end:eol if eol >= 0 else b1]
            out.append(sf.Call(pos, chain, name, args))
        return out

    @staticmethod
    def _chain(bare: str, dot: int, floor: int) -> list[str] | None:
        parts: list[str] = []
        k = dot
        while True:
            j = k - 1
            while j >= floor and bare[j] in " \t":
                j -= 1
            if j < floor or bare[j] in ")]}":
                return None
            e = j + 1
            while j >= floor and (bare[j].isalnum() or bare[j] in "_?!@$:"):
                j -= 1
            tok = bare[j + 1:e].lstrip(":")
            if not tok:
                return None
            parts.append(tok)
            if j >= floor and bare[j] == "." and bare[j - 1:j] != ".":
                k = j
                continue
            break
        parts.reverse()
        return parts[1:] if parts and parts[0] == "self" else parts

    def regions(self, u: sf.Unit, fn: sf.Fn) -> list[sf.Region]:
        b0, b1 = fn.body
        blks = [b for b in u.extra.get("blocks", []) if b0 <= b.pos < b1]
        out: list[sf.Region] = []
        for b in blks:
            if b.kw in ("if", "unless"):
                why = phrase(u.code[b.pos + len(b.kw):b.head], b.kw == "unless")
                out.append((b.head, b.end, 1, why))
                for m in re.finditer(r"(?<![\w.])(elsif|else)\b([^\n]*)", u.bare[b.head:b.end]):
                    p = b.head + m.start()
                    if _innermost(blks, p, ("if", "unless", "case", "while", "until", "do", "def", "begin", "for")) is not b:
                        continue
                    if m.group(1) == "elsif":
                        other = phrase(u.code[p + 5:b.head + m.end()]) or "otherwise"
                    else:
                        other = why[:-4] + " missing" if why.endswith((" is set", " are set")) else "otherwise"
                    out.append((p, b.end, 1, other))
            elif b.kw == "case":
                out.append((b.head, b.end, 1, ""))
        for m in re.finditer(r"(?<![\w.:])(if|unless)\b", u.bare[b0:b1]):
            p = b0 + m.start()
            if is_modifier(u.bare, p):
                start = max(u.bare.rfind("\n", 0, p), u.bare.rfind(";", 0, p)) + 1
                eol = u.bare.find("\n", p)
                out.append((start, p, 1, phrase(u.code[m.end() + b0:eol if eol >= 0 else b1], m.group(1) == "unless")))
        for m in re.finditer(r"(?<![\w.:])rescue\b", u.bare[b0:b1]):
            p = b0 + m.start()
            if is_modifier(u.bare, p):
                continue
            owner = _innermost(u.extra.get("blocks", []), p, ("def", "begin", "do"))
            end = owner.end if owner else b1
            stop = re.search(r"(?<![\w.:])(ensure|else)\b", u.bare[p:end])
            eol = u.bare.find("\n", p)
            out.append((p, p + stop.start() if stop else end, 2, rescue_text(u.code[p + 6:eol if eol >= 0 else end])))
        return out

    def local_types(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn) -> dict[str, str]:
        return {m.group(1): _simple(m.group(2)) for m in TYPED.finditer(u.code, fn.body[0], fn.body[1])}

    def models(self, svc: sf.Service) -> set[str]:
        if "models" not in svc.extra:
            svc.extra["models"] = {n for n in svc.classes if svc.bases_of(n) & {"ApplicationRecord", "ActiveRecord::Base"}} - {"ApplicationRecord"}
        return svc.extra["models"]

    def model_of(self, svc: sf.Service, chain: list[str], types: dict[str, str]) -> str | None:
        if not chain:
            return None
        tok = chain[-1].lstrip("@$").rstrip("?!")
        models = self.models(svc)
        for cand in (_simple(tok) if tok[:1].isupper() else "", types.get(chain[-1], ""), camel(singular(tok)), camel(singular(tok.split("_")[-1]))):
            if cand in models:
                return cand
        return None

    def external(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn, c: sf.Call, types: dict[str, str]) -> tuple[str, str] | None:
        name, chain = c.name, c.chain
        if chain and c.name in CACHE_VERBS and any(re.search(r"redis|cache", t, re.I) for t in chain):
            return "cache", f"Cache: {CACHE_VERBS[name]}"
        model = self.model_of(svc, chain, types)
        if model and (name in AR or re.match(r"find_(all_)?by_\w+!?$", name)):
            return "sql", f"{svc.db}: {_ar_verb(name)} {model}"
        if chain and chain[-1] == "connection" and name in ("execute", "exec_query", "select_all", "select_value", "select_rows"):
            label = sf.sql_text_label(c.args)
            return "sql", f"{svc.db}: {label or 'query'}"
        if chain and chain[0] in HTTP_CONSTS and name in HTTP_VERBS or (chain and chain[-1] in ("http", "conn", "connection", "client", "faraday")
                                                                         and name in HTTP_VERBS - {"start"} and re.search(r"Faraday|HTTParty|Net::HTTP", u.text)):
            target = re.search(r"['\"]([^'\"]+)['\"]", c.args)
            verb = name.upper() if name in ("get", "post", "put", "patch", "delete", "head") else ("POST" if name == "post_form" else "GET")
            return "http", f"HTTP {verb} {re.sub(r'#[{][^}]*[}]', '{…}', target.group(1))[:60] if target else '(other service)'}"
        if name in KAFKA_CALLS and (any(re.search(r"kafka|producer|karafka|waterdrop", t, re.I) for t in chain) or re.search(r"Kafka|Karafka|WaterDrop", u.text)):
            topic = re.search(r"topic:\s*['\"]([^'\"]+)['\"]", c.args)
            return "kafka", f"Kafka: send to {topic.group(1)[:60]}" if topic else "Kafka: send"
        if name in AWS_CALLS and self.resolve(svc, u, fn, c, types) is None:
            svc_name, kind = AWS_CALLS[name]
            return kind, f"{svc_name}: {''.join(p.capitalize() for p in name.split('_'))}"
        return None

    def resolve(self, svc: sf.Service, u: sf.Unit, fn: sf.Fn, c: sf.Call, types: dict[str, str]) -> sf.Fn | None:
        chain, name = c.chain, c.name
        if not chain:
            hit = svc.method_in(fn.owner, name) if fn.owner else None
            return hit or next((f for f in u.fns if f.name == name and not f.owner), None)
        if len(chain) == 1 and chain[0][:1].isupper():
            return svc.method_in(_simple(chain[0]), name)
        if len(chain) == 2 and chain[0][:1].isupper() and chain[1] == "new":
            return svc.method_in(_simple(chain[0]), name)
        if len(chain) == 1 and chain[0] in types:
            return svc.method_in(types[chain[0]], name)
        model = self.model_of(svc, chain, types)
        return svc.method_in(model, name) if model else None

    def inline(self, u: sf.Unit, path: str, line: int) -> sf.Fn | None:
        for b in u.extra.get("blocks", []):
            if b.kw == "do" and u.text.count("\n", 0, b.pos) + 1 == line:
                return sf.Fn("handler", path, line, (b.head, b.end))
        return None

    def accessor(self, name: str) -> bool:
        return name.endswith(("?", "="))


def _ar_verb(name: str) -> str:
    n = name.rstrip("!?")
    if n == "find":
        return "SELECT by id"
    if n in ("save", "upsert", "upsert_all") or n.startswith(("find_or_create", "first_or_create")):
        return "UPSERT"
    if n.startswith(("update", "increment", "decrement", "toggle", "touch")):
        return "UPDATE"
    if n.startswith(("create", "insert")):
        return "INSERT"
    if n.startswith(("destroy", "delete")):
        return "DELETE"
    return "SELECT"


ADAPTER = Ruby()


def flows(data: dict, base: Path, route_index: int) -> list[list] | None:
    files = [f["p"] for f in data.get("files", [])]
    sid = sf.service_of(files[data["routes"][route_index]["f"]], data.get("services") or ["."])
    db = gem_db(base / sid) if sid != "." else gem_db(base)
    return sf.flows(ADAPTER, (".rb",), data, base, route_index, "SQLite" if db == "" else "MySQL" if db == "mysql" else "Postgres")
