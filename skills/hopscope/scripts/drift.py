"""Drift: does what is deployed match what the code and the infrastructure files say? Writes data/drift.json.

    python drift.py --data .hopscope/data [--similar]

Compares up to three sources: the code (code.json and links.json), infrastructure files (iac.json in live mode,
aws.json itself in design mode) and the live AWS inventory (aws.json in live mode). In design mode only the
code-to-files checks run.

An AWS resource and a file row are the same only when their names are equal once environment words and numbers are
ignored (prod-shop-api = shop-api), when the file's name was built from a variable and AWS's ends with it, or, for
ECS, when the links place the service on the folder the file builds. Names that merely share words are not matched:
they never produce a difference, and are listed under "uncertain" only with --similar. Per-tenant families and the
single resources of a kind are reported as one finding each, with counts. Standard library only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import drift_code
import drift_names as n
from iac_common import mask_numbers

match = n.match  # kept as drift.match for callers and tests
KINDS = n.KINDS
ORDER = ("link_may_be_wrong", "image_differs", "desired_differs", "taskdef_differs", "ref_missing", "queue_missing", "files_not_deployed",
         "aws_not_in_files", "queue_unreferenced", "client_unmatched")
LABEL = {"link_may_be_wrong": "links may be wrong", "image_differs": "images differ", "desired_differs": "desired counts differ",
         "taskdef_differs": "task definitions differ", "ref_missing": "broken settings", "queue_missing": "missing queues",
         "files_not_deployed": "not deployed", "aws_not_in_files": "in AWS but in no file", "queue_unreferenced": "queues no code references",
         "client_unmatched": "frontend calls with no route",
         "ref_elsewhere": "settings naming resources defined elsewhere", "queue_elsewhere": "queues the code names, defined elsewhere"}
GROUPED = ("aws_not_in_files", "queue_unreferenced")
DIFFERENCES = ("link_may_be_wrong", "image_differs", "desired_differs", "taskdef_differs", "files_not_deployed")
MAX_FINDINGS = 200
CONFIRMED = 0.6  # links below this confidence are listed for review (linker.REVIEW_BELOW)


def _service_of(path: str, services: list[str]) -> str | None:
    for s in sorted(services, key=len, reverse=True):
        if s == "." or path == s or path.startswith(s + "/"):
            return s
    return None


def _builds(f: dict, services: list[str]) -> str | None:
    built = f.get("build")
    return None if built is None else (_service_of(str(built).rstrip("/") + "/", services) or str(built))


def _strict(a_name: str, f: dict) -> str | None:
    lv = n.level(a_name, str(f.get("n") or ""))
    if lv in ("exact", "env") or (lv == "prefix" and f.get("nameVar")):  # a prefix only counts when the file's name came from a variable
        return lv
    return None


def pair(kind: str, a: dict, frows: list[dict], links: dict, services: list[str]) -> tuple[dict | None, str | None]:
    """The file row that defines AWS row `a`, and how it was matched (exact, env, prefix, folder), or (None, None)."""
    cands = [(f, lv) for f in frows if (lv := _strict(str(a.get("n") or ""), f))]
    if cands:
        top = max(n.LEVELS[lv] for _, lv in cands)
        best = [(f, lv) for f, lv in cands if n.LEVELS[lv] == top]
        if len({"-".join(n.canon(str(f.get("n")))) for f, _ in best}) > 1:
            return None, None  # several different resources fit equally: not sure which
        best.sort(key=lambda fl: (fl[0].get("env", "prod") != a.get("env", "prod"), fl[0].get("cluster") != a.get("cluster")))
        return best[0]
    folder = ((links.get("placement") or {}).get(n.key(kind, a)) or {}).get("code") if kind == "ecs" else None
    if folder:
        same = [f for f in frows if _builds(f, services) == folder]
        same.sort(key=lambda f: f.get("env", "prod") != a.get("env", "prod"))
        if same:
            return same[0], "folder"
    return None, None


def _covered(files: dict) -> set[str]:
    """Kinds the files define with something other than docker compose (usually a local-development setup)."""
    return {k for k in KINDS if any(isinstance(x, dict) and x.get("source") != "compose" and not x.get("src") for x in files.get(k) or [])}


def compare_live_files(live: dict, files: dict, links: dict, services: list[str], similar: bool = False) -> tuple[list[dict], list[dict]]:
    findings: list[dict] = []
    uncertain: list[dict] = []
    covered = _covered(files)
    srcs = files.get("sources") or []
    where = f'{len(srcs)} file{"s" if len(srcs) != 1 else ""} ({", ".join(srcs[:3])}{"…" if len(srcs) > 3 else ""})'
    for kind in KINDS:
        frows = [x for x in files.get(kind) or [] if isinstance(x, dict)]
        arows = [x for x in live.get(kind) or [] if isinstance(x, dict)]
        used: set[int] = set()
        missing: list[dict] = []
        for a in arows:
            f, how = pair(kind, a, frows, links, services)
            if f is None:
                if similar:
                    hit, lv = n.match(str(a.get("n") or ""), [str(x.get("n") or "") for x in frows])
                    if hit:
                        uncertain.append({"kind": kind, "aws": n.key(kind, a), "files": hit, "why": f"names are only similar ({lv}): not treated as the same resource"})
                missing.append(a)
                continue
            used.add(id(f))
            if kind == "ecs":
                findings += _ecs_pair(a, f, how or "", links, services)
        if kind in covered:
            for g in n.group(kind, missing):
                findings.append(n.grouped_finding("aws_not_in_files", g, "in AWS, in no infrastructure file", kind,
                                                  "Created by hand or from another repo; it will not be rebuilt from the files and may be forgotten.",
                                                  f"files: nothing with the same name in {where}"))
        for f in frows:
            if id(f) in used or f.get("source") == "compose" or f.get("src"):
                continue
            if similar:
                hit, lv = n.match(str(f.get("n") or ""), [str(x.get("n") or "") for x in arows])
                if hit:
                    uncertain.append({"kind": kind, "files": n.key(kind, f), "aws": hit, "why": f"names are only similar ({lv}): not treated as the same resource"})
            name = str(f["n"]) if kind == "ecs" else n.key(kind, f)
            findings.append(n.finding("files_not_deployed", f"{KINDS[kind]} {name} is in the files but not deployed",
                                      "Never applied, applied to another account or region, or removed by hand.",
                                      [f'files: {KINDS[kind]} {f["n"]} in {f.get("buildFrom") or f.get("source") or "the files"}',
                                       f"AWS: no {KINDS[kind]} with the same name in {live.get('region') or 'this region'}"], name, kind))
    return findings, uncertain


def _image_names(images: list) -> set[str]:
    return {str(i).split("@")[0].rsplit("/", 1)[-1].split(":")[0] for i in images if i and "$" not in str(i) and "<" not in str(i)}


def _ecs_pair(a: dict, f: dict, how: str, links: dict, services: list[str]) -> list[dict]:
    key, out = n.key("ecs", a), []
    src = f.get("buildFrom") or f.get("source") or "files"
    via = {"exact": "same name", "env": "same name apart from environment words", "prefix": "file name built from a variable",
           "folder": "same code folder"}.get(how, how)
    matched = f'matched {f.get("cluster")}/{f["n"]} in {src} ({via})'
    same_env = a.get("env", "prod") == f.get("env", "prod")
    if same_env and f.get("desiredSet") and a.get("desired") != f.get("desired"):
        out.append(n.finding("desired_differs", f'{key}: files say {f["desired"]} task(s), AWS wants {a.get("desired")}',
                             "Changed by hand, by autoscaling or by a deploy tool; the next apply of the files will set it back.",
                             [matched, f'files: desired count {f["desired"]}', f'AWS: desired {a.get("desired")}, running {a.get("running")}'], key, "ecs"))
    fi, ai = _image_names(f.get("images") or []), _image_names(a.get("images") or [])
    if same_env and fi and ai and not fi & ai:
        out.append(n.finding("image_differs", f'{key}: image differs (files {", ".join(sorted(fi))}, AWS {", ".join(sorted(ai))})',
                             "AWS runs a different image repository than the files name.", [matched, f'files: {", ".join(sorted(fi))}', f'AWS: {", ".join(sorted(ai))}'], key, "ecs"))
    fam_a = str(a.get("taskDef") or "").rsplit(":", 1)[0]
    if same_env and f.get("taskFamily") and fam_a and fam_a != f["taskFamily"]:
        out.append(n.finding("taskdef_differs", f'{key}: task definition {a["taskDef"]}, files define family {f["taskFamily"]}',
                             "The service runs a task definition the files don't manage.", [matched, f'files: family {f["taskFamily"]}', f'AWS: {a["taskDef"]}'], key, "ecs"))
    if how == "folder":  # the pair itself rests on the link: say how sure it is
        return [with_link_confidence(x, ((links.get("placement") or {}).get(key) or {}).get("confidence")) for x in out]
    return out + _link_check(key, f, links, services)


def _link_check(key: str, f: dict, links: dict, services: list[str]) -> list[dict]:
    """The links place the service on folder A, but the file row with its name builds folder B: usually the linker guessed wrong."""
    want, placed = _builds(f, services), ((links.get("placement") or {}).get(key) or {})
    if want is None or not placed.get("code") or want == placed["code"]:
        return []
    ev = "; ".join(placed.get("evidence") or []) or "linker"
    return [with_link_confidence(n.finding("link_may_be_wrong", f'The link for {key} may be wrong: Hopscope placed it on {placed["code"]}, but {f.get("buildFrom") or "a compose file"} builds {want} under that name',
                      f'Most often the linker guessed wrong: set "placement": {{"{key}": "{want}"}} in hopscope.json if the file is right. Otherwise the file builds a different folder than is deployed.',
                      [f'links: {key} -> {placed["code"]} (confidence {placed.get("confidence")}, {ev})', f'files: {f["n"]} builds {f.get("build")} in {f.get("buildFrom") or "compose"}'],
                      key, "ecs"), placed.get("confidence"))]


def with_link_confidence(f: dict, conf: Any) -> dict:
    """A copy of a finding that rests on a code-to-compute link, carrying the link's confidence; below 0.6 it is unconfirmed."""
    c = round(float(conf), 2) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
    low = c < CONFIRMED
    return {**f, "linkConfidence": c, "confidence": "low" if low else f["confidence"], **({"unconfirmed": True} if low else {})}


