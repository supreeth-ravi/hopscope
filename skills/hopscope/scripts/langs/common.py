"""Helpers shared by the language modules: names, line numbers, the external systems every language shares, and
small text tools for brace languages (comments blanked out, matching braces, string literals). Standard library only."""
from __future__ import annotations

import re
from functools import lru_cache

# External systems, detected from imports and SDK calls in any language. Keys are what the console draws.
EXTERNAL = {
    "postgres": r"^\s*(from|import)\s+(sqlalchemy|sqlmodel|asyncpg|psycopg2?|alembic|tortoise|peewee|django\.db)\b|from\s+['\"](pg|postgres|@prisma/client|typeorm|sequelize|knex|drizzle-orm)['\"]|\"github\.com/(lib/pq|jackc/pgx)",
    "mysql": r"^\s*(from|import)\s+(pymysql|MySQLdb|aiomysql)\b|from\s+['\"]mysql2?['\"]",
    "mongo": r"^\s*(from|import)\s+(pymongo|motor|beanie|mongoengine)\b|from\s+['\"]mongoose|mongodb['\"]",
    "cache": r"^\s*(from|import)\s+(redis|aioredis|valkey)\b|from\s+['\"](ioredis|redis)['\"]",
    "llm": r"^\s*(from|import)\s+(openai|litellm|anthropic)\b|from\s+['\"](openai|@anthropic-ai/sdk)['\"]",
    "cognito": r"cognito-idp|CognitoIdentityProvider|cognito_idp",
    "sqs": r"['\"]sqs['\"]|client-sqs|send_message\(|SendMessageCommand",
    "ses": r"['\"]sesv?2?['\"]|client-ses|send_email\(|SendEmailCommand",
    "s3": r"['\"]s3['\"]|client-s3|put_object\(|upload_fileobj\(|PutObjectCommand",
    "search": r"opensearch|elasticsearch",
    "chatapps": r"slack_sdk|slack_bolt|@slack/|telegram",
    "tracing": r"opentelemetry|langfuse|@sentry|sentry_sdk|datadog|ddtrace",
}
EXT_RX = {k: re.compile(v, re.M | re.I) for k, v in EXTERNAL.items()}


def shared_externals(text: str) -> set[str]:
    return {k for k, rx in EXT_RX.items() if rx.search(text)}


def humanize(name: str) -> str:
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", re.sub(r"[_\-.]+", " ", name)).strip().split()
    return " ".join(words).capitalize() if words else name


def line_of(text: str, i: int) -> int:
    return text.count("\n", 0, i) + 1


def blank_comments(text: str, blank_strings: bool = False) -> str:
    """The text with // and /* */ comments (and, when asked, the inside of string literals) replaced by spaces, so
    offsets and line numbers stay the same. Understands "…", '…', `…`, Kotlin/C# raw strings (\"\"\"…\"\"\") and C#
    verbatim strings (@"…")."""
    out = list(text)
    i, n = 0, len(text)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            if blank_strings:
                blank(i + 3, j - 3)
            i = j
        elif c in "\"'`":
            verbatim = c == '"' and i > 0 and text[i - 1] == "@"
            j = i + 1
            while j < n and text[j] != c and (verbatim or text[j] != "\n" or c == "`"):
                if text[j] == "\\" and not verbatim:
                    j += 1
                elif verbatim and text.startswith('""', j):
                    j += 1
                j += 1
            if blank_strings:
                blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def match_close(text: str, i: int, open_: str = "{", close: str = "}") -> int:
    """Index of the bracket closing the one at text[i] (text should have comments and strings blanked), or len(text)."""
    depth = 0
    for k in range(i, len(text)):
        ch = text[k]
        if ch == open_:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return k
    return len(text)


STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def strings_in(args: str) -> list[str]:
    """The double-quoted string literals in an argument list, in order."""
    return [m.group(1) for m in STRING.finditer(args)]


