"""HTTP calls a JavaScript/TypeScript frontend makes: fetch(...), axios.get/post(...), axios instances, Angular
HttpClient (this.http.get(...)), useSWR/useFetch/$fetch, with the URL rebuilt from string literals, template literals,
`+` concatenation and the file's own constants. A leading base (API_BASE, environment.apiUrl, process.env.X) is dropped;
other variables become {param}. Absolute URLs to other hosts and static files are skipped."""
from __future__ import annotations

import re
from pathlib import Path

from langs.common import blanked, line_of, match_close, split_top

VERBS = ("get", "post", "put", "patch", "delete", "head")
RECEIVERS = {"axios", "api", "apiClient", "client", "http", "httpClient", "$http", "$axios", "request", "instance", "axiosInstance", "ky",
             "this.http", "this.httpClient", "this.$http", "this.$axios", "this.api", "this.client"}
FETCHES = ("fetch", "$fetch", "ofetch", "useFetch", "useSWR", "useLazyFetch")
VERB_CALL = re.compile(r"((?:this\.)?\$?\w+)\s*\.\s*(get|post|put|patch|delete|head)\s*(?:<[^<>()]*(?:<[^<>()]*>[^<>()]*)*>)?\s*\(")
FETCH_CALL = re.compile(r"(?<![\w.$])(\$?\w+)\s*(?:<[^<>()]*>)?\s*\(")
INSTANCE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:axios|ky)\.(?:create|extend)\s*\(")
INJECTED = re.compile(r"(?:private|public|protected|readonly)\s+(?:readonly\s+)?(\w+)\s*:\s*HttpClient\b|\b(\w+)\s*=\s*inject\(\s*HttpClient\s*\)")
FIELD = re.compile(r"^[ \t]+(\w+)\s*(?::\s*[^=\n]+?)?\s*=\s*([^;\n=][^;\n]*)", re.M)  # class fields: entityUrl = BASE + 'x'
CONST = re.compile(r"^[ \t]*(?:export\s+)?(?:(?:const|let|var|private|public|protected|readonly|static)\s+)+(\w+)\s*(?::\s*[^=\n]+?)?\s*=\s*([^;\n]+)", re.M)
# A leading variable is our own backend's base only when named like one; STRIPE_URL or SENDGRID_HOST are someone else's.
BASE_NAME = re.compile(r"api_?url|api_?base|api_?root|api_?host|api_?endpoint|backend|base_?url|server_?url|^base$|^api$", re.I)
STATIC = re.compile(r"\.(json|svg|png|jpe?g|gif|webp|ico|css|js|map|txt|html|woff2?|md)$", re.I)
BASE, MAX_DEPTH = "\x00", 4


def _consts(code: str) -> dict[str, str]:
    out = {m.group(1): m.group(2).strip() for m in FIELD.finditer(code)}
    out.update({m.group(1): m.group(2).strip() for m in CONST.finditer(code)})
    return out


def _param(expr: str) -> str:
    names = re.findall(r"[A-Za-z_$][\w$]*", expr)
    names = [n for n in names if n not in ("this", "encodeURIComponent", "String", "toString", "await")]
    return "{" + (names[-1] if names else "param") + "}"


def _template(expr: str, consts: dict[str, str], depth: int) -> str:
    out, i, body = [], 0, expr[1:-1]
    while i < len(body):
        j = body.find("${", i)
        if j < 0:
            out.append(body[i:])
            break
        out.append(body[i:j])
        close = match_close(body, j + 1)
        val = _value(body[j + 2:close], consts, depth + 1, first=(j == 0))
        if not _glued(val, "".join(out), "/" in body[close + 1:]):
            out.append(val)
        i = close + 1
    return "".join(out)


def _glued(val: str, before: str, slash_after: bool) -> bool:
    """A variable stuck to the end of the last segment (`/agents${qs}`, '/agents' + query) is a query string, not a
    path parameter: only a variable forming a whole segment is one."""
    return val.startswith("{") and bool(before) and not before.endswith(("/", BASE)) and not slash_after


def _value(expr: str, consts: dict[str, str], depth: int = 0, first: bool = True) -> str:
    """An expression as URL text: literals as written, the leading base as BASE, other variables as {name}."""
    e = expr.strip()
    while e.startswith("(") and match_close(e, 0, "(", ")") == len(e) - 1:
        e = e[1:-1].strip()
    if depth > MAX_DEPTH or not e:
        return _param(e)
    parts = [p for p in _split_plus(e)]
    if len(parts) > 1:
        text = ""
        for k, p in enumerate(parts):
            val = _value(p, consts, depth + 1, first and k == 0)
            later = "".join(parts[k + 1:])
            if not _glued(val, text, "/" in later):
                text += val
        return text
    if len(e) >= 2 and e[0] == e[-1] and e[0] in "'\"":
        return e[1:-1]
    if e[0] == "`" and e[-1] == "`":
        return _template(e, consts, depth)
    name = re.sub(r"^this\.", "", e)
    if re.fullmatch(r"[\w$.]+", e):
        if name in consts:
            val = _value(consts[name], consts, depth + 1, first)
            return val if val else _param(e)  # sessionId = '' is a default filled in later, not a constant
        if first and BASE_NAME.search(e.split(".")[-1]):
            return BASE
    return _param(e)


def _split_plus(e: str) -> list[str]:
    out, depth, cur, quote = [], 0, "", ""
    for ch in e:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "+" and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    return out + [cur]


def normalise(url: str) -> str | None:
    """URL text -> a path ('/api/x/{id}'), or None for other hosts, static files and URLs that are only a variable."""
    lead = re.match(r"^\{[^}/]*\}(?=/api(?:/|$))", url)  # ${PAYMENTS_HOST}/api/refunds: same-origin style API path
    based = url.startswith(BASE) or bool(lead)
    u = url[lead.end():] if lead else url.replace(BASE, "")
    m = re.match(r"^(?:https?:)?//([^/{]+)(.*)$", u)
    if m:
        if not re.match(r"(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$", m.group(1)) and not based:
            return None
        u = m.group(2)
    u = re.split(r"[?#]", u, maxsplit=1)[0]
    if not based and not u.startswith("/"):
        return None
    path = "/" + "/".join(s for s in u.split("/") if s)
    if path == "/" or STATIC.search(path) or re.fullmatch(r"/\{[^}]*\}", path):
        return None
    return path


def _first_arg(code: str, open_: int) -> tuple[str, str]:
    close = match_close(code, open_, "(", ")")
    args = split_top(code[open_ + 1:close])
    return (args[0] if args else ""), code[open_ + 1:close]


ROUTE_RECEIVER = re.compile(r"^(?:app|router|server|api|\w+Router)$")  # what js.routes reads as a route definition


def _has_handler(args: str) -> bool:
    """The second argument is a request handler (arrow, function, or a bare name like listNotes / notes.remove):
    then api.get('/x', handler) defines a route; axios takes data or a config object there."""
    parts = split_top(args)
    if len(parts) < 2:
        return False
    second = parts[1].strip()
    return "=>" in second or second.startswith(("function", "async")) or bool(re.fullmatch(r"[A-Za-z_$][\w$.]*", second))


def client_calls(path: Path, text: str, ctx=None) -> list[tuple[int, str, str]]:
    """(line, method, path) of each HTTP call to a path this file makes. With ctx, client bases imported from other
    scanned files are followed (ctx.index: only scanned files are read)."""
    from langs import jsbases
    code = blanked(text)
    consts = _consts(code)
    own, wrapper_spans = jsbases.local_bases(code, consts)
    bases = {**jsbases.imported_bases(path, code, ctx.index if ctx is not None else {}), **own}
    receivers = RECEIVERS | set(INSTANCE.findall(code)) | {"this." + (a or b) for a, b in INJECTED.findall(code)} | set(bases)
    found: list[tuple[int, int, str, str, str]] = []
    for m in VERB_CALL.finditer(code):
        if m.group(1) in receivers:
            first, allargs = _first_arg(code, m.end() - 1)
            if ROUTE_RECEIVER.match(m.group(1)) and _has_handler(allargs):
                continue
            found.append((m.start(), line_of(text, m.start()), m.group(2).upper(), first, bases.get(m.group(1), ("", ""))[1]))
    wrappers = {n for n, (kind, _) in bases.items() if kind == "wrapper"}
    for m in FETCH_CALL.finditer(code):
        name = m.group(1)
        inside = any(s <= m.start() < e for s, e in wrapper_spans)
        if (name in FETCHES and not inside) or name == "axios" or (name in wrappers and not re.search(r"function\s+$", code[max(0, m.start() - 12):m.start()])):
            first, allargs = _first_arg(code, m.end() - 1)
            given = re.search(r"method\s*:\s*['\"`](\w+)", allargs)
            if first.strip().startswith("{"):  # axios({ url, method })
                url = re.search(r"url\s*:\s*([^,}]+)", first)
                first = url.group(1) if url else ""
            base = bases[name][1] if name in wrappers else ""
            found.append((m.start(), line_of(text, m.start()), (given.group(1) if given else "GET").upper(), first, base))
    out = []
    for _, line, verb, expr, base in sorted(found):
        p = normalise(jsbases.join(base, _value(expr, consts))) if expr.strip() else None
        if p:
            out.append((line, verb, p))
    return out
