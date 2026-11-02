"""Static call-sequence extraction for each Python API handler.

For every route in the floorplan, walk the handler body in source order, follow calls into
functions and methods defined in the same service (up to MAX_DEPTH), and record the calls that
leave the process: AWS SDK calls, SQL, cache, queues, HTTP to other services, AI model calls.
Writes flows.json: {route_index: [[depth, kind, label, file, line, cond], ...]} where cond is
0 = always, 1 = only under a condition, 2 = only on error.

    python call_flow.py data/code.json data/flows.json
"""
from __future__ import annotations

import ast
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

DATA = json.loads(Path(sys.argv[1]).read_text())
OUT = Path(sys.argv[2])
ROOT = Path(DATA["base"])
FILES = [f["p"] for f in DATA["files"]]
SERVICES = sorted(DATA.get("services", []), key=len, reverse=True)
MAX_DEPTH, MAX_STEPS = 4, 28

BOTO_SERVICE = {"cognito-idp": "Cognito", "sqs": "SQS", "ses": "SES", "sesv2": "SES", "s3": "S3", "secretsmanager": "Secrets Manager",
                "lambda": "Lambda", "ecs": "ECS", "ecr": "ECR", "sns": "SNS", "events": "EventBridge", "dynamodb": "DynamoDB",
                "cloudwatch": "CloudWatch", "logs": "CloudWatch Logs", "apigateway": "API Gateway", "bedrock-runtime": "Bedrock"}
SQL_METHODS = {"exec", "execute", "query", "add", "add_all", "commit", "scalars", "scalar", "get", "merge", "delete", "refresh", "flush", "fetch", "fetchrow", "fetchval", "first", "all"}
DB_NAMES = re.compile(r"^(db|session|sess|conn|connection|async_session|self\.db|self\.session|self\._session|self\.repo\.session|tx)$")
CACHE_NAMES = re.compile(r"redis|cache|valkey", re.I)
HTTP_NAMES = re.compile(r"^(httpx|requests|aiohttp|client|http_client|self\.client|self\._client|self\.http|session)$")
LLM_FUNCS = {"completion", "acompletion", "embedding", "aembedding"}


def service_root(path: str) -> str:
    """Deepest service folder that contains the file (falls back to the first two path segments)."""
    for svc in SERVICES:
        if svc != "." and path.startswith(svc + "/"):
            return svc
    return "/".join(path.split("/")[:2])