def join_path(prefix: str, sub: str) -> str:
    """A route path from a class or group prefix and a method path: '/api/' + 'x' -> '/api/x'."""
    parts = [p.strip("/") for p in (prefix, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


@lru_cache(maxsize=32)
def blanked(text: str, strings: bool = False) -> str:
    """blank_comments, cached: routes, imports and externals of one file share it."""
    return blank_comments(text, strings)


def split_top(args: str) -> list[str]:
    """An argument list split at its top-level commas (brackets and string literals respected)."""
    out, depth, cur, quote = [], 0, [], ""
    i = 0
    while i < len(args):
        ch = args[i]
        if quote:
            if ch == "\\":
                cur.append(args[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur))
    return out


NAMED_ARG = re.compile(r"^\s*(\w+)\s*[:=]\s*(.*)$", re.S)


def named_args(args: str) -> dict[str, str]:
    """{"": the first positional argument, name: value} for an annotation's or attribute's arguments."""
    out: dict[str, str] = {}
    for k, part in enumerate(split_top(args)):
        m = NAMED_ARG.match(part)
        if m and not part.strip().startswith(('"', "'")):
            out.setdefault(m.group(1), m.group(2).strip())
        elif k == 0:
            out[""] = part.strip()
    return out


UNSURE = "{?}"  # a path built from a constant the scanner couldn't resolve


def _const(ref: str, consts: dict[str, str]) -> str | None:
    """A constant reference (Paths.CUPS, CUPS, a.b.Paths.CUPS) looked up as Type.NAME, then NAME."""
    parts = ref.strip().strip("{}[]").strip().split(".")
    for key in (".".join(parts[-2:]), parts[-1]):
        if key in consts:
            return consts[key]
    return None


def literal_paths(value: str, consts: dict[str, str]) -> list[str]:
    """The paths an annotation value names: string literals ("/a", {"/a", "/b"}, ["/a"]), "/a" + CONST, or a constant.
    A constant that can't be resolved becomes {?}, so the route is kept but never matched."""
    if not value:
        return []
    if "+" in re.sub(r'"(?:[^"\\]|\\.)*"', "", value):
        parts = []
        for piece in value.split("+"):
            s = strings_in(piece)
            parts.append(s[0] if s else (_const(piece, consts) or UNSURE))
        return ["".join(parts)]
    found = strings_in(value)
    if found:
        return found
    ref = value.strip().strip("{}[]").strip()
    if not re.fullmatch(r"[\w.]+", ref):
        return []
    hit = _const(ref, consts)
    return [hit if hit is not None else UNSURE]


CONST = re.compile(r"\b(?:static\s+final|final\s+static|const\s+val|const)\s+(?:String\s+|string\s+)?(\w+)\s*(?::\s*String\s*)?=\s*\"([^\"]*)\"")


def string_consts(code: str) -> dict[str, str]:
    """String constants a file declares (static final / const val / const string), for paths built from them."""
    return {m.group(1): m.group(2) for m in CONST.finditer(code)}


def javadoc_before(text: str, pos: int, reach: int = 4000) -> str:
    """The first sentence of a /** … */ or /// <summary> comment that ends right before pos. Scans backwards from pos
    (at most `reach` characters), so finding every route's comment stays linear in the file size."""
    k = pos
    while k > 0 and text[k - 1].isspace():
        k -= 1
    if text[max(0, k - 2):k] == "*/":
        start = text.rfind("/**", max(0, k - reach), k - 2)
        if start < 0:
            return ""
        body = [ln.strip().lstrip("*").strip() for ln in text[start + 3:k - 2].splitlines()]
    else:
        body = []
        end = k
        while end > 0 and len(body) < 40:
            line_start = text.rfind("\n", max(0, end - reach), end) + 1
            line = text[line_start:end].strip()
            if not line.startswith("///"):
                break
            body.insert(0, line[3:].strip())
            end = max(0, line_start - 1)
        body = [re.sub(r"</?\w+[^>]*>", "", b).strip() for b in body]
    first = next((b for b in body if b and not b.startswith("@")), "")
    return first[:140]


def contained_glob(root: str, patterns: tuple[str, ...], limit: int) -> list:
    """Regular files matching the patterns under root that are not symlinks and resolve inside root."""
    from pathlib import Path
    base = Path(root).resolve()
    out = []
    for pat in patterns:
        for p in sorted(Path(root).glob(pat))[:limit]:
            try:
                if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(base):
                    out.append(p)
            except (OSError, RuntimeError):
                continue
    return out


@lru_cache(maxsize=64)
def build_text(root: str, patterns: tuple[str, ...]) -> str:
    """The lower-cased text of a service's build and settings files (pom.xml, *.csproj, application.yml, ...)."""
    out = []
    for p in contained_glob(root, patterns, 20):
        try:
            if p.stat().st_size < 400_000:
                out.append(p.read_text(errors="ignore").lower())
        except OSError:
            continue
    return "\n".join(out)


DB_HINTS = (("postgres", re.compile(r"postgres|npgsql")), ("mysql", re.compile(r"mysql|mariadb")),
            ("mssql", re.compile(r"sqlserver|mssql|sqlclient")))


def db_kind(root: str, patterns: tuple[str, ...], default: str = "postgres") -> str:
    """Which SQL database a service's build files name: postgres, mysql or mssql (first found, in that order)."""
    text = build_text(root, patterns)
    return next((kind for kind, rx in DB_HINTS if rx.search(text)), default)


CLASS_BODY = re.compile(r"\w+\s*(?:<[^{;]*?>)?\s*(?:\([^{;]*?\))?[^{;]*?\{")


def class_span(bare: str, pos: int) -> tuple[int, int]:
    """The body of the class whose name is at pos: from its '{' to the matching '}' (empty when it has none)."""
    m = CLASS_BODY.match(bare, pos)
    if not m:
        return pos, pos
    return m.end() - 1, match_close(bare, m.end() - 1)
