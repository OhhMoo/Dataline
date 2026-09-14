"""The kimi subject driver: isolation machinery and transcript translation.

Kimi Code CLI 0.42.0 has no `--mcp-config` flag and no `--tools ""` equivalent
on the command line, but it has three mechanisms that compose into the same
revocation the claude `mcp` driver achieves:

  1. `KIMI_CODE_HOME=<per-rollout dir>` isolates ALL user-level state: the
     user-level `mcp.json`, installed plugins (which can declare MCP servers
     of their own), skills, hooks and session history simply do not exist in
     the isolated home. The home is populated with:
       - `mcp.json`        the gateway server, and nothing else. User-level
                           mcp.json is NOT trust-gated; project-level is, and
                           the subject's cwd has no project (no .git above
                           it), so the isolated user-level file is the only
                           MCP config that can load.
       - `config.toml`     a HARDLINK to the user's own file, so the subject
                           authenticates as the same account with the same
                           default model. No secret bytes are read or copied
                           by the harness - the OS hands the process the same
                           file it would have read anyway.
       - `credentials/`    a directory JUNCTION to the user's credential
                           store, same reasoning. (Verified: a probe rollout
                           left config.toml's mtime untouched; kimi -p does
                           not write back through the link.)
  2. `--agent-file <f>` where f declares `tools: [mcp__alkera__*]` and
     `subagents: []`. This is revocation, not a request: the allowlist shapes
     the tools SHOWN to the model and is enforced again before execution, and
     with subagents empty there is no Agent tool to delegate through. The
     file's body replaces the system prompt, so nothing inherited (base
     prompt, plugin sections, skills listing) is injected either.
  3. `--skills-dir <empty dir>` replaces skill discovery, plus
     KIMI_CODE_BUILTIN_PRODUCT_SKILLS=0 for the built-in ones. (Belt and
     braces: the Skill tool is not in the allowlist anyway.)

The evidence problem this module exists to solve: kimi's stream-json stdout
has NO session-init event, so blindness detector 1 (capability) would have
nothing to read. But kimi's own session wire log records
`llm.tools_snapshot` - the exact tool list sent to the model API - which is
the same class of evidence as claude's init event (the subject's client
reporting its own surface), arguably stronger. `translate_wire()` converts
the wire log into the claude-shaped transcript the detectors parse, and
synthesises the init event FROM that snapshot. It fabricates nothing: the
tools list is copied verbatim, and anything the translator does not
recognise is emitted as an `unparsed` line, which the evidence detector
turns into a hard `unobserved_episode` quarantine. Translation fails CLOSED.

Verified against kimi 0.42.0 before this driver was admitted (probe rollout,
isolated home, agent-file `tools: [mcp__probe__*]`): the wire log's
llm.tools_snapshot listed exactly one tool, `mcp__probe__ping`; agentsMdPaths
was empty; permission mode was auto; the MCP server connected with no trust
prompt.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

KIMI_BIN = (os.environ.get("ALKERA_KIMI_BIN")
            or str(Path.home() / ".kimi-code" / "bin" / "kimi.exe"))

# top-level wire events that carry no subject content. Anything NOT in this
# set and not explicitly translated becomes an `unparsed` transcript line,
# which quarantines the rollout - a kimi update that adds an event type makes
# rollouts unobserved, never silently clean.
_NEUTRAL_TOP = {
    "metadata", "runtime.set_binding", "profile.bind", "permission.set_mode",
    "plugin.session_start", "llm.request", "usage.record",
    "token_counting.measured", "token_counting.turn_recorded",
    "turn.ended", "prompt.completed", "mcp.tools_discovered",
    "llm.tools_snapshot", "turn.prompt",
}
_NEUTRAL_LOOP = {"step.begin", "step.end"}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in (content or []):
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text") or "")
    return "\n".join(p for p in parts if p)


def translate_wire(wire_path: Path, out_path: Path) -> dict:
    """Translate kimi's wire.jsonl into the claude-shaped transcript the
    detectors parse. Returns usage/final-text/tools metadata for the record.

    Fail-closed contract: unrecognised event types, unparseable lines and
    unrecognised content parts all become {"type": "unparsed"} lines, which
    blindness.detect() turns into a hard unobserved_episode quarantine.
    """
    raw_lines = Path(wire_path).read_text(
        encoding="utf-8", errors="replace").splitlines()

    events: list[dict] = []
    tools_seen: set[str] = set()
    usage_sum: dict[str, float] = {}
    final_text = ""
    turns = 0

    def neutral(ev: dict) -> None:
        events.append({"type": "system", "subtype": "kimi_event",
                       "kimi_type": ev.get("type")})

    def unparsed(raw: str) -> None:
        events.append({"type": "unparsed", "raw": raw[:400]})

    # pass 1: the tool surface, from the subject client's own snapshot(s)
    for line in raw_lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "llm.tools_snapshot":
            for t in ev.get("tools") or []:
                if isinstance(t, dict) and t.get("name"):
                    tools_seen.add(t["name"])

    # the init event the capability detector reads, synthesised from (only
    # from) the wire's own llm.tools_snapshot. No snapshot -> no init -> the
    # rollout quarantines as unverified_tool_surface. That is deliberate.
    if tools_seen:
        servers = sorted({n.split("__")[1] for n in tools_seen
                          if n.startswith("mcp__") and n.count("__") >= 2})
        events.append({
            "type": "system", "subtype": "init",
            "tools": sorted(tools_seen),
            "mcp_servers": [{"name": s, "status": "connected"} for s in servers],
            "model": "kimi",
            "source": "kimi wire llm.tools_snapshot (the tool list the "
                      "subject's client sent to the model API)"})

    # pass 2: the conversation
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            unparsed(line)
            continue
        t = ev.get("type")

        if t in ("llm.tools_snapshot", "mcp.tools_discovered"):
            neutral(ev)                       # already consumed in pass 1
        elif t == "turn.prompt":
            neutral(ev)                       # the harness's own prompt; the
            # first context.append_message below carries it as the first user
            # event, which the detectors skip by design (claude parity)
        elif t in _NEUTRAL_TOP:
            if t == "usage.record":
                for k, v in (ev.get("usage") or {}).items():
                    if isinstance(v, (int, float)):
                        usage_sum[k] = usage_sum.get(k, 0) + v
            if t == "turn.ended":
                turns += 1
            neutral(ev)
        elif t == "context.append_message":
            m = ev.get("message") or {}
            role = m.get("role")
            if role == "user":
                events.append({"type": "user",
                               "message": {"role": "user",
                                           "content": _text_of(m.get("content"))}})
            elif role == "assistant":
                blocks = []
                text = _text_of(m.get("content"))
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in (m.get("toolCalls") or []):
                    name = tc.get("name") or (tc.get("function") or {}).get("name")
                    args = tc.get("args", (tc.get("function") or {}).get("arguments"))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"_raw": args}
                    if not name:
                        unparsed(line)
                        continue
                    blocks.append({"type": "tool_use", "name": name,
                                   "input": args if isinstance(args, dict)
                                   else {"_raw": args}})
                events.append({"type": "assistant",
                               "message": {"role": "assistant", "content": blocks}})
            else:
                unparsed(line)
        elif t == "context.append_loop_event":
            e = ev.get("event") or {}
            lt = e.get("type")
            if lt in _NEUTRAL_LOOP:
                neutral(ev)
            elif lt == "content.part":
                part = e.get("part") or {}
                if part.get("type") == "text":
                    final_text = part.get("text") or final_text
                    events.append({"type": "assistant", "message": {
                        "role": "assistant",
                        "content": [{"type": "text",
                                     "text": part.get("text") or ""}]}})
                elif part.get("type") == "think":
                    events.append({"type": "assistant", "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking",
                                     "thinking": part.get("think") or ""}]}})
                else:
                    unparsed(line)
            elif lt == "tool.call":
                events.append({"type": "assistant", "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use",
                                 "name": e.get("name") or "?",
                                 "input": e.get("args")
                                 if isinstance(e.get("args"), dict) else {}}]}})
            elif lt == "tool.result":
                res = e.get("result")
                content = res.get("output") if isinstance(res, dict) else res
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                events.append({"type": "user", "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": content}]}})
            else:
                unparsed(line)
        else:
            unparsed(line)

    events.append({"type": "result", "subtype": "success",
                   "result": final_text, "num_turns": turns,
                   "usage": dict(usage_sum)})
    Path(out_path).write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8")
    return {"events": len(events), "final_text": final_text,
            "usage": dict(usage_sum), "tools": sorted(tools_seen),
            "turns": turns}


# ------------------------------------------------------------------ the home
def prepare_home(kimi_home: Path) -> None:
    """An isolated KIMI_CODE_HOME for one rollout.

    mcp.json is written per rollout by the caller (it names the rollout's own
    gateway config). What THIS does is the delicate part: give the subject
    the user's authentication without the harness ever reading a secret.
    `config.toml` is hardlinked and `credentials/` is junctioned - new names
    for the same files, created by the OS, copied by nobody. A kimi process
    running under the isolated home reads exactly what it would have read
    under the real one, and cannot see the user's mcp.json (none exists),
    plugins, skills or session history because those live under paths the
    isolated home does not share.
    """
    real = Path.home() / ".kimi-code"
    kimi_home.mkdir(parents=True, exist_ok=True)
    cfg_src, cfg_dst = real / "config.toml", kimi_home / "config.toml"
    cred_src, cred_dst = real / "credentials", kimi_home / "credentials"
    if not cfg_src.exists() or not cred_src.exists():
        from episode import HarnessError
        raise HarnessError(f"kimi home {real} is missing config.toml or "
                           "credentials; the subject cannot authenticate")
    if not cfg_dst.exists():
        os.link(cfg_src, cfg_dst)              # hardlink: same file, new name
    if not cred_dst.exists():
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(cred_dst),
                                str(cred_src)], capture_output=True, text=True)
            if r.returncode != 0:
                from episode import HarnessError
                raise HarnessError(f"could not junction kimi credentials: "
                                   f"{r.stdout} {r.stderr}")
        else:
            os.symlink(cred_src, cred_dst)


def write_mcp_config(path: Path, gateway_cfg: Path, py: Path, track_a: Path,
                     server_name: str) -> Path:
    """kimi's mcp.json has the same shape as claude's; the difference is WHERE
    it must live (the isolated home's user-level file) rather than a flag."""
    path.write_text(json.dumps({"mcpServers": {server_name: {
        "command": str(py),
        "args": ["-m", "alkera_gateway.cli", "serve", "--transport", "stdio",
                 "--config", str(gateway_cfg)],
        "env": {"PYTHONPATH": str(track_a), "PYTHONUTF8": "1"},
        "startupTimeoutMs": 60000,
    }}}, indent=2), encoding="utf-8")
    return path


def write_agent_file(path: Path, server_name: str) -> Path:
    """The revocation. `tools` shapes the tool list shown to the model and is
    enforced again before execution; `subagents: []` removes delegation; the
    body replaces the system prompt, so nothing inherited is injected."""
    path.write_text(f"""---
name: alkera-subject
description: blind measured subject for the alkera rollout harness
tools:
  - mcp__{server_name}__*
subagents: []
---

You are the subject in a measured episode. Work the ticket in the user's
prompt using only the tools available to you, then stop.
""", encoding="utf-8")
    return path
