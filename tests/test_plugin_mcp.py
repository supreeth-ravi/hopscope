"""The plugin registers the MCP server for Claude Code; Codex users add it by hand (see docs/codex.md)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_claude_plugin_registers_the_server() -> None:
    server = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["mcpServers"]["hopscope"]
    assert server["command"] == "python3"
    script = server["args"][0].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
    assert Path(script).is_file() and script.endswith("hopscope_mcp.py")
    assert server["args"][1:] == ["--workdir", "${CLAUDE_PROJECT_DIR}/.hopscope"]


def test_no_root_mcp_json_for_codex_to_misread() -> None:
    # Codex loads <plugin>/.mcp.json by default but does not expand ${CLAUDE_PLUGIN_ROOT} in MCP args,
    # so the Claude Code registration lives in .claude-plugin/plugin.json instead.
    assert not (ROOT / ".mcp.json").exists()
    assert "mcpServers" not in json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
