"""Link code to infrastructure: which code folder runs on which ECS service, which queues and
database each service uses, and which worker consumes each queue. Writes links.json.

    python linker.py --aws data/aws.json --code data/code.json [--overrides hopscope.json] --out data/links.json

Every automatic link carries a confidence (0-1) and the evidence behind it. Anything below 0.6 is
listed under "review" so a person can confirm it; hopscope.json overrides always win.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

from iac_common import mask_numbers
from langs.common import read_contained
from linker_lambda import lambda_links
from linker_streams import stream_links
from scopes import plain

STOP = {"prod", "production", "staging", "stage", "stg", "dev", "test", "qa", "svc", "service", "services", "ecs", "app", "the", "main", "default",
        "src", "server", "fargate", "task", "container", "img", "image", "latest", "v1", "v2"}
DEPLOY_FILE = re.compile(r"(Dockerfile|docker-compose[^/]*\.ya?ml|compose[^/]*\.ya?ml|\.github/workflows/[^/]+\.ya?ml|buildspec[^/]*\.ya?ml|Makefile|"
                         r"[^/]*task[-_]?def[^/]*\.json|\.tf|deploy[^/]*\.(sh|ya?ml|json)|serverless\.ya?ml|copilot/.+\.ya?ml|\.gitlab-ci\.yml)$", re.I)
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".terraform", "floorplan"}
REVIEW_BELOW = 0.6
GENERIC = {"worker", "workers", "backend", "frontend", "server", "api", "web", "app", "service", "agent", "base", "main", "consumer", "processor", "proxy", "nginx", "sidecar",
           "handler", "lambda", "function", "functions", "lib", "common", "shared"}
MIN_SCORE = 0.8  # below this, a service stays unplaced rather than guessed


def tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", text.lower())
    out = set()
    for w in words:
        if len(w) < 3 or w in STOP or w.isdigit():
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)  # payments ~ payment
    return out


def deploy_files(base: Path) -> list[tuple[str, str]]:
    """Deployment files under base: regular files inside it, never through a symlink, under 300 KB."""
    out = []
    root = base.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for f in filenames:
            rel = str((Path(dirpath) / f).relative_to(base))
            if DEPLOY_FILE.search(rel) and (text := read_contained(root, rel, 300_000)) is not None:
                out.append((rel, text))
    return out


def service_of(path: str, services: list[str]) -> str | None:
    for s in sorted(services, key=len, reverse=True):
        if s == "." or path.startswith(s + "/"):
            return s
    return None


def _built_service(built: str, services: list[str]) -> str | None:
    return service_of(built.rstrip("/") + "/", services) or (built if built in services else None)


def unscanned_builds(code: dict, inv: dict, services: list[str]) -> dict[str, str]:
    """Services built from a folder that is in the repo but holds no scanned code (SQL, dbt, config): guessing
    another folder by name would be wrong, so they stay unplaced with this reason."""
    base = Path(code.get("base") or ".")
    out = {}
    for x in inv["ecs"]:
        built = str(x.get("build") or "").rstrip("/")
        if not built or built == "." or _built_service(built, services) or any(s.startswith(built + "/") for s in services):
            continue
        if (base / built).is_dir():
            out[f'{x["cluster"]}/{x["n"]}'] = f"builds {built}, where no code was scanned"
    return out


def score_placement(code: dict, inv: dict, deploys: list[tuple[str, str]], services: list[str]) -> dict[str, dict]:
    """For each production ECS service, the code folder it most likely runs, with evidence."""
    ecs = inv["ecs"]
    svc_tokens = {s: tokens(Path(s).name) | (tokens(Path(s).parent.name) if Path(s).parent.name not in ("", ".") else set()) for s in services}
    ecs_tokens = {f'{x["cluster"]}/{x["n"]}': tokens(x["n"]) | tokens(" ".join(x["images"] + x["containers"])) for x in ecs}
    cluster_tokens = {f'{x["cluster"]}/{x["n"]}': tokens(plain(x["cluster"])) for x in ecs}  # "eu-west-1:shop" is the shop cluster in one region
    clusters = {plain(x["cluster"]) for x in ecs}
    cdf = Counter(t for c in clusters for t in tokens(c))
    convention = {t for t, n in cdf.items() if n >= max(2, 0.4 * len(clusters))}  # e.g. the company prefix in most cluster names
    ecs_tokens = {k: v - convention for k, v in ecs_tokens.items()}
    cluster_tokens = {k: v - convention for k, v in cluster_tokens.items()}
    cidf = {t: math.log(1 + len(clusters) / n) for t, n in cdf.items()}
    df = Counter(t for ts in ecs_tokens.values() for t in ts)
    idf = {t: math.log(1 + len(ecs) / c) for t, c in df.items()}
    files_by_service = Counter(service_of(f["p"], services) for f in code["files"])
    result: dict[str, dict] = {}
    unscanned = unscanned_builds(code, inv, services)
    for x in ecs:
        key = f'{x["cluster"]}/{x["n"]}'
        built = x.get("build")
        if key in unscanned:
            continue
        if built is not None:
            s = _built_service(built, services)
            if s:
                files = [f for f in str(x.get("buildFrom") or "").split(", ") if f]  # Terraform rows name every file in the folder
                source = files[0] + (f" (+{len(files) - 1} more)" if len(files) > 1 else "") if files else "a deploy file"
                result[key] = {"code": s, "confidence": 1.0, "evidence": [f"{source} builds {built}"]}
                continue
        if x.get("ownImage") is False:
            continue  # public image (a database UI, a model gateway…): nothing in these repos builds it
        # Only names that identify this service; a container called "worker" says nothing.
        names = {n for n in {x["n"], *x["images"]} if n and n.lower() not in GENERIC and len(n) > 3}
        best: tuple[float, str | None, list[str]] = (0.0, None, [])
        runner_up = 0.0
        for s in services:
            if not files_by_service.get(s):
                continue
            ev, score = [], 0.0
            overlap = ecs_tokens[key] & svc_tokens[s]
            if overlap:
                w = sum(idf.get(t, 1) for t in overlap)
                score += w
                ev.append(f"name match: {', '.join(sorted(overlap))}")
            c_overlap = cluster_tokens[key] & svc_tokens[s] - overlap
            if c_overlap:
                score += 0.8 * sum(cidf.get(t, 1) for t in c_overlap)
                ev.append(f"cluster name: {', '.join(sorted(c_overlap))}")
            for rel, text in deploys:
                inside = s != "." and rel.startswith(s + "/") and not any(o != s and o.startswith(s + "/") and rel.startswith(o + "/") for o in services)
                hits = [m for n in names for m in re.finditer(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)]
                if not hits:
                    continue
                near = s != "." and any(text.find(f"{s}/", max(0, m.start() - 400), m.end() + 400) >= 0 for m in hits)
                if inside or near:  # a deploy file only vouches for the folder it lives in, or a path named right next to the service
                    # With no name in common, a mention is usually a dependency (a compose file using a database), not the build.
                    full = bool(overlap or c_overlap)
                    score += 3 if full else 1
                    ev.append(f"{rel} mentions {hits[0].group(0)}" + ("" if full else " (weak: no name in common)"))
                    break
            if score > 0:  # tie-breaker: deployable services are usually top-level folders with more code
                score += 0.15 * math.log(1 + files_by_service.get(s, 0)) - 0.1 * s.count("/")
            if score > best[0]:
                runner_up = best[0]
                best = (score, s, ev)
            elif score > runner_up:
                runner_up = score
        if best[1] and best[0] >= MIN_SCORE:
            conf = min(1.0, best[0] / 3)
            if runner_up >= 0.8 * best[0]:  # two folders almost equally likely: ask a person
                conf *= 0.5
                best[2].append(f"close second ({runner_up:.1f} vs {best[0]:.1f})")
            result[key] = {"code": best[1], "confidence": round(conf, 2), "evidence": best[2]}
    for p in result.values():  # evidence quotes deploy files and image names, which can carry an account number
        p["evidence"] = [mask_numbers(e) for e in p["evidence"]]
    return result


QUEUE_WORDS = re.compile(r"queue|sqs", re.I)
QUEUE_ENV = re.compile(r"\b([A-Z][A-Z0-9_]*QUEUE[A-Z0-9_]*)\b")
CONSUMER = re.compile(r"receive_message|ReceiveMessage(Command)?|\.consume\(|@sqs|sqs_listener|\.poll\(")


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\.fifo$", "", name.lower()))


def mentions_queue(text: str, stem: str) -> bool:
    """Design mode has no traffic to confirm a queue, so a bare substring is not enough: the name must stand
    alone as a string literal or the tail of a URL, or an environment variable named ...QUEUE must carry it."""
    token = re.compile(rf"(?<![\w-]){re.escape(stem)}(?:\.fifo)?(?![\w-])")
    for line in text.splitlines():
        for m in token.finditer(line):
            before, after = line[max(0, m.start() - 1):m.start()], line[m.end():m.end() + 1]
            hint = QUEUE_WORDS.search(line) is not None
            if before in ("'", '"') and after == before and (len(stem) >= 6 or hint):
                return True
            if before == "/" and m.start() >= 2 and re.match(r"\w", line[m.start() - 2]) and (hint or "amazonaws" in line):
                return True
        for var in QUEUE_ENV.findall(line):
            core = _squash(re.sub(r"_?(QUEUE|URL|NAME)(?=_|$)_?", "", var))
            if len(core) >= 4 and _squash(stem).endswith(core):
                return True
    return False


def same_region(a: dict, b: dict) -> bool:
    """Rows of a merged inventory carry their region and account; a single-scope inventory's rows carry neither."""
    return all(not a.get(k) or not b.get(k) or a[k] == b[k] for k in ("region", "account"))


