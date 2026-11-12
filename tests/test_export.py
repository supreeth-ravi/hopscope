"""hopscope_export: Mermaid and draw.io output from the demo data, with hostile labels escaped."""
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import hopscope_export as ex
import hopscope_query as q

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs" / "demo"


@pytest.fixture(scope="module")
def data() -> dict:
    return q.load(DEMO)


def test_graph_has_columns_and_edges(data: dict) -> None:
    g = ex.architecture(data)
    by_label = {n["label"]: n for n in g["nodes"]}
    assert by_label["kestrel-public"]["column"] == 0
    assert by_label["shop-api"]["column"] == 1
    assert by_label["kestrel-orders.fifo"]["column"] == 2 and by_label["kestrel-db"]["column"] == 2
    assert by_label["order-worker"]["column"] == 3
    ids = {n["id"]: n["label"] for n in g["nodes"]}
    pairs = {(ids[e["from"]], ids[e["to"]]) for e in g["edges"]}
    assert ("kestrel-public", "shop-api") in pairs
    assert ("shop-api", "kestrel-orders.fifo") in pairs
    assert ("kestrel-orders.fifo", "order-worker") in pairs
    assert ("shop-api", "kestrel-db") in pairs
    assert g == ex.architecture(data)  # deterministic


def test_mermaid_flowchart(data: dict) -> None:
    text = ex.mermaid_architecture(data)
    lines = text.splitlines()
    assert lines[0] == "flowchart LR"
    g = ex.architecture(data)
    assert sum(1 for ln in lines if re.match(r'^\s+n\d+\["', ln)) == len(g["nodes"])
    assert sum(1 for ln in lines if "-->" in ln) == len(g["edges"])
    assert text.count("subgraph") == text.count("\n  end")


def test_mermaid_sequence(data: dict) -> None:
    text = ex.mermaid_trace(data, "POST /webhooks/payments")
    lines = text.splitlines()
    assert lines[0] == "sequenceDiagram"
    assert any(ln.strip().startswith("participant ") for ln in lines)
    assert any(ln.strip().startswith("opt ") and "charge.refunded" in ln for ln in lines)
    assert sum(1 for ln in lines if ln.strip().startswith(("opt ", "alt ", "loop "))) == sum(1 for ln in lines if ln.strip() == "end")
    assert "SendMessage" in text and "order-worker" in text
    aliases = [ln.split(" as ", 1)[1] for ln in lines if ln.strip().startswith("participant ")]
    assert not any(re.fullmatch(r"p\d+", a) for a in aliases)  # self-calls don't become participants
    with pytest.raises(ValueError):
        ex.mermaid_trace(data, "DELETE /nothing")


def test_drawio_parses_with_one_cell_per_node_and_edge(data: dict) -> None:
    xml = ex.drawio_architecture(data)
    root = ET.fromstring(xml)
    model = root.find("./diagram/mxGraphModel")
    assert model is not None
    cells = model.findall("./root/mxCell")
    g = ex.architecture(data)
    assert len([c for c in cells if c.get("vertex") == "1"]) == len(g["nodes"])
    assert len([c for c in cells if c.get("edge") == "1"]) == len(g["edges"])
    styles = [c.get("style") or "" for c in cells if c.get("vertex") == "1"]
    assert all("mxgraph.aws4." in s for s in styles)
    assert not any("html=1" in s for s in styles)  # labels are plain text, never HTML


def hostile(tmp: Path) -> dict:
    for f in DEMO.glob("*.json"):
        shutil.copy(f, tmp / f.name)
    bad = 'evil"]\n--> x{{<img src=x onerror=alert(1)>}} #; end & \x01'
    aws = json.loads((tmp / "aws.json").read_text())
    aws["lb"][0]["n"] = bad
    aws["lb"][0]["routes"][0]["when"] = bad
    (tmp / "aws.json").write_text(json.dumps(aws))
    m = json.loads((tmp / "model.json").read_text())
    m["queueWorkers"]["kestrel-orders.fifo"] = [bad, "job"]
    (tmp / "model.json").write_text(json.dumps(m))
    code = json.loads((tmp / "code.json").read_text())
    code["routes"][9]["p"] = "/webhooks/" + bad.replace("\n", "")
    (tmp / "code.json").write_text(json.dumps(code))
    return q.load(tmp)


def test_labels_are_escaped(tmp_path: Path) -> None:
    d = hostile(tmp_path)
    flow = ex.mermaid_architecture(d)
    for ln in flow.splitlines()[1:]:
        assert "<" not in ln and "\x01" not in ln
        m = re.match(r'^\s+n\d+\["(.*)"\]$', ln)
        if m:
            assert '"' not in m.group(1)
    assert flow.count("subgraph") == flow.count("\n  end")
    seq = ex.mermaid_trace(d, 9)
    assert "<img" not in seq and "\x01" not in seq
    root = ET.fromstring(ex.drawio_architecture(d))  # still well-formed XML
    assert any("onerror" in (c.get("value") or "") for c in root.iter("mxCell"))  # kept as text, escaped by XML


def test_cli_export(tmp_path: Path) -> None:
    work = tmp_path / ".hopscope"
    (work / "data").mkdir(parents=True)
    for f in DEMO.glob("*.json"):
        shutil.copy(f, work / "data" / f.name)
    cli = ROOT / "skills" / "hopscope" / "scripts" / "hopscope.py"
    for fmt, extra, name in (("mermaid", [], "a.mmd"), ("mermaid", ["--route", "POST /account/sign-in"], "t.mmd"), ("drawio", [], "a.drawio")):
        p = subprocess.run([sys.executable, str(cli), "export", "--format", fmt, *extra, "--out", str(tmp_path / name), "--workdir", str(work)],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr
    assert (tmp_path / "a.mmd").read_text().startswith("flowchart LR")
    assert (tmp_path / "t.mmd").read_text().startswith("sequenceDiagram")
    ET.parse(tmp_path / "a.drawio")
    p = subprocess.run([sys.executable, str(cli), "export", "--format", "drawio", "--route", "GET /health", "--out", str(tmp_path / "x"), "--workdir", str(work)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode != 0