def _label_kind(f: dict) -> str:
    """Design-mode findings about resources that may be defined elsewhere are informational: counted apart from broken references."""
    if f.get("severity") == "info" and f["kind"] in ("ref_missing", "queue_missing"):
        return "ref_elsewhere" if f["kind"] == "ref_missing" else "queue_elsewhere"
    return f["kind"]


def summary(findings: list[dict]) -> str:
    if not findings:
        return "0 findings"
    parts = []
    for kind in (*ORDER, "ref_elsewhere", "queue_elsewhere"):
        mine = [f for f in findings if _label_kind(f) == kind]
        if mine:
            parts.append(f"{LABEL[kind]}: {n.describe(mine) if kind in GROUPED else len(mine)}")
    return f'{len(findings)} finding{"s" if len(findings) != 1 else ""}: ' + "; ".join(parts)


def report(code: dict, links: dict, live: dict | None, files: dict | None, similar: bool = False) -> dict:
    """The drift report. live is the AWS inventory (None in design mode); files the inventory read from infrastructure files."""
    texts = drift_code.texts(code)
    services = code.get("services") or []
    findings: list[dict] = []
    uncertain: list[dict] = []
    checked, skipped = [], []
    if live is not None and files is not None and files.get("sources"):
        f, u = compare_live_files(live, files, links, services, similar)
        findings += f
        uncertain += u
        checked.append("live AWS against infrastructure files")
    elif live is not None:
        skipped.append("AWS against infrastructure files: no infrastructure files found in the repos")
    target, where = (live, "AWS") if live is not None else (files or {}, "the files")
    f, u = drift_code.check(code, links, target, where, texts, similar, design=live is None)
    findings += f
    uncertain += u
    checked.append(f"code against {where}")
    if code.get("clientCalls"):
        f, u = drift_code.unmatched_calls(code)
        findings += f
        uncertain += u
        checked.append("frontend calls against backend routes")
    if live is None:
        skipped.append("AWS checks: design mode reads no AWS account")
        for x in (files or {}).get("ecs") or []:
            findings += _link_check(n.key("ecs", x), x, links, services)
    findings.sort(key=lambda x: (ORDER.index(x["kind"]) if x["kind"] in ORDER else 99, x["resource"]))
    counts: dict[str, int] = {}
    for x in findings:
        counts[x["kind"]] = counts.get(x["kind"], 0) + 1
    return {"mode": "design" if live is None else "live", "sources": {"code": len(texts), "files": list((files or {}).get("sources") or [])[:50], "aws": live is not None},
            "checked": checked, "skipped": skipped, "findings": findings[:MAX_FINDINGS], "uncertain": uncertain[:MAX_FINDINGS], "counts": counts,
            "summary": summary(findings)}