def _same_scope(q: str, x: dict, names: list[str], rows: dict[str, dict]) -> list[str]:
    """The queues named q (exactly, or once a scope prefix is dropped) in the service's own region and account."""
    return [n for n in names if (n == q or plain(n) == q) and same_region(x, rows.get(n, {}))]


def queue_links(code: dict, inv: dict, services: list[str], placement: dict[str, dict], base: Path) -> tuple[dict, dict]:
    design = inv.get("mode") == "design"  # no traffic numbers: every queue in the files counts
    names = sorted({q["n"] for q in inv["sqs"] if design or q["sent"] > 0 or q["backlog"] > 0}, key=len, reverse=True)
    uses: dict[str, set[str]] = {}
    consumers: dict[str, set[str]] = {}
    stems = {n: re.sub(r"\.fifo$", "", plain(n)) for n in names}  # the name the code uses, without a region or account prefix
    rows = {q["n"]: q for q in inv["sqs"]}
    root = base.resolve()
    for f in code["files"]:
        s = service_of(f["p"], services)
        if not s:
            continue
        text = read_contained(root, f["p"])
        if text is None:
            continue
        for n in names:
            if (mentions_queue(text, stems[n]) if design else stems[n] in text):
                uses.setdefault(s, set()).add(n)
                if CONSUMER.search(text):
                    consumers.setdefault(n, set()).add(s)
    by_code: dict[str, list[str]] = {}
    for key, p in placement.items():  # queue URLs in the container environment are exact
        x = next((e for e in inv["ecs"] if f'{e["cluster"]}/{e["n"]}' == key), None)
        if x is None:  # a hopscope.json key that names no service in this inventory
            continue
        for r in x["refs"]:
            if "sqs." in r["v"]:
                q = r["v"].rsplit("/", 1)[-1]  # a bare SQS host names no queue
                found = _same_scope(q, x, names, rows) if "/" in r["v"] else []
            elif design and "QUEUE" in r["k"].upper():  # design rows keep the bare queue name
                found = _same_scope(r["v"], x, names, rows)
            else:
                found = []
            if found:  # no empty entry for a service whose setting names no known queue
                uses.setdefault(p["code"], set()).update(found)
    for s, qs in uses.items():
        by_code[s] = sorted(qs)
    workers: dict[str, list[str]] = {}
    for q in names:
        code_side = consumers.get(q, set())
        candidates = [e for e in inv["ecs"] if e["env"] == "prod" and e["running"] and re.search(r"worker|consumer|processor|queue|sqs|aggregat", e["n"], re.I)
                      and same_region(e, rows.get(q, {})) and (placement.get(f'{e["cluster"]}/{e["n"]}', {}).get("code") in code_side or any(plain(q) in r["v"] for r in e["refs"]))]
        if candidates:
            w = candidates[0]
            exts = {x for f in code["files"] if service_of(f["p"], services) == placement.get(f'{w["cluster"]}/{w["n"]}', {}).get("code") for x in f["x"]}
            job = "SES: SendEmail" if "ses" in exts and "email" in q else "Postgres: write result" if exts & {"postgres", "mysql", "mongo"} else "process message"
            workers[q] = [w["n"], job]
    return by_code, workers


