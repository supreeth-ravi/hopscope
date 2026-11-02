"""Hopscope: build a live ops view of your code and your AWS account.

    hopscope.py all     --region ap-south-1 --repo ~/src/app [--repo …] [--profile p] [--overrides hopscope.json] [--workdir .hopscope]
    hopscope.py collect …same flags…        # gather data only
    hopscope.py build   [--workdir …]       # write the site from collected data
    hopscope.py review  [--workdir …]       # show links that need a human to confirm
    hopscope.py serve   [--workdir …] [--port 8765]

Data goes to <workdir>/data, the site to <workdir>/site (index.html + JSON files).
"""
from __future__ import annotations

import argparse
import http.server
import json
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "assets" / "console.html"
SITE_FILES = ("code.json", "aws.json", "flows.json", "ts.json", "model.json")


def run(script: str, *args: str) -> None:
    cmd = [sys.executable, str(HERE / script), *args]
    res = subprocess.run(cmd, cwd=HERE)
    if res.returncode != 0:
        raise SystemExit(f"{script} failed (exit {res.returncode}); see the message above.")


def collect(a: argparse.Namespace, data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    prof = ["--profile", a.profile] if a.profile else []
    stg = ["--staging-pattern", a.staging_pattern] if a.staging_pattern else []
    ov = ["--overrides", str(Path(a.overrides).resolve())] if a.overrides else []
    repos = [x for r in a.repo for x in ("--repo", str(Path(r).expanduser().resolve()))]
    print("1/6 AWS inventory (read-only)…")
    run("aws_inventory.py", "--region", a.region, *prof, *stg, "--out", str(data / "aws.json"))
    print("2/6 code map…")
    run("code_scan.py", *repos, *(["--no-floorplan"] if a.no_floorplan else []), "--out", str(data / "code.json"))
    print("3/6 call flows…")
    run("call_flow.py", str(data / "code.json"), str(data / "flows.json"))
    print("4/6 linking code to infrastructure…")
    run("linker.py", "--aws", str(data / "aws.json"), "--code", str(data / "code.json"), *ov, "--out", str(data / "links.json"))
    print("5/6 hourly metrics and cost…")
    run("aws_timeseries.py", "--aws", str(data / "aws.json"), *prof, "--out", str(data / "ts.json"))
    print("6/6 health rules…")
    run("rules.py", "--data", str(data), *ov)


def build(data: Path, site: Path) -> None:
    missing = [f for f in SITE_FILES if not (data / f).exists() and f not in ("flows.json", "ts.json")]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)} in {data}: run `hopscope.py collect` first.")
    site.mkdir(parents=True, exist_ok=True)
    head = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<style>body{margin:0}[hidden]{display:none!important}</style></head><body>')
    (site / "index.html").write_text(head + TEMPLATE.read_text(encoding="utf-8") + "</body></html>", encoding="utf-8")
    (site / "page.html").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")  # bare body, for artifact publishing
    for f in SITE_FILES:
        if (data / f).exists():
            shutil.copy(data / f, site / f)
    print(f"site written to {site} (open with: hopscope.py serve)")


def review(data: Path) -> None:
    links = json.loads((data / "links.json").read_text())
    print("Low-confidence links (confirm or set them in hopscope.json under \"placement\"):")
    for r in links["review"]:
        print(f'  {r["ecs"]:55} -> {r["code"]:40} confidence {r["confidence"]}  ({"; ".join(r["evidence"]) or "name only"})')
    if links["unplaced"]:
        print("Running ECS services with no code found:")
        for u in links["unplaced"]:
            print(f"  {u}")
    if links.get("thirdParty"):
        print("Off-the-shelf images (public registry, not linked to code): " + ", ".join(links["thirdParty"]))
    print("Placed with good confidence:")
    for k, v in sorted(links["placement"].items(), key=lambda kv: kv[1]["code"]):
        if v["confidence"] >= 0.6:
            print(f'  {k:55} -> {v["code"]}')


class LocalOnlyHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the site only to requests addressed to this machine by name or loopback address.

    A malicious web page could otherwise point its own domain at 127.0.0.1 (DNS rebinding) and read the
    console's data through the visitor's browser.
    """

    allowed_hosts: frozenset[str] = frozenset()

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if not self._host_ok():
            self.send_error(403, "Hopscope only answers on localhost")
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._host_ok():
            self.send_error(403, "Hopscope only answers on localhost")
            return
        super().do_HEAD()


def serve(site: Path, port: int) -> None:
    LocalOnlyHandler.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    handler = partial(LocalOnlyHandler, directory=str(site))
    print(f"serving {site} at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["all", "collect", "build", "review", "serve"])
    ap.add_argument("--region")
    ap.add_argument("--profile")
    ap.add_argument("--repo", action="append", default=[])
    ap.add_argument("--overrides", help="hopscope.json with manual links, labels, thresholds")
    ap.add_argument("--staging-pattern")
    ap.add_argument("--no-floorplan", action="store_true")
    ap.add_argument("--workdir", default=".hopscope")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    work = Path(a.workdir).expanduser().resolve()
    data, site = work / "data", work / "site"
    if a.cmd in ("all", "collect"):
        if not a.region or not a.repo:
            raise SystemExit("collect needs --region and at least one --repo")
        collect(a, data)
    if a.cmd in ("all", "build"):
        build(data, site)
    if a.cmd == "review" or a.cmd == "all":
        review(data)
    if a.cmd == "serve":
        serve(site, a.port)


if __name__ == "__main__":
    main()