def actions(rep: dict) -> list[list]:
    """At most three action-queue items: differences first, then broken references, then what the files lack, then unused queues."""
    fs = rep.get("findings") or []
    of = lambda *kinds: [f for f in fs if f["kind"] in kinds and f.get("severity") != "info"]  # noqa: E731
    out: list[list] = []
    diff = of(*DIFFERENCES)
    if diff:
        out.append(["warn", f"{len(diff)} difference(s) between the infrastructure files and what runs", "; ".join(f["title"] for f in diff)[:300],
                    "Fix the link in hopscope.json, or apply the files / update them to match; see hopscope.py drift."])
    broken = of("ref_missing", "queue_missing")
    if broken:
        out.append(["warn", f"{len(broken)} reference(s) point at queues or databases that don't exist", "; ".join(f["title"] for f in broken)[:300],
                    "Create the resource or fix the name; see hopscope.py drift."])
    nif = of("aws_not_in_files")
    if nif:
        out.append(["warn", f"In AWS but in no infrastructure file: {n.describe(nif)}", "; ".join(", ".join(f["names"]) for f in nif)[:300] + ". They won't be rebuilt from the files.",
                    "Import them into the files or delete them; see hopscope.py drift."])
    unref = of("queue_unreferenced")
    if unref:
        out.append(["warn", f"Queues no code references: {n.describe(unref)}", "; ".join(", ".join(f["names"]) for f in unref)[:300],
                    "Delete them if nothing outside these repos uses them."])
    nomatch = of("client_unmatched")
    if nomatch:
        out.append(["warn", f"{len(nomatch)} frontend call(s) reach no backend route", "; ".join(f["title"] for f in nomatch)[:300],
                    "Fix the path in the frontend or add the route; see hopscope.py drift."])
    return out[:3]


def _read(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--similar", action="store_true", help="also list names that are only similar (not treated as matches)")
    a = ap.parse_args(argv)
    d = Path(a.data)
    aws, code, links = _read(d / "aws.json") or {}, _read(d / "code.json") or {}, _read(d / "links.json") or {}
    design = aws.get("mode") == "design"
    rep = report(code, links, None if design else aws, aws if design else _read(d / "iac.json"), a.similar)
    (d / "drift.json").write_text(json.dumps(mask_numbers(rep), ensure_ascii=False), encoding="utf-8")  # evidence quotes code and settings
    print(f'drift: {rep["summary"]} -> {d / "drift.json"}')


if __name__ == "__main__":
    main()
