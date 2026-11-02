"""Answer the console's Analyst questions on this machine through the Claude Code or Codex CLI.

The console's prompts already carry the trace and metrics they need, so the agent gets as little as
each CLI allows: Claude Code with its tools, settings, hooks, skills and MCP servers off; Codex with its
shell, plugins, apps, browser and sub-agents off, inside its read-only sandbox, ignoring the user's
config. Both run in an empty temporary folder with a trimmed environment (no AWS credentials), and
neither keeps the conversation in its history. Answers use the person's own sign-in.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ENGINES = ("claude", "codex")
MAX_PROMPT = 60_000          # characters; the console's largest prompt (a long trace) is well under this
TIMEOUT_S = 180
# The CLIs get only what they need to find themselves, sign in and reach their service; AWS credentials
# and other secrets in the server's environment stay behind.
_KEEP_VARS = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|TMPDIR|TERM|LANG|LC_\w+|TZ|XDG_\w+"
                        r"|(HTTPS?|ALL|NO)_PROXY|(https?|all|no)_proxy|SSL_CERT_(FILE|DIR)|NODE_EXTRA_CA_CERTS"
                        r"|ANTHROPIC_\w+|CLAUDE_CONFIG_DIR|CLAUDE_CODE_USE_\w+|CODEX_HOME|OPENAI_\w+)$")
# Codex features that would let it act beyond answering: shell commands, apps, plugins, browsing, sub-agents.
_CODEX_OFF = ("shell_tool", "unified_exec", "apps", "plugins", "browser_use", "browser_use_external",
              "in_app_browser", "computer_use", "multi_agent")
# What the browser sees for each failure; the CLI's own output stays in the server log.
PUBLIC_MESSAGE = {
    "not_logged_in": "the CLI is not signed in",
    "rate_limited": "the CLI hit a usage or rate limit",
    "timeout": "the CLI took too long to answer",
    "bad_request": "the question was empty or too long",
    "cli_error": "the CLI failed; see the server log",
}


@dataclass(frozen=True)
class Engine:
    name: str
    path: str
    model: str | None = None


class AgentError(Exception):
    """`code` and `public` are safe to show in the browser; `detail` is for the server log only."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.public = PUBLIC_MESSAGE.get(code, PUBLIC_MESSAGE["cli_error"])


def find_cli(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / name  # Claude Code's native installer puts it here
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def running_under_codex(env: Mapping[str, str]) -> bool:
    return bool(env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"))


def pick_engine(want: str | None = None, model: str | None = None, env: Mapping[str, str] = os.environ,
                finder=find_cli) -> Engine | None:
    """The forced engine, else the agent that launched us, else whichever CLI is installed."""
    if want:
        if want not in ENGINES:
            raise ValueError(f"--engine must be one of {', '.join(ENGINES)}")
        path = finder(want)
        return Engine(want, path, model) if path else None
    first = "codex" if running_under_codex(env) else "claude"
    for name in (first, *(e for e in ENGINES if e != first)):
        path = finder(name)
        if path:
            return Engine(name, path, model)
    return None


def command(engine: Engine, answer_file: str) -> list[str]:
    if engine.name == "claude":
        cmd = [engine.path, "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config",
               "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence"]
        return cmd + (["--model", engine.model] if engine.model else [])
    cmd = [engine.path, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
           "--ignore-user-config", "--ignore-rules", "--color", "never", "--output-last-message", answer_file]
    cmd += [x for f in _CODEX_OFF for x in ("--disable", f)]
    return cmd + (["--model", engine.model] if engine.model else []) + ["-"]


def child_env(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    return {k: v for k, v in env.items() if _KEEP_VARS.match(k)}


def failure_code(message: str) -> str:
    if re.search(r"not logged in|logged out|/login|log ?in again|sign in|authenticat|unauthori[sz]ed|\b401\b", message, re.I):
        return "not_logged_in"
    if re.search(r"usage limit|rate limit|too many requests|\b429\b", message, re.I):
        return "rate_limited"
    return "cli_error"


def ask(engine: Engine, prompt: str, timeout: float = TIMEOUT_S) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise AgentError("bad_request", "empty prompt")
    if len(prompt) > MAX_PROMPT:
        raise AgentError("bad_request", f"prompt longer than {MAX_PROMPT} characters")
    with tempfile.TemporaryDirectory(prefix="hopscope-ask-") as cwd:
        answer_file = str(Path(cwd) / "answer.txt")
        returncode, stdout, stderr = _run(command(engine, answer_file), prompt, cwd, timeout, engine.name)
        if returncode != 0:
            detail = (stderr or stdout).strip()[-500:] or f"{engine.name} exited with code {returncode}"
            raise AgentError(failure_code(detail), detail)
        out = Path(answer_file)
        text = out.read_text(encoding="utf-8") if engine.name == "codex" and out.exists() else stdout
    if not text.strip():
        raise AgentError("cli_error", f"{engine.name} returned an empty answer")
    return text.strip()


def _run(cmd: list[str], prompt: str, cwd: str, timeout: float, name: str) -> tuple[int, str, str]:
    """Runs the CLI in its own process group, so a timeout stops it and anything it started."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=cwd, env=child_env(), start_new_session=sys.platform != "win32")
    except OSError as e:
        raise AgentError("cli_error", f"could not start {name}: {e}") from e
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill_group(proc)
        raise AgentError("timeout", f"{name} took longer than {int(timeout)} s") from e
    return proc.returncode, stdout, stderr


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # a grandchild still holds the pipes; the group was killed, so don't block the request on it
