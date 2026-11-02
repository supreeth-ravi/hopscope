"""Hopscope: build a live ops view of your code and your AWS account.

    hopscope.py all     --region ap-south-1 --repo ~/src/app [--repo …] [--profile p] [--overrides hopscope.json] [--workdir .hopscope]
    hopscope.py collect …same flags…        # gather data only
    hopscope.py build   [--workdir …]       # write the site from collected data
    hopscope.py review  [--workdir …]       # show links that need a human to confirm
    hopscope.py serve   [--workdir …] [--port 8765] [--engine claude|codex] [--model m]

Data goes to <workdir>/data, the site to <workdir>/site (index.html + JSON files).
"""
from __future__ import annotations

import argparse
import http.server
import json
import secrets
import shutil
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path

import local_agent

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
    console's data through the visitor's browser. `/api/ask` also needs a same-origin request carrying the
    token this run injected into the page, so another site open in the browser can't spend the person's
    Claude Code or Codex sign-in.
    """

    allowed_hosts: frozenset[str] = frozenset()
    engine: local_agent.Engine | None = None
    token: str = ""
    asking = threading.BoundedSemaphore(2)
    timeout = 30  # seconds a client may take to send its request; a slow sender can't pin a thread

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _page(self) -> None:
        """index.html with this run's token and engine, so the page can reach /api/ask."""
        local = {"token": self.token, "engine": self.engine.name} if self.engine else {}
        tag = f"<script>window.HOPSCOPE_LOCAL={json.dumps(local)};</script>"
        html = (Path(self.directory) / "index.html").read_text(encoding="utf-8").replace("</head>", tag + "</head>", 1)
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if not self._host_ok():
            self.send_error(403, "Hopscope only answers on localhost")
            return
        if self.path.split("?")[0] in ("/", "/index.html"):
            self._page()
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._host_ok():
            self.send_error(403, "Hopscope only answers on localhost")
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        same_origin = origin is None or origin == f"http://{self.headers.get('Host')}"
        if not self._host_ok() or not same_origin or not secrets.compare_digest(self.headers.get("X-Hopscope-Token", ""), self.token or " "):
            self._json(403, {"error": {"code": "forbidden", "message": "not allowed"}})
            return
        if self.path != "/api/ask":
            self._json(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})
            return
        if not self.engine:
            self._json(503, {"error": {"code": "no_engine", "message": "neither Claude Code nor Codex was found on this machine"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= local_agent.MAX_PROMPT * 4:
                raise ValueError("bad length")
            prompt = json.loads(self.rfile.read(length)).get("prompt")
        except (ValueError, AttributeError):
            self._json(400, {"error": {"code": "bad_request", "message": "expected JSON {\"prompt\": …}"}})
            return
        if not self.asking.acquire(blocking=False):
            self._json(429, {"error": {"code": "rate_limited", "message": "two questions are already running"}})
            return
        try:
            self._json(200, {"text": local_agent.ask(self.engine, prompt)})
        except local_agent.AgentError as e:
            self.log_message("ask failed (%s): %s", e.code, e.detail)
            status = {"bad_request": 400, "rate_limited": 429, "timeout": 504}.get(e.code, 502)
            self._json(status, {"error": {"code": e.code, "message": e.public}})
        finally:
            self.asking.release()


def serve(site: Path, port: int, engine: local_agent.Engine | None) -> None:
    LocalOnlyHandler.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    LocalOnlyHandler.engine = engine
    LocalOnlyHandler.token = secrets.token_hex(16)
    handler = partial(LocalOnlyHandler, directory=str(site))
    ask = f"Ask and Analyst use {engine.name} ({engine.path})" if engine else "Ask uses the built-in analysis (install Claude Code or Codex for more)"
    print(f"serving {site} at http://127.0.0.1:{port}/  (Ctrl-C to stop)\n{ask}")
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
    ap.add_argument("--engine", choices=local_agent.ENGINES, help="CLI that answers Ask in `serve` (default: the one that launched Hopscope, else whichever is installed)")
    ap.add_argument("--model", help="model for the Ask CLI")
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
        serve(site, a.port, local_agent.pick_engine(a.engine, a.model))


if __name__ == "__main__":
    main()