def db_links(inv: dict, placement: dict[str, dict]) -> dict[str, str]:
    """The database each code folder uses, from host names in its service's environment. Compose files can
    each define a "postgres": the one from the service's own file wins."""
    by_endpoint: dict[str, list[dict]] = {}
    for d in inv["rds"]:
        for ep in {d.get("endpoint") or "", d.get("clusterEndpoint") or ""}:
            if ep:
                by_endpoint.setdefault(ep.split(".")[0], []).append(d)
    db_for: dict[str, str] = {}
    for key, p in placement.items():
        x = next((e for e in inv["ecs"] if f'{e["cluster"]}/{e["n"]}' == key), None)
        if x is None:
            continue
        for r in x["refs"]:
            found = by_endpoint.get(re.sub(r"^[a-z]+://", "", r["v"]).split(".")[0])
            if found and x["env"] == "prod":
                same = [d for d in found if d.get("src") and d.get("src") == x.get("buildFrom")]
                db_for.setdefault(p["code"], (same or found)[-1]["n"])
    return db_for


def resolve_folder(path: str, services: list[str]) -> str | None:
    """A code folder from hopscope.json, as a scanned service. Overrides written when the code base was a parent
    folder ("<repo>/payments") still match when Hopscope is pointed at the single repo ("payments")."""
    parts = [x for x in str(path or "").strip().strip("/").split("/") if x]
    for i in range(len(parts)):
        cand = "/".join(parts[i:])
        if cand in services:
            return cand
    return None


