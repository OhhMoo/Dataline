"""The cursor subject driver: isolation machinery and transcript translation.

Cursor CLI 2026.09.18 (`agent` / `cursor-agent`) runs a non-interactive turn
with `-p --output-format stream-json`, reads the prompt from stdin, and takes
its MCP servers from `<workspace>/.cursor/mcp.json`. Its stream is
claude-SHAPED (`system/init`, `user`, `assistant`, `result`) but not
claude-EQUAL, and the two differences are the whole reason this module exists:

  1. Tool calls are their own event type, `{"type": "tool_call", "subtype":
     "started" | "completed", "tool_call": {"<kind>ToolCall": {"args": ...,
     "result": ...}}}`, not `tool_use` / `tool_result` blocks. `translate()`
     rewrites them into the blocks blindness.py reads. The kind names the
     tool family (`mcpToolCall`, `shellToolCall`, `readToolCall`, ...); an
     MCP call names its server and tool in `serverIdentifier` / `toolName`.
  2. The init event carries NO tool list and no MCP-server list. The
     capability detector therefore has nothing to verify against, and this
     driver does NOT claim `tool_surface_revoked`. It is detection-only, like
     the `cli` driver, and the record says so.

What the harness does instead, measured 2026-09-20 (probe workspace, model
gpt-5.4-nano-none, one FastMCP server named `alkera`):

  * `HOME` and `USERPROFILE` point at a per-rollout directory. The CLI read
    `<that>/.cursor/cli-config.json` (its permission rules applied), found no
    user rules, skills, hooks or plugins (the operator's own `using-superpowers`
    hook, which HAD leaked into an un-isolated probe, was gone), and still
    authenticated (`apiKeySource: login`; the token is not under ~/.cursor).
  * `permissions.deny` names every built-in family the CLI's permission
    tokens can express (Shell, Read, Write, WebFetch - see DENY_PATTERNS for
    what they do not cover) and `--force` allows everything not explicitly
    denied - which is the one MCP server. The denied calls still APPEAR in
    the stream as the model's attempts, and the CLI answers each with its own
    `permissionDenied` / `error: Permission denied` payload without executing
    it. The translation copies that payload into the tool_result verbatim,
    prefixed with `POLICY_REFUSAL`, so the detectors can tell a refused
    attempt from a call that ran. A built-in the policy cannot refuse (glob,
    grep, ls, delete, web search, subagents) that DOES run is a hard
    violation and the episode quarantines.
  * Before an MCP tool's first use the CLI fetches its schema through a
    `getMcpToolsToolCall` - the same deferred loading claude's `ToolSearch`
    does. It reads the gateway's own schema, never the world, and is emitted
    under the bookkeeping name `ToolSearch`.

Fail-closed contract, as in kimi_subject.py: an event type or content block
this module does not recognise becomes an `unparsed` line, which the evidence
detector turns into a hard `unobserved_episode` quarantine. A CLI update that
adds an event type makes rollouts unobserved, never silently clean.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

#: `cursor-agent.cmd` is the wrapper the installer puts on disk; `agent.cmd`
#: beside it is an alias. Neither is on PATH by default on Windows.
_DEFAULT_BIN = (Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent"
                / "cursor-agent.cmd")
CURSOR_BIN = (os.environ.get("ALKERA_CURSOR_BIN")
              or shutil.which("cursor-agent")
              or shutil.which("agent")
              or (str(_DEFAULT_BIN) if _DEFAULT_BIN.exists() else None))

from blindness import POLICY_REFUSAL, RUNTIME_REFUSAL  # the markers blindness.detect reads

#: Built-in tool families the CLI can be told to refuse: the permission tokens
#: its docs define (cursor.com/docs/cli/reference/permissions - Shell, Read,
#: Write, WebFetch, Mcp). Measured 2026-09-20: Shell and Read calls under this
#: list came back `permissionDenied`; a `Glob(**)` entry did NOTHING, the
#: search ran and listed the workspace. So the client's search tools (glob,
#: grep, ls), delete, web search and subagents are NOT refusable by config;
#: they are caught by blindness.detect as hard violations when they run, which
#: quarantines the episode. That is the fail-closed half of this driver, and
#: the reason its guarantee is `detection_only`.
DENY_PATTERNS = ("Shell(**)", "Read(**)", "Write(**)", "WebFetch(*)")

#: `<kind>ToolCall` -> the name blindness.classify_tool reads. Families it
#: knows (FS_TOOLS, EXEC_TOOLS, WEB_TOOLS, DELEGATE_TOOLS) are hard violations
#: when they RAN; an unknown kind falls through as its own name, which
#: classifies as `unknown_tool` (hard) - the fail-closed default.
_KIND_NAMES = {
    "shellToolCall": "Shell",
    "readToolCall": "Read",
    "writeToolCall": "Write",
    "editToolCall": "Edit",
    "strReplaceToolCall": "Edit",
    "multiEditToolCall": "MultiEdit",
    "deleteToolCall": "Delete",
    "globToolCall": "Glob",
    "grepToolCall": "Grep",
    "lsToolCall": "LS",
    "webSearchToolCall": "WebSearch",
    "webFetchToolCall": "WebFetch",
    "fetchToolCall": "WebFetch",
    "taskToolCall": "Task",
    "todoToolCall": "TodoWrite",
    "todoWriteToolCall": "TodoWrite",
    # cursor-agent 2026.09.18 renamed the todo-list call; still the agent's own
    # scratchpad, never a world access (GT009 approved r1, 2026-09-21)
    "updateTodosToolCall": "TodoWrite",
    # reads the gateway's own tool schema; bookkeeping, never a world access
    "getMcpToolsToolCall": "ToolSearch",
    # cursor-agent wait/sleep bookkeeping primitive
    "awaitToolCall": "ToolSearch",
}

#: top-level event types that carry transport state and nothing of the
#: agent's: neutral, counted, never `unparsed`. Everything else unknown
#: still fails closed.
_TRANSPORT_EVENTS = frozenset({"connection", "retry"})


# ------------------------------------------------------------------ isolation
def prepare_home(home: Path) -> Path:
    """A per-rollout HOME/USERPROFILE holding one file: the permission policy.

    Nothing else exists there, so no rules, skills, hooks, plugins or session
    history can load. Authentication is not copied because the CLI does not
    keep it under ~/.cursor (measured: an isolated home authenticated as the
    same login without any file being carried over).
    """
    home = Path(home)
    cfg_dir = home / ".cursor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = {
        "version": 1,
        "permissions": {"allow": [], "deny": list(DENY_PATTERNS)},
        # allowlist + `--force` on the command line: everything not denied
        # runs without a prompt, and in `-p` mode there is nobody to prompt
        "approvalMode": "allowlist",
        # subagents inherit the parent's tools, MCP included, and their calls
        # are not documented to appear in the parent's stream. Off.
        "subagentModels": {"explore": "disabled"},
        "exploreSubagentModel": "default",
        "autoAcceptWebSearch": False,
        "attribution": {"attributeCommitsToAgent": False,
                        "attributePRsToAgent": False},
    }
    (cfg_dir / "cli-config.json").write_text(json.dumps(cfg, indent=2),
                                             encoding="utf-8")
    return home


def write_mcp_config(workspace: Path, gateway_cfg: Path, py: Path,
                     track_a: Path, server_name: str,
                     server_argv: list[str] | None = None) -> Path:
    """`<workspace>/.cursor/mcp.json` with the gateway and nothing else.

    Same shape as agent_under_test.write_mcp_config; `server_argv` is the
    container topology's `docker exec -i ... serve`.
    """
    if server_argv:
        entry = {"command": server_argv[0], "args": list(server_argv[1:])}
    else:
        entry = {
            "command": str(py),
            "args": ["-m", "alkera_gateway.cli", "serve", "--transport", "stdio",
                     "--config", str(gateway_cfg)],
            "env": {"PYTHONPATH": str(track_a), "PYTHONUTF8": "1"},
        }
    cfg_dir = Path(workspace) / ".cursor"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {server_name: entry}}, indent=2),
                    encoding="utf-8")
    return path


def subject_env(base: dict, home: Path) -> dict:
    env = {**base, "HOME": str(home), "USERPROFILE": str(home),
           "PYTHONUTF8": "1"}
    real_home = base.get("HOME")
    if real_home and "XDG_CONFIG_HOME" not in env:
        auth_src = Path(real_home) / ".config" / "cursor" / "auth.json"
        if auth_src.is_file():
            xdg_dir = Path(home).parent / "cursor_xdg"
            cursor_cfg_dir = xdg_dir / "cursor"
            cursor_cfg_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(auth_src, cursor_cfg_dir / "auth.json")
            cli_cfg = Path(home) / ".cursor" / "cli-config.json"
            if cli_cfg.is_file():
                shutil.copy2(cli_cfg, cursor_cfg_dir / "cli-config.json")
            env["XDG_CONFIG_HOME"] = str(xdg_dir)
    for k in ("CURSOR_PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        env.pop(k, None)
    return env


# ---------------------------------------------------------------- translation
def _mcp_text(content) -> str:
    """`success.content` of an mcpToolCall: [{"text": {"text": ...}}, ...]."""
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if isinstance(c, dict):
            t = c.get("text")
            if isinstance(t, dict) and "text" in t:
                parts.append(str(t["text"]))
            elif isinstance(t, str):
                parts.append(t)
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        else:
            parts.append(str(c))
    return "\n".join(parts)


def _result_block(kind: str, result, call_id: str) -> dict:
    """The claude-shaped tool_result for one completed cursor tool call.

    Refusals (`rejected`, `permissionDenied`, an `error` on a denied built-in)
    are marked `is_error` and prefixed with POLICY_REFUSAL, the CLI's own
    payload kept verbatim after it, so a detector can see both that the
    subject asked and that nothing ran.
    """
    if not isinstance(result, dict):
        return {"type": "tool_result", "tool_use_id": call_id,
                "content": json.dumps(result, ensure_ascii=False)}
    if "success" in result:
        ok = result["success"] or {}
        if kind == "mcpToolCall":
            content = _mcp_text(ok.get("content"))
            return {"type": "tool_result", "tool_use_id": call_id,
                    "content": content, "is_error": bool(ok.get("isError"))}
        content = ok.get("content")
        if not isinstance(content, str):
            content = json.dumps(ok, ensure_ascii=False)
        return {"type": "tool_result", "tool_use_id": call_id, "content": content}
    for key in result:
        if key == "rejected" or key.lower().endswith("permissiondenied"):
            return {"type": "tool_result", "tool_use_id": call_id, "is_error": True,
                    "content": f"{POLICY_REFUSAL}: {key} "
                               f"{json.dumps(result[key], ensure_ascii=False)}"}
    if "error" in result:
        err = result["error"]
        msg = (err.get("errorMessage") or err.get("error") or "") if isinstance(err, dict) else str(err)
        refused = ("Permission denied" in msg and kind != "mcpToolCall")
        not_found = (kind == "mcpToolCall" and "was not found" in msg)
        prefix = (f"{POLICY_REFUSAL}: error " if refused
                  else f"{RUNTIME_REFUSAL}: error " if not_found
                  else "error ")
        return {"type": "tool_result", "tool_use_id": call_id, "is_error": True,
                "content": prefix + json.dumps(err, ensure_ascii=False)}
    return {"type": "tool_result", "tool_use_id": call_id,
            "content": json.dumps(result, ensure_ascii=False)}


def translate(raw_path: Path, out_path: Path, mcp_server: str = "alkera") -> dict:
    """Cursor stream-json -> the claude-shaped transcript the detectors parse.

    Returns {"events", "final_text", "usage", "session_id", "tool_calls",
    "refused", "mcp_servers"} for the record. Fail-closed: anything not
    recognised is an `unparsed` line.
    """
    text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]

    events: list[dict] = []
    init_index: int | None = None
    servers_seen: list[str] = []
    final_text = ""
    usage: dict = {}
    session_id = None
    n_calls = 0
    n_refused = 0
    n_reconnects = 0
    # kind per call id, so the completed event can be rendered without
    # re-deriving it; the CLI omits `args` on a rejected completion
    kinds: dict[str, str] = {}
    # a thinking model streams `thinking/delta` text fragments and closes the
    # block with `thinking/completed` (918 deltas in GT009 r1's first Fable
    # episode). Buffered here and emitted as ONE claude-shaped thinking block.
    thinking: list[str] = []

    def unparsed(raw: str) -> None:
        events.append({"type": "unparsed", "raw": raw[:400]})

    def flush_thinking() -> None:
        if thinking:
            events.append({"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "".join(thinking)}]}})
            thinking.clear()

    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            unparsed(line)
            continue
        if not isinstance(ev, dict):
            unparsed(line)
            continue
        t = ev.get("type")

        if t == "system" and ev.get("subtype") == "init":
            session_id = ev.get("session_id") or session_id
            init_index = len(events)
            events.append({
                "type": "system", "subtype": "init",
                # the CLI reports no tool list: nothing to put here, and
                # nothing is invented. blindness.detect reads the flag.
                "tools": [], "tools_reported": False,
                "mcp_servers": [],
                "model": ev.get("model"),
                "session_id": session_id,
                "permissionMode": ev.get("permissionMode"),
                "source": "cursor stream-json system/init (carries no tool "
                          "list; mcp_servers filled from the servers the "
                          "client's own tool calls named)"})
        elif t == "thinking":
            sub = ev.get("subtype")
            if sub == "delta":
                thinking.append(str(ev.get("text") or ""))
            elif sub == "completed":
                flush_thinking()
            else:
                unparsed(line)
        elif t == "user":
            msg = ev.get("message") or {}
            blocks = msg.get("content")
            if isinstance(blocks, list) and all(
                    isinstance(b, dict) and b.get("type") == "text" for b in blocks):
                events.append({"type": "user", "message": {
                    "role": "user",
                    "content": "\n".join(b.get("text") or "" for b in blocks)}})
            elif isinstance(blocks, str):
                events.append({"type": "user", "message": {"role": "user",
                                                           "content": blocks}})
            else:
                unparsed(line)
        elif t == "assistant":
            msg = ev.get("message") or {}
            blocks = msg.get("content")
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            out_blocks = []
            bad = False
            for b in blocks or []:
                if not isinstance(b, dict):
                    bad = True
                    break
                bt = b.get("type")
                if bt == "text":
                    out_blocks.append({"type": "text", "text": b.get("text") or ""})
                    if b.get("text"):
                        final_text = b["text"]
                elif bt in ("thinking", "reasoning"):
                    out_blocks.append({"type": "thinking",
                                       "thinking": b.get("thinking")
                                       or b.get("text") or ""})
                else:
                    bad = True
                    break
            if bad:
                unparsed(line)
            else:
                events.append({"type": "assistant",
                               "message": {"role": "assistant",
                                           "content": out_blocks}})
        elif t == "tool_call":
            tc = ev.get("tool_call") or {}
            kind_keys = [k for k in tc if isinstance(k, str) and k.endswith("ToolCall")]
            call_id = str(ev.get("call_id") or tc.get("toolCallId") or "")
            if not kind_keys and isinstance(tc.get("function"), dict) and call_id:
                # the docs' fallback shape for "other tools":
                # {"function": {"name": ..., "arguments": "<json>"}}. Carried
                # under its own name so detector 2 judges it (a name it does
                # not know that RAN is hard) instead of the whole episode
                # quarantining as unparsed.
                fn = tc["function"]
                kind = f"function:{fn.get('name') or '?'}"
                try:
                    fn_args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    fn_args = {"_raw": fn.get("arguments")}
                body = {"args": fn_args if isinstance(fn_args, dict) else {"_raw": fn_args},
                        "result": tc.get("result")}
            elif len(kind_keys) != 1 or not call_id:
                unparsed(line)
                continue
            else:
                kind = kind_keys[0]
                body = tc.get(kind) or {}
            sub = ev.get("subtype")
            if sub == "started":
                kinds[call_id] = kind
                args = body.get("args") or {}
                if kind == "mcpToolCall":
                    server = str(args.get("serverIdentifier")
                                 or args.get("providerIdentifier") or "")
                    tool = str(args.get("toolName") or "")
                    if not server or not tool:
                        unparsed(line)
                        continue
                    if server not in servers_seen:
                        servers_seen.append(server)
                    name = f"mcp__{server}__{tool}"
                    inp = args.get("args")
                    inp = inp if isinstance(inp, dict) else {"_raw": inp}
                elif kind == "getMcpToolsToolCall":
                    name = _KIND_NAMES[kind]
                    inp = {"server": args.get("server"),
                           "toolName": args.get("toolName")}
                elif kind.startswith("function:"):
                    name = kind[len("function:"):]
                    inp = dict(args)
                else:
                    name = _KIND_NAMES.get(kind, kind)
                    inp = {k: v for k, v in args.items()
                           if k in ("command", "path", "pattern", "query", "url",
                                    "content", "old_string", "new_string",
                                    "workingDirectory", "description", "prompt")}
                    if not inp:
                        inp = {"_kind": kind}
                n_calls += 1
                events.append({"type": "assistant", "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": call_id,
                                 "name": name, "input": inp}]}})
            elif sub == "completed":
                k = kinds.get(call_id, kind)
                block = _result_block(k, body.get("result"), call_id)
                if block.get("is_error") and str(block.get("content", "")
                                                  ).startswith((POLICY_REFUSAL, RUNTIME_REFUSAL)):
                    n_refused += 1
                events.append({"type": "user", "message": {
                    "role": "user", "content": [block]}})
            else:
                unparsed(line)
        elif t == "result":
            flush_thinking()
            usage = {
                "duration_ms": ev.get("duration_ms"),
                "is_error": ev.get("is_error"),
                "subtype": ev.get("subtype"),
                "request_id": ev.get("request_id"),
            }
            if isinstance(ev.get("usage"), dict):
                usage["tokens"] = ev["usage"]
            if ev.get("result") and not final_text:
                final_text = str(ev.get("result"))
            events.append({"type": "result", "subtype": ev.get("subtype") or "success",
                           "result": ev.get("result") or "",
                           "is_error": bool(ev.get("is_error")),
                           "usage": usage.get("tokens") or {},
                           "num_turns": None})
        elif t in _TRANSPORT_EVENTS:
            # transport bookkeeping (cursor-agent 2026.09.18; GT004 authorized
            # r0, 2026-09-21: `connection/reconnecting`, `retry/starting`,
            # `retry/resuming` with a checkpoint turn count, then
            # `connection/reconnected`). No agent content and no tool call
            # rides on these, so the episode is still fully read; the
            # reconnect is counted so the record shows the session resumed
            # from a checkpoint rather than ran uninterrupted.
            if t == "connection" and ev.get("subtype") == "reconnected":
                n_reconnects += 1
        else:
            unparsed(line)

    flush_thinking()  # a block the stream never closed (killed episode)
    if init_index is not None:
        events[init_index]["mcp_servers"] = [
            {"name": s, "status": "connected"} for s in servers_seen]

    Path(out_path).write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8")
    return {"events": len(events), "final_text": final_text, "usage": usage,
            "session_id": session_id, "tool_calls": n_calls,
            "refused": n_refused, "mcp_servers": list(servers_seen),
            "reconnects": n_reconnects,
            "unparsed": sum(1 for e in events if e.get("type") == "unparsed")}