@lru_cache(maxsize=None)
def parse(path):
    try:
        return ast.parse((ROOT / path).read_text(errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return None


def resolve_module(mod, from_path, level=0):
    """Map a Python module name to a file in the same service."""
    root = service_root(from_path)
    if level:
        base = Path(from_path).parent
        for _ in range(level - 1):
            base = base.parent
        rel = str(base / mod.replace(".", "/")) if mod else str(base)
        cands = [rel + ".py", rel + "/__init__.py"]
    else:
        tail = mod.replace(".", "/")
        cands = [p for p in FILES if p.startswith(root + "/") and (p.endswith("/" + tail + ".py") or p.endswith("/" + tail + "/__init__.py"))]
        cands.sort(key=len)
    for c in cands:
        if c in FILES_SET:
            return c
    return None


FILES_SET = set(FILES)
FILE_IDX = {p: i for i, p in enumerate(FILES)}


@lru_cache(maxsize=None)
def module_index(path):
    """Imports, top-level functions, classes (with methods and boto3 client attrs) of a file."""
    tree = parse(path)
    idx = {"imports": {}, "funcs": {}, "classes": {}, "boto_globals": {}}
    if not tree:
        return idx
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            target = resolve_module(node.module or "", path, node.level)
            for a in node.names:
                sub = resolve_module(f"{node.module}.{a.name}" if node.module else a.name, path, node.level) if not target else None
                idx["imports"][a.asname or a.name] = (target or sub, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                idx["imports"][a.asname or a.name] = (resolve_module(a.name, path), None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            idx["funcs"][node.name] = node
        elif isinstance(node, ast.ClassDef):
            methods = {n.name: n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            attrs = {}
            for m in methods.values():
                for sub in ast.walk(m):
                    if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call):
                        svc = boto_service(sub.value)
                        for t in sub.targets:
                            if isinstance(t, ast.Attribute) and svc:
                                attrs["self." + t.attr] = svc
            idx["classes"][node.name] = {"methods": methods, "boto": attrs}
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            svc = boto_service(node.value)
            for t in node.targets:
                if isinstance(t, ast.Name) and svc:
                    idx["boto_globals"][t.id] = svc
    return idx


def boto_service(call):
    name = dotted(call.func)
    if name and re.search(r"(boto3|aioboto3|session)\.(client|resource)$|^(client|resource)$", name) and call.args and isinstance(call.args[0], ast.Constant):
        return BOTO_SERVICE.get(call.args[0].value, call.args[0].value)
    return None


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return None


def find_def(path, line, name):
    tree = parse(path)
    if not tree:
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.name == name or abs(node.lineno - line) <= 3):
            if best is None or abs(node.lineno - line) < abs(best.lineno - line):
                best = node
    return best


def model_name(call):
    for sub in ast.walk(call):
        if isinstance(sub, ast.Call) and (dotted(sub.func) or "").split(".")[-1] in ("select", "update", "insert", "delete") and sub.args:
            inner = dotted(sub.args[0])
            if inner:
                verb = (dotted(sub.func) or "").split(".")[-1].upper()
                return f"{verb} {inner.split('.')[0]}"
    for a in call.args:
        n = dotted(a.func) if isinstance(a, ast.Call) else dotted(a)
        if isinstance(a, ast.Call) and n in ("select", "sqlmodel.select", "sa.select", "update", "delete", "insert"):
            inner = [dotted(x) for x in a.args if dotted(x)]
            return f"{n.split('.')[-1].upper()} {inner[0].split('.')[0]}" if inner else n.upper()
        if n and n[:1].isupper():
            return n.split(".")[0]
    return ""


@lru_cache(maxsize=None)
def source_lines(path):
    try:
        return (ROOT / path).read_text(errors="ignore").splitlines()
    except OSError:
        return []


def words(node):
    """A variable or attribute as plain words: evt.raw_mime_s3_key -> 'raw mime s3 key'."""
    if isinstance(node, ast.Call):  # x.strip(), len(x), bool(x) -> x
        inner = node.args[0] if node.args and isinstance(node.func, ast.Name) else getattr(node.func, "value", None)
        return words(inner) if inner is not None else ""
    if isinstance(node, ast.Attribute):
        return re.sub(r"[_]+", " ", node.attr).strip()
    if isinstance(node, ast.Name):
        return re.sub(r"[_]+", " ", node.id).strip()
    if isinstance(node, ast.Subscript):
        return words(node.value)
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return ""


def phrase(test):
    """A condition expression in plain words, or '' when it can't be said simply."""
    if isinstance(test, ast.BoolOp):
        parts = [phrase(v) for v in test.values]
        if not all(parts):
            return ""
        if isinstance(test.op, ast.And) and all(p.endswith(" is set") for p in parts):
            items = [p[:-7] for p in parts]
            uniq = list(dict.fromkeys(items))
            return (", ".join(uniq[:-1]) + " and " + uniq[-1] + " are set") if len(uniq) > 1 else uniq[0] + " is set"
        return (" and " if isinstance(test.op, ast.And) else " or ").join(dict.fromkeys(parts))
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        w = words(test.operand)
        return f"there is no {w}" if w else ""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, op, right = words(test.left), test.ops[0], test.comparators[0]
        if not left:
            return ""
        if isinstance(right, ast.Constant) and right.value is None:
            return f"{left} is {'missing' if isinstance(op, ast.Is) else 'set'}"
        r = words(right)
        verb = {ast.Eq: "is", ast.NotEq: "is not", ast.Gt: "is above", ast.Lt: "is below", ast.GtE: "is at least", ast.LtE: "is at most",
                ast.In: "is one of", ast.NotIn: "is not one of", ast.Is: "is", ast.IsNot: "is not"}.get(type(op))
        return f"{left} {verb} {r}" if verb and r else ""
    if isinstance(test, ast.Name):  # a boolean flag: `if developer:` -> "it is a developer"
        n = test.id.lstrip("_")
        if n.startswith(("is_", "has_", "can_", "should_")):
            verb, rest = n.split("_", 1)
            return f"it {dict(is_='is', has_='has', can_='can', should_='should').get(verb + '_', verb)} {rest.replace('_', ' ')}"
        if "_" not in n and not n.endswith(("s", "id", "data", "config", "cfg", "resp", "result")):
            return f"it is a {n}"
    w = words(test)
    return f"{w} is set" if w else ""


def condition_text(path, node, in_else):
    """Why a branch runs: the developer's comment above the `if` when there is one, else the condition in words."""
    lines = source_lines(path)
    above = lines[node.lineno - 2].strip() if node.lineno >= 2 and len(lines) >= node.lineno - 1 else ""
    if above.startswith("#") and not in_else:
        c = above.lstrip("#").strip().split(",")[0].rstrip(".")
        if re.search(r"\b(?:if|when|unless)\b\s+\S", c, re.I):
            return c  # keep the whole sentence; the narrator splits "do X if Y"
    p = phrase(node.test)
    if not p or len(p) > 80:
        return "otherwise" if in_else else ""
    if in_else:
        if p.endswith(" are set"):
            return p[:-4] + " missing"
        if p.endswith(" is set"):
            return p[:-4] + " missing"
        return "otherwise"
    return p


def handler_text(h):
    if h.type is None:
        return "it fails"
    names = [n.id if isinstance(n, ast.Name) else getattr(n, "attr", "") for n in (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])]
    pretty = [re.sub(r"(Error|Exception)$", "", n) for n in names if n]
    return "it fails" if not pretty or pretty == [""] or "Exception" in names else f"a {' or '.join(re.sub(r'([a-z])([A-Z])', r'\1 \2', p).lower() for p in pretty)} error happens"


def walk_calls(fn, path=None):
    """Calls in a function body in source order: (call, kind, why) with kind 0 = always, 1 = conditional,
    2 = on error, and why = the condition in plain words (or '')."""
    out = []
    def visit(node, cond, why):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and child is not fn:
                continue
            c, w = cond, why
            if isinstance(child, ast.ExceptHandler):
                c, w = 2, handler_text(child)
            elif isinstance(node, ast.If) and child is not node.test:
                c = max(cond, 1)
                text = condition_text(path, node, child in node.orelse) if path else ""
                w = text or why
            elif isinstance(node, ast.IfExp) and child is not node.test and cond == 0:
                c = 1
            if isinstance(child, ast.Call):
                out.append((child, c, w))
            visit(child, c, w)
    visit(fn, 0, "")
    return sorted(out, key=lambda x: (x[0].lineno, x[0].col_offset))


def param_types(fn):
    out = {}
    args = fn.args.args + fn.args.kwonlyargs
    for a in args:
        if a.annotation is not None:
            t = dotted(a.annotation.slice if isinstance(a.annotation, ast.Subscript) else a.annotation)
            if t:
                out[a.arg] = t.split(".")[-1]
    return out


def trace(path, fn, depth, steps, seen, cls=None, local_types=None, inherited=0, inherited_why=""):
    if depth > MAX_DEPTH or len(steps) >= MAX_STEPS:
        return
    idx = module_index(path)
    types = dict(param_types(fn))
    types.update(local_types or {})
    for node in ast.walk(fn):  # x = SomeClass(...) assignments
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.targets[0], ast.Name):
            cn = dotted(node.value.func)
            if cn and cn.split(".")[-1][:1].isupper():
                types[node.targets[0].id] = cn.split(".")[-1]
    via = re.sub(r"_+", " ", fn.name).strip() if depth else ""
    for call, cond, why in walk_calls(fn, path):
        cond = max(cond, inherited)
        why = why or inherited_why
        if len(steps) >= MAX_STEPS:
            return
        name = dotted(call.func) or ""
        parts = name.split(".")
        head, meth = ".".join(parts[:-1]), parts[-1]
        line = call.lineno
        # AWS SDK via self.<attr> or module-level client
        boto = (cls or {}).get("boto", {}).get(head) or idx["boto_globals"].get(head)
        if boto:
            steps.append([depth, "aws", f"{boto}: {to_api(meth)}", path, line, cond, why, via])
            continue
        if meth in ("send_message", "send_message_batch") and (not boto):
            steps.append([depth, "queue", "SQS: SendMessage", path, line, cond, why, via])
            continue
        if meth in ("send_email", "send_raw_email"):
            steps.append([depth, "aws", "SES: SendEmail", path, line, cond, why, via])
            continue
        if meth in ("upload_fileobj", "put_object", "generate_presigned_url", "get_object"):
            steps.append([depth, "aws", f"S3: {to_api(meth)}", path, line, cond, why, via])
            continue
        if meth in SQL_METHODS and (DB_NAMES.match(head) or head.endswith(".session") or head.endswith(".db")):
            if meth in ("commit", "refresh", "flush", "all", "first") and steps and steps[-1][1] == "sql":
                steps[-1][2] = steps[-1][2] + (" + commit" if meth == "commit" and "commit" not in steps[-1][2] else "")
                continue
            what = model_name(call)
            steps.append([depth, "sql", f"Postgres: {sql_verb(meth, what)}", path, line, cond, why, via])
            continue
        if CACHE_NAMES.search(head) and meth in ("get", "set", "setex", "hget", "hset", "delete", "incr", "expire", "publish", "lpush", "rpush", "xadd", "exists"):
            steps.append([depth, "cache", f"Cache: {meth.upper()}", path, line, cond, why, via])
            continue
        if meth in LLM_FUNCS or name.endswith("chat.completions.create"):
            steps.append([depth, "llm", "AI model gateway: completion", path, line, cond, why, via])
            continue
        if meth in ("get", "post", "put", "patch", "delete", "request", "stream") and HTTP_NAMES.match(head):
            steps.append([depth, "http", f"HTTP {meth.upper()} {http_target(call)}", path, line, cond, why, via])
            continue
        # follow into our own code
        target = None
        if not head and name in idx["funcs"]:
            target = (path, idx["funcs"][name], None)
        elif not head and name in idx["imports"]:
            ipath, iname = idx["imports"][name]
            if ipath:
                tidx = module_index(ipath)
                if (iname or name) in tidx["funcs"]:
                    target = (ipath, tidx["funcs"][iname or name], None)
        elif head == "self" and cls and meth in cls["methods"]:
            target = (path, cls["methods"][meth], cls)
        elif head in types:
            tname = types[head]
            tpath = path if tname in idx["classes"] else (idx["imports"].get(tname, (None,))[0])
            if tpath:
                tcls = module_index(tpath)["classes"].get(tname)
                if tcls and meth in tcls["methods"]:
                    target = (tpath, tcls["methods"][meth], tcls)
        elif head in idx["imports"] and idx["imports"][head][0]:
            tidx = module_index(idx["imports"][head][0])
            if meth in tidx["funcs"]:
                target = (idx["imports"][head][0], tidx["funcs"][meth], None)
        if target:
            key = (target[0], target[1].name, target[1].lineno)
            if key in seen:
                continue
            seen.add(key)
            steps.append([depth, "call", f"{target[1].name}()", target[0], target[1].lineno, cond, why, via])
            before = len(steps)
            trace(target[0], target[1], depth + 1, steps, seen, target[2], inherited=cond, inherited_why=why)
            if len(steps) == before and depth >= 1:
                steps.pop()  # drop leaf helpers that do nothing external


def to_api(meth):
    return "".join(p.capitalize() for p in meth.split("_"))


def sql_verb(meth, what):
    verb = {"add": "INSERT", "add_all": "INSERT", "delete": "DELETE", "merge": "UPSERT", "get": "SELECT by id"}.get(meth, "")
    if what.startswith(("SELECT", "UPDATE", "DELETE", "INSERT")):
        return what
    return f"{verb or 'query'} {what}".strip()


def http_target(call):
    for a in call.args[:1]:
        if isinstance(a, ast.JoinedStr):
            s = "".join(v.value if isinstance(v, ast.Constant) else "{…}" for v in a.values)
            return s[:60]
        if isinstance(a, ast.Constant):
            return str(a.value)[:60]
    return "(other service)"


flows = {}
for i, r in enumerate(DATA["routes"]):
    path = FILES[r["f"]]
    if not path.endswith(".py"):
        continue
    fn = find_def(path, r["l"], "")
    if not fn:
        continue
    steps: list[list] = []
    seen: set[tuple] = set()
    cls = None
    trace(path, fn, 0, steps, seen, cls)
    flows[i] = [[d, k, lab, FILE_IDX.get(p, -1), ln, c, why, via] for d, k, lab, p, ln, c, why, via in steps]

OUT.write_text(json.dumps(flows, separators=(",", ":")))
ext = sum(1 for s in flows.values() if any(x[1] != "call" for x in s))
print(f"routes traced={len(flows)} with external calls={ext} size={OUT.stat().st_size/1024:.0f}KB")
