import http.client
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import code_scan
import hopscope
from awsutil import safe_value


@pytest.mark.parametrize("value, expected", [
    ("https://sqs.eu-west-1.amazonaws.com/123456789012/orders.fifo", "https://sqs.eu-west-1.amazonaws.com/123456789012/orders.fifo"),
    ("postgres://app:s3cret@db1.abc123.eu-west-1.rds.amazonaws.com:5432/app", "db1.abc123.eu-west-1.rds.amazonaws.com"),
    ("redis://:token@cache.xyz.ng.0001.euw1.cache.amazonaws.com:6379/0", "cache.xyz.ng.0001.euw1.cache.amazonaws.com"),
    ("db1.abc123.eu-west-1.rds.amazonaws.com", "db1.abc123.eu-west-1.rds.amazonaws.com"),
    ("https://sqs.eu-west-1.amazonaws.com/123456789012/q?X-Amz-Signature=abc", None),
    ("sk_live_51Habc", None),
    ("https://evil.example/?u=db.rds.amazonaws.com", None),
])
def test_safe_value_keeps_only_endpoints(value: str, expected: str | None) -> None:
    assert safe_value(value) == expected


def test_scanner_drops_unknown_methods(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "s.js").write_text("router.get('/ok', h);\n")
    cm = code_scan.scan([tmp_path])
    assert {r["m"] for r in cm.routes} <= code_scan.METHODS
    assert [r["p"] for r in cm.routes] == ["/ok"]


def test_local_server_rejects_foreign_host(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("ok")
    hopscope.LocalOnlyHandler.allowed_hosts = frozenset()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(hopscope.LocalOnlyHandler, directory=str(tmp_path)))
    port = srv.server_address[1]
    hopscope.LocalOnlyHandler.allowed_hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def status(host: str) -> int:
            c = http.client.HTTPConnection("127.0.0.1", port)
            c.request("GET", "/index.html", headers={"Host": host})
            return c.getresponse().status
        assert status(f"localhost:{port}") == 200
        assert status(f"attacker.example:{port}") == 403
    finally:
        srv.shutdown()
