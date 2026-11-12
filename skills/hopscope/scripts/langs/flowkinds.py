"""What a call in Java, Kotlin or C# reaches outside the process, from the receiver's type and the method name, and
conditions in plain words. Used by braceflow.py. Standard library only."""
from __future__ import annotations

import re

from langs.common import strings_in

AWS_TYPES = (("SQS", r"Sqs(Async)?Client|AmazonSQS(Async)?(Client)?|IAmazonSQS|SqsTemplate"),
             ("SES", r"SesV?2?(Async)?Client|AmazonSimpleEmailService(V2)?(Client)?|IAmazonSimpleEmailService(V2)?"),
             ("S3", r"S3(Async)?Client|AmazonS3(Client)?|IAmazonS3|S3Template|TransferUtility"),
             ("Cognito", r"CognitoIdentityProvider\w*|IAmazonCognitoIdentityProvider|AWSCognitoIdentityProvider\w*"),
             ("SNS", r"Sns(Async)?Client|AmazonSNS\w*|IAmazonSimpleNotificationService|SnsTemplate"),
             ("DynamoDB", r"DynamoDb(Async)?Client|AmazonDynamoDB\w*|IAmazonDynamoDB|DynamoDBContext|IDynamoDBContext"),
             ("Lambda", r"Lambda(Async)?Client|AWSLambda\w*|IAmazonLambda"),
             ("Secrets Manager", r"SecretsManager(Async)?Client|IAmazonSecretsManager|AWSSecretsManager\w*"))
AWS_RX = [(svc, re.compile(rf"^(?:{rx})$")) for svc, rx in AWS_TYPES]
HTTP_TYPE = re.compile(r"^(RestTemplate|WebClient|RestClient|HttpClient|OkHttpClient|IHttpClientFactory|IFlurlClient|RestSharp\w*|RestClient\w*)$")
CACHE_TYPE = re.compile(r"^(\w*RedisTemplate|Jedis|JedisPool|JedisCluster|RedisCommands|RedisAsyncCommands|RedissonClient|IDatabase|IDatabaseAsync|"
                        r"IConnectionMultiplexer|ConnectionMultiplexer|IDistributedCache|IRedisClient\w*)$")
KAFKA_TYPE = re.compile(r"^(KafkaTemplate|KafkaProducer|Producer|IProducer|ITopicProducer|ReactiveKafkaProducerTemplate)$")
SQL_TYPE = re.compile(r"^(JdbcTemplate|NamedParameterJdbcTemplate|JdbcClient|EntityManager|Session|IDbConnection|SqlConnection|NpgsqlConnection|"
                      r"DbConnection|MySqlConnection|DSLContext|R2dbcEntityTemplate|DatabaseClient|MongoTemplate)$")
REPO_TYPE = re.compile(r"(Repository|Repo|Dao|DAO)$")
DB_BASES = {"DbContext", "IdentityDbContext", "ApiAuthorizationDbContext", "IdentityUserContext"}
SPRING_REPOS = re.compile(r"^(Jpa|Crud|PagingAndSorting|ListCrud|ListPagingAndSorting|Reactive|Mongo|R2dbc|Coroutine)\w*Repository$|^Repository$")
DB_LABEL = {"postgres": "Postgres", "mysql": "MySQL", "mssql": "SQL Server", "mongo": "MongoDB"}
HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")


def api_name(meth: str) -> str:
    """sendMessage / SendMessageAsync / send_message -> SendMessage."""
    m = re.sub(r"Async$", "", meth)
    return "".join(p[:1].upper() + p[1:] for p in m.split("_") if p)


def aws_service(t: str) -> str | None:
    return next((svc for svc, rx in AWS_RX if rx.match(t)), None)


def sql_verb(meth: str) -> str:
    m = re.sub(r"Async$", "", meth).lower()
    if m in ("findbyid", "getbyid", "find", "findasync", "getreferencebyid", "single", "first", "firstordefault", "singleordefault"):
        return "SELECT by id"
    if m.startswith(("find", "get", "list", "select", "query", "count", "exists", "where", "tolist", "any", "search", "read", "load", "fetch", "stream")):
        return "SELECT"
    if m.startswith(("save", "upsert", "merge")):
        return "UPSERT"
    if m.startswith(("add", "insert", "create", "persist")):
        return "INSERT"
    if m.startswith(("delete", "remove")):
        return "DELETE"
    if m.startswith("update"):
        return "UPDATE"
    if m.startswith(("execute", "exec")):
        return "query"
    return ""


SQL_TEXT = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE)\b(?:\s+(?:INTO\s+)?([\w.]+)\s+SET\b|.*?\b(?:FROM|INTO)\s+([\w.]+))?", re.I | re.S)
NOT_SQL = {"contains", "detach", "clear", "close", "gettransaction", "unwrap", "getcriteriabuilder", "setflushmode", "isopen", "dispose"}


