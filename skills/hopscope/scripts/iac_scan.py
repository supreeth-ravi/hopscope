# skills/hopscope/scripts/iac_scan.py
"""Read infrastructure files (docker compose, Terraform, CloudFormation/SAM) under the code base into
aws.json, so Hopscope works with no AWS account. Writes an inventory marked "mode": "design".

    python iac_scan.py --code data/code.json [--region eu-west-1] --out data/aws.json
    python iac_scan.py --code data/code.json --out data/iac.json --optional   # live mode: the files, for drift checks
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
from pathlib import Path

import iac_cfn
import iac_common
import iac_compose
import iac_terraform

SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".terraform", ".aws-sam", "cdk.out", "floorplan", ".hopscope"}
MAX_BYTES = 1_000_000


def reader_for(rel: str) -> object | None:
    if iac_compose.FILE.search(rel):
        return iac_compose
    if iac_terraform.FILE.search(rel):
        return iac_terraform
    if iac_cfn.FILE.search(rel):
        return iac_cfn
    return None


def worth_parsing(mod: object, text: str) -> bool:
    """A cheap check before the YAML/JSON parser runs on a file that is probably something else."""
    if mod is iac_compose:
        return "services" in text
    if mod is iac_cfn:
        return "Resources" in text and "AWS::" in text
    return True


def _has_rows(part: dict) -> bool:
    return any(v for v in part.values() if isinstance(v, list)) or bool(part.get("s3", {}).get("count"))


def _collect(base: Path) -> tuple[list[tuple[str, object]], dict[str, list[str]]]:
    """Single files for the compose and CloudFormation readers; .tf files grouped by folder."""
    singles: list[tuple[str, object]] = []
    tf_dirs: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        for f in sorted(filenames):
            p = Path(dirpath) / f
            rel = p.relative_to(base).as_posix()
            mod = reader_for(rel)
            if mod is None or p.is_symlink():
                continue
            if mod is iac_terraform:
                tf_dirs.setdefault(posixpath.dirname(rel), []).append(rel)
            else:
                singles.append((rel, mod))
    return singles, tf_dirs


def _read_file(base: Path, rel: str) -> str | None:
    p = base / rel
    return None if p.stat().st_size > MAX_BYTES else p.read_text(errors="ignore")


def _rename_cluster(part: dict, old: str, new: str) -> None:
    """Move one file's services to another cluster name, with the routes and load balancer names that point at them."""
    for x in part.get("ecs", []):
        if x["cluster"] == old:
            x["cluster"] = new
    for lb in part.get("lb", []):
        if lb["n"] == old or lb["n"].startswith(old + " ("):
            lb["n"] = new + lb["n"][len(old):]
        for r in lb["routes"]:
            r["services"] = [new + s[len(old):] if s.startswith(old + "/") else s for s in r["services"]]


def _unique_clusters(inv: dict, part: dict, files: list[str]) -> None:
    """Two files can name the same cluster (two folders called deploy): the later one gets the file stem, then its folder path."""
    taken = {f'{x["cluster"]}/{x["n"]}' for x in inv["ecs"]}
    clusters = {x["cluster"] for x in inv["ecs"]}
    for old in sorted({x["cluster"] for x in part.get("ecs", []) if f'{x["cluster"]}/{x["n"]}' in taken}):
        rel = files[0]
        folder = posixpath.dirname(rel).replace("/", "-")
        options = [f"{old}-{Path(rel).stem}", *([f"{old} ({folder})"] if folder else []), *(f"{old}-{i}" for i in range(2, 1000))]
        mine = {x["n"] for x in part["ecs"] if x["cluster"] == old}
        new = next(o for o in options if o not in clusters and not any(f"{o}/{n}" in taken for n in mine))
        _rename_cluster(part, old, new)
        clusters.add(new)


def _unique_data_names(inv: dict, part: dict) -> None:
    """Same-named data stores from different files (two postgres, four redis): later ones show their cluster.
    `endpoint` keeps the bare service name, which is what environment values refer to."""
    for kind in ("rds", "cache", "search"):
        names = {x["n"] for x in inv[kind]}
        for x in part.get(kind, []):
            if x["n"] in names and x.get("cluster"):
                x["n"] = f'{x["n"]} ({x["cluster"]})'
            names.add(x["n"])


def scan(base: Path, region: str) -> dict:
    inv = iac_common.empty_inventory(region)
    singles, tf_dirs = _collect(base)

    def add(part: dict, files: list[str]) -> None:
        if _has_rows(part):
            _unique_clusters(inv, part, files)
            _unique_data_names(inv, part)
            iac_common.merge(inv, part)
            inv["sources"].extend(files)

    for rel, mod in singles:
        try:
            text = _read_file(base, rel)
            if text is not None and worth_parsing(mod, text):
                add(iac_compose.read(text, rel, base=base) if mod is iac_compose else mod.read(text, rel), [rel])  # type: ignore[attr-defined]
        except Exception as e:  # one unreadable file must not stop the scan
            inv["skipped"].append({"file": rel, "error": type(e).__name__})
    for files in (tf_dirs[k] for k in sorted(tf_dirs)):
        # A Terraform module spans every .tf file in its folder: services, task definitions and listeners refer to each other.
        texts, readable = [], []
        for rel in files:
            try:
                text = _read_file(base, rel)
            except Exception as e:
                inv["skipped"].append({"file": rel, "error": type(e).__name__})
                continue
            if text is not None:
                texts.append(text)
                readable.append(rel)
        try:
            if texts:
                add(iac_terraform.read("\n".join(texts), ", ".join(readable)), readable)
        except Exception as e:
            inv["skipped"].append({"file": readable[0], "error": type(e).__name__})
    inv["sources"].sort()
    return iac_common.mask_numbers(inv)  # a last pass: no account number leaves the scan, whatever field it hid in


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--code", required=True, help="code.json from code_scan.py (its base is scanned)")
    ap.add_argument("--region", default="design")
    ap.add_argument("--out", required=True)
    ap.add_argument("--optional", action="store_true", help="live mode: no infrastructure files is fine (writes nothing, removes a stale --out)")
    a = ap.parse_args(argv)
    base = Path(json.loads(Path(a.code).read_text())["base"])
    inv = scan(base, a.region)
    if not inv["sources"] and a.optional:
        Path(a.out).unlink(missing_ok=True)
        print(f"no infrastructure files under {base}: drift checks against files are skipped")
        return
    if not inv["sources"]:
        msg = f"no infrastructure files found under {base} (looked for docker compose, Terraform and CloudFormation/SAM)."
        if inv["skipped"]:
            msg += " Skipped: " + ", ".join(f'{x["file"]} ({x["error"]})' for x in inv["skipped"])
        raise SystemExit(msg)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(inv, indent=1))
    print(f"design inventory: {len(inv['ecs'])} services, {len(inv['lb'])} load balancers, {len(inv['sqs'])} queues, "
          f"{len(inv['rds'])} databases from {len(inv['sources'])} files -> {a.out}")


if __name__ == "__main__":
    main()
