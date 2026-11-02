import http.client
import json
import sys
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import hopscope
import local_agent
from local_agent import AgentError, Engine

# Stand-ins for the two CLIs: they record their arguments and answer from stdin.
FAKE = """#!{py}
import sys, json, os
args = sys.argv[1:]
prompt = sys.stdin.read()
if "SLEEP" in prompt:
    import time; time.sleep(30)
if "FAIL" in prompt:
    sys.stderr.write("Error: not logged in. Please run /login")
    sys.exit(1)
answer = "answer from {name}: " + prompt + " | args=" + json.dumps(args) + " | cwd=" + os.getcwd()
if "{name}" == "codex":
    open(args[args.index("--output-last-message") + 1], "w").write(answer)
    print("codex progress noise")
else:
    print(answer)
"""


def fake_cli(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.write_text(FAKE.format(py=sys.executable, name=name))
    p.chmod(0o755)
    return str(p)


def test_pick_engine_prefers_the_agent_that_launched_it():
    both = {"claude": "/x/claude", "codex": "/x/codex"}.get
    assert local_agent.pick_engine(env={}, finder=both).name == "claude"
    assert local_agent.pick_engine(env={"CODEX_THREAD_ID": "t"}, finder=both).name == "codex"


def test_pick_engine_falls_back_to_whichever_is_installed():
    only_codex = {"codex": "/x/codex"}.get
    assert local_agent.pick_engine(env={}, finder=only_codex).name == "codex"
    assert local_agent.pick_engine(env={}, finder=lambda n: None) is None


def test_forced_engine_does_not_fall_back():
    only_codex = {"codex": "/x/codex"}.get
    assert local_agent.pick_engine("claude", finder=only_codex) is None
    with pytest.raises(ValueError):
        local_agent.pick_engine("gpt", finder=only_codex)


def test_commands_give_the_agent_no_tools_and_no_history():
    claude = local_agent.command(Engine("claude", "claude", "opus"), "/tmp/a")
    assert claude[claude.index("--tools") + 1] == ""
    assert claude[claude.index("--setting-sources") + 1] == "" and "--disable-slash-commands" in claude
    assert "--no-session-persistence" in claude and claude[-2:] == ["--model", "opus"]
    codex = local_agent.command(Engine("codex", "codex"), "/tmp/a")
    assert codex[codex.index("--sandbox") + 1] == "read-only"
    assert {"--ephemeral", "--ignore-user-config"} <= set(codex) and codex[-1] == "-"
    disabled = {codex[i + 1] for i, a in enumerate(codex) if a == "--disable"}
    assert {"shell_tool", "unified_exec", "plugins", "apps", "multi_agent"} <= disabled


def test_child_env_keeps_only_what_the_cli_needs():
    env = local_agent.child_env({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/bin",
                                 "HOME": "/h", "AWS_SECRET_ACCESS_KEY": "s", "AWS_PROFILE": "p", "DATABASE_URL": "postgres://u:p@h",
                                 "CODEX_HOME": "/c", "ANTHROPIC_API_KEY": "k"})
    assert env == {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/c", "ANTHROPIC_API_KEY": "k"}


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_ask_runs_the_cli_in_an_empty_temp_folder(tmp_path, name):
    text = local_agent.ask(Engine(name, fake_cli(tmp_path, name)), "what does /login do?")
    assert text.startswith(f"answer from {name}: what does /login do?")
    assert "hopscope-ask-" in text  # cwd was the throwaway folder, not the project
    assert "progress noise" not in text


def test_ask_reports_sign_in_failures(tmp_path):
    with pytest.raises(AgentError) as e:
        local_agent.ask(Engine("claude", fake_cli(tmp_path, "claude")), "FAIL")
    assert e.value.code == "not_logged_in"
    assert "/login" in e.value.detail and "/login" not in e.value.public


def test_ask_stops_a_cli_that_runs_too_long(tmp_path):
    with pytest.raises(AgentError) as e:
        local_agent.ask(Engine("claude", fake_cli(tmp_path, "claude")), "SLEEP", timeout=1)
    assert e.value.code == "timeout"


@pytest.mark.parametrize("prompt", ["", "   ", "x" * (local_agent.MAX_PROMPT + 1), None])
def test_ask_rejects_bad_prompts(prompt):
    with pytest.raises(AgentError) as e:
        local_agent.ask(Engine("claude", "/nonexistent"), prompt)
    assert e.value.code == "bad_request"


@pytest.fixture()
def server(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html><head></head><body>console</body></html>")
    hopscope.LocalOnlyHandler.engine = Engine("claude", fake_cli(tmp_path, "claude"))
    hopscope.LocalOnlyHandler.token = "t0ken"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(hopscope.LocalOnlyHandler, directory=str(site)))
    port = srv.server_address[1]
    hopscope.LocalOnlyHandler.allowed_hosts = frozenset({f"127.0.0.1:{port}"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


def post(port, body, headers):
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/ask", body=json.dumps(body), headers={"Content-Type": "application/json", **headers})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_page_carries_the_token_and_engine(server):
    c = http.client.HTTPConnection("127.0.0.1", server)
    c.request("GET", "/")
    html = c.getresponse().read().decode()
    assert 'window.HOPSCOPE_LOCAL={"token": "t0ken", "engine": "claude"}' in html


def test_ask_endpoint_answers_with_the_token(server):
    status, body = post(server, {"prompt": "hi"}, {"X-Hopscope-Token": "t0ken"})
    assert status == 200 and body["text"].startswith("answer from claude: hi")


@pytest.mark.parametrize("headers", [
    {},                                                                  # no token
    {"X-Hopscope-Token": "wrong"},
    {"X-Hopscope-Token": "t0ken", "Origin": "http://evil.example"},      # another site in the browser
])
def test_ask_endpoint_refuses_other_callers(server, headers):
    status, body = post(server, {"prompt": "hi"}, headers)
    assert status == 403 and "text" not in body


def test_ask_endpoint_maps_cli_failures(server):
    status, body = post(server, {"prompt": "FAIL"}, {"X-Hopscope-Token": "t0ken"})
    assert status == 502 and body["error"]["code"] == "not_logged_in"
    assert body["error"]["message"] == local_agent.PUBLIC_MESSAGE["not_logged_in"]  # CLI output stays in the log