def sql_label(meth: str, args: str) -> str | None:
    """What a database call does: the statement in its SQL text (SELECT Specialty), else a verb from the method name;
    None for calls that don't reach the database (EntityManager.contains)."""
    for s in strings_in(args):
        m = SQL_TEXT.match(s)
        if m:
            return f"{m.group(1).upper()} {m.group(2) or m.group(3) or ''}".strip()
    low = re.sub(r"Async$", "", meth).lower()
    if low in NOT_SQL:
        return None
    if low.startswith("create") and ("query" in low or "criteria" in low):
        return "query"
    if low == "persist":
        return "INSERT"
    return sql_verb(meth) or "query"


def cache_verb(meth: str, after: str) -> str:
    m = re.sub(r"Async$", "", meth)
    if m.lower().startswith("opsfor") or m in ("boundValueOps", "boundHashOps"):
        nxt = re.match(r"\s*\.\s*(\w+)", after)
        m = nxt.group(1) if nxt else m
    low = m.lower()
    for key, verb in (("del", "DELETE"), ("remove", "DELETE"), ("incr", "INCR"), ("expire", "EXPIRE"), ("publish", "PUBLISH"),
                      ("set", "SET"), ("get", "GET"), ("push", "PUSH"), ("exists", "EXISTS")):
        if key in low:
            return verb
    return m.upper()


def http_call(meth: str, args: str, after: str) -> tuple[str, str]:
    """(verb, target) of an HTTP client call: restTemplate.postForObject("http://x/y", ...), http.GetAsync(...),
    webClient.get().uri("/x")."""
    low = re.sub(r"Async$", "", meth).lower()
    verb = next((v.upper() for v in HTTP_VERBS if low.startswith(v)), "")
    if low in ("exchange", "send", "sendasync", "execute", "request"):
        m = re.search(r"HttpMethod\.(\w+)", args + after[:200])
        verb = m.group(1).upper() if m else "SEND"
    target = strings_in(args)
    if not target:
        m = re.match(r"\s*\.\s*uri\s*\(\s*\"([^\"]*)\"", after)
        target = [m.group(1)] if m else []
    text = target[0] if target else "(other service)"
    return verb or "SEND", text[:60]


def _words(expr: str) -> str:
    """roast.isReady() -> 'roast is ready'; member.Points -> 'points'; _members -> 'members'."""
    e = re.sub(r"\(\s*\)", "", expr.strip())
    parts = [p for p in re.split(r"\.|\?\.|!!\.", e) if p]
    if not parts or not all(re.fullmatch(r"\w+", p) for p in parts):
        return ""
    last = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"^(?:get|Get)(?=[A-Z])", "", parts[-1].lstrip("_"))).lower()
    if len(parts) > 1 and last.startswith(("is ", "has ", "can ", "should ")):
        return f"{parts[-2].lstrip('_').lower()} {last}"
    return last.replace("_", " ")


def phrase(cond: str) -> str:
    """A Java/Kotlin/C# condition in plain words, or '' when it can't be said simply."""
    c = re.sub(r"\s+", " ", cond.strip())
    if "&&" in c or "||" in c:
        parts = [phrase(p) for p in re.split(r"&&|\|\|", c)]
        return (" and " if "&&" in c else " or ").join(parts) if all(parts) else ""
    m = re.fullmatch(r"(.+?)\s*(==|!=|is not|is)\s*null", c)
    if m:
        w = _words(m.group(1))
        return f"{w} is {'missing' if m.group(2) in ('==', 'is') else 'set'}" if w else ""
    m = re.fullmatch(r"(!)?\s*(?:string\.IsNullOrEmpty|String\.IsNullOrEmpty|string\.IsNullOrWhiteSpace)\((.+)\)", c)
    if m:
        w = _words(m.group(2))
        return f"{w} is {'set' if m.group(1) else 'missing'}" if w else ""
    m = re.fullmatch(r"(.+?)\s*(>=|<=|>|<|==|!=)\s*(.+)", c)
    if m:
        left, right = _words(m.group(1)), m.group(3).strip()
        r = right if re.fullmatch(r"-?[\d.]+|\"[^\"]*\"|true|false", right) else _words(right)
        verb = {">": "is above", "<": "is below", ">=": "is at least", "<=": "is at most", "==": "is", "!=": "is not"}[m.group(2)]
        return f"{left} {verb} {r}" if left and r else ""
    neg = c.startswith("!")
    w = _words(c.lstrip("!").strip())
    if not w:
        return ""
    if " is " in w or " has " in w or " can " in w:
        return w.replace(" is ", " is not ", 1) if neg else w
    if w.endswith(" empty"):
        return w.replace(" empty", " is not empty" if neg else " is empty")
    return f"there is no {w}" if neg else f"{w} is set"


def catch_text(spec: str) -> str:
    """catch (FooException e) -> 'a foo error happens'; Exception, Throwable or none -> 'it fails'."""
    m = re.match(r"\s*(?:\w+\s*:\s*)?([\w.|\s]+?)(?:\s+\w+)?\s*$", spec or "")
    names = [n.strip().split(".")[-1] for n in (m.group(1).split("|") if m else []) if n.strip()]
    if not names or any(n in ("Exception", "Throwable", "RuntimeException", "Error") for n in names):
        return "it fails"
    spaced = " or ".join(re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", re.sub(r"(Exception|Error)$", "", n)).lower() for n in names)
    return f"a {spaced} error happens"
