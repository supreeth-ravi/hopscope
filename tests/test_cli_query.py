"""hopscope.py impact / query: readable answers on the command line."""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills" / "hopscope" / "scripts" / "hopscope.py"


def work(tmp: Path) -> Path:
    d = tmp / ".hopscope" / "data"
    d.mkdir(parents=True)
    for f in (ROOT / "docs" / "demo").glob("*.json"):
        shutil.copy(f, d / f.name)
    return tmp / ".hopscope"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60)


def test_impact_text(tmp_path: Path) -> None:
    p = run("impact", "app/identity.py", "--workdir", str(work(tmp_path)))
    assert p.returncode == 0, p.stderr
    out = p.stdout
    assert "shop-api/app/identity.py" in out and "POST /account/sign-in" in out
    assert out.index("POST /account/sign-in") < out.index("GET /health")
    assert "shop-api" in out


def test_query_text(tmp_path: Path) -> None:
    p = run("query", "POST /webhooks/payments", "--workdir", str(work(tmp_path)))
    assert p.returncode == 0, p.stderr
    assert "kestrel-public" in p.stdout and "SQS: SendMessage" in p.stdout
    assert "only if body is 'charge.refunded'" in p.stdout and "order-worker" in p.stdout
    assert "(service publishes to)" in p.stdout


def test_query_unknown_route_suggests(tmp_path: Path) -> None:
    p = run("query", "POST /orders", "--workdir", str(work(tmp_path)))
    assert p.returncode == 1 and "GET /orders" in p.stdout


def test_query_single_word(tmp_path: Path) -> None:
    w = str(work(tmp_path))
    p = run("query", "checkout", "--workdir", w)
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.startswith("POST /cart/checkout") and "best match for 'checkout'" in p.stdout
    p = run("query", "account", "--workdir", w)
    assert p.returncode == 1 and "pick one" in p.stdout and "POST /account/sign-up" in p.stdout


def test_needs_target_and_data(tmp_path: Path) -> None:
    assert run("impact", "--workdir", str(work(tmp_path))).returncode != 0
    p = run("impact", "x.py", "--workdir", str(tmp_path / "empty"))
    assert p.returncode != 0 and "hopscope.py design or all" in p.stderr