def _not_scanned(path: str) -> str:
    return f"hopscope.json points at {path}, which isn't a scanned folder"


def expand_key(key: str, keys: list[str] | None) -> list[str]:
    """The inventory's cluster/service keys a hopscope.json key means: itself, or in a merged inventory every service whose
    key without the region or account prefix is the same ("kestrel/shop-api" -> "eu-west-1:kestrel/shop-api", …)."""
    if keys is None or key in keys:
        return [key]
    return [k for k in keys if plain(k) == plain(key)]


def expand_workers(workers: dict, inv: dict) -> dict:
    """hopscope.json queueWorkers keyed by plain queue names, for every queue of that name in a merged inventory."""
    names = [str(q.get("n")) for q in inv.get("sqs") or [] if isinstance(q, dict)]
    out: dict = {}
    for q, w in (workers or {}).items():
        for n in ([q] if q in names or not any(plain(x) == q for x in names) else [x for x in names if plain(x) == q]):
            out[n] = w
    return out


def apply_overrides(placement: dict[str, dict], ov: dict, services: list[str], keys: list[str] | None = None) -> dict:
    """hopscope.json placement, labels and queues, with every folder resolved to a scanned service. A folder that
    matches none is not used: a placement goes to review, and each one is reported on stderr. With `keys` (the
    inventory's cluster/service keys), a plain key covers every region's copy and a key naming no service is reported."""
    placed, review = dict(placement), []
    ignored: set[str] = set()
    for key, s in (ov.get("placement") or {}).items():  # {"cluster/service": "code/folder"}; null = not our code
        targets = expand_key(str(key), keys)
        if not targets:
            print(f"warning: placement of {key}: no service with that name in the inventory", file=sys.stderr)
            continue
        if s is None:
            ignored.update(targets)
            for k in targets:
                placed.pop(k, None)
            continue
        folder = resolve_folder(s, services)
        for k in targets:
            if folder:
                placed[k] = {"code": folder, "confidence": 1.0, "evidence": ["set in hopscope.json"]}
            else:
                placed.pop(k, None)
                review.append({"ecs": k, "code": str(s), "confidence": 0.0, "evidence": [_not_scanned(str(s))]})
        if not folder:
            print(f"warning: placement of {key}: {_not_scanned(str(s))}", file=sys.stderr)
    resolved: dict[str, dict] = {"labels": {}, "queues": {}}
    for section in ("labels", "queues"):
        for path, v in (ov.get(section) or {}).items():
            folder = resolve_folder(path, services)
            if folder:
                resolved[section][folder] = v
            else:
                print(f"warning: {section} entry: {_not_scanned(str(path))}", file=sys.stderr)
    return {"placement": placed, "review": review, "ignored": ignored, **resolved}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--aws", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--overrides")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    inv, code = json.load(open(a.aws)), json.load(open(a.code))
    ov = json.load(open(a.overrides)) if a.overrides and Path(a.overrides).exists() else {}
    base = Path(code["base"])
    services = code["services"]
    placement = score_placement(code, inv, deploy_files(base), services)
    ov_ecs = {**ov, "placement": {k: v for k, v in (ov.get("placement") or {}).items() if not str(k).startswith("lambda:")}}
    over = apply_overrides(placement, ov_ecs, services, [f'{e["cluster"]}/{e["n"]}' for e in inv["ecs"]])
    placement, ignored = over["placement"], over["ignored"]
    queues, workers = queue_links(code, inv, services, placement, base)
    queues.update(over["queues"])
    workers.update(expand_workers(ov.get("queueWorkers") or {}, inv))
    ec2_by_id = {e["id"]: e["n"] for e in inv["ec2"]}
    tg_host = {r["tg"]: ec2_by_id[r["instances"][0]] for lb in inv["lb"] for r in lb["routes"] if r["instances"] and r["instances"][0] in ec2_by_id}
    db_for = db_links(inv, placement)
    streams, consumers = stream_links(code, inv, services, placement, base)
    code_to_compute: dict[str, list[str]] = {}
    for key, p in sorted(placement.items(), key=lambda kv: -kv[1]["confidence"]):
        code_to_compute.setdefault(p["code"], []).append(key.split("/", 1)[1])
    lambdas = lambda_links(code, inv, placement, ov.get("placement"))
    for fn, p in sorted(lambdas.items()):
        code_to_compute.setdefault(p["code"], []).append(fn)
    labels = {s: over["labels"].get(s) or re.sub(r"[_\-]+", " ", Path(s).name).strip().capitalize() for s in code_to_compute}
    review = [{"ecs": k, **v} for k, v in placement.items() if v["confidence"] < REVIEW_BELOW] + over["review"]
    unplaced = sorted(k for e in inv["ecs"] if (k := f'{e["cluster"]}/{e["n"]}') not in placement and k not in ignored and e["running"] and e.get("ownImage") is not False)
    why = {k: v for k, v in unscanned_builds(code, inv, services).items() if k in unplaced}
    third_party = sorted(f'{e["cluster"]}/{e["n"]}' for e in inv["ecs"] if e.get("ownImage") is False)
    out = {"placement": placement, "codeToCompute": code_to_compute, "labels": labels, "queues": queues, "queueWorkers": dict(sorted(workers.items())),
           "tgHost": tg_host, "dbFor": db_for, "lambdas": lambdas, "streams": streams, "consumers": consumers, "review": review, "unplaced": unplaced, "unplacedWhy": why, "thirdParty": third_party}
    json.dump(out, open(a.out, "w"), indent=1)
    print(f"links: {len(lambdas)} Lambda functions placed, " if lambdas else "links: ", end="")
    print(f"{len(placement)} ECS services placed ({len(review)} to review, {len(unplaced)} unplaced), {sum(map(len, queues.values()))} queue links, "
          f"{len(workers)} queue workers, {len(tg_host)} EC2 targets -> {a.out}")


if __name__ == "__main__":
    main()
