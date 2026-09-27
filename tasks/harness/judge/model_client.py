"""The model behind the judge tools, when one is wanted.

Every judge in this repo was written against a `client` and none had one:
`clarity_gate.judge_brief(client=...)` has never been called with a model, and
GT001's `shadow_judge.py` scores with a keyword heuristic. This module is that
client. It satisfies the protocol `clarity_gate` already defines - a callable,
or an object with `judge(text)` - so no caller changes shape.

It runs the `claude` CLI headless (`claude --print`), because that is the one
credential a machine running this harness is guaranteed to have: the subject
itself needs it. No SDK import, so `lab_card.py`'s rule (no `anthropic` import
near a grader) cannot be tripped by accident.

Rules from docs/LLM_Judge_Research.md, enforced here rather than trusted to
callers:

  * k samples and a majority, with `unknown` when there is none. Temperature 0
    still flips ~14% of verdicts; one sample is not a verdict.
  * The judge's model family is recorded on every verdict, and
    `same_family_as` says when it matches the subject's. The memo asks for a
    different family; when none is available the record says so instead of
    pretending.
  * A judge with tools on the world beats a transcript-only judge (~90% vs
    60-70%). `tools` hands it Read/Grep/Glob/Bash, rooted at `cwd`. The caller
    is responsible for making `cwd` a COPY of anything the judge must not
    change (grader_audit.py does).

Nothing here is imported by reward.py, outcome.py or any grade.py
(test_judge_isolation.py checks the first two).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: A parent Claude Code session's variables. A judge started from inside one
#: must not inherit its IPC socket or session (same list as
#: container_episode.PARENT_SESSION_VARS, repeated so this package imports no
#: harness module).
_PARENT_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PROJECT_DIR",
)
TOOLS = "Read,Grep,Glob,Bash"
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def family(model: str) -> str:
    """`opus` / `claude-opus-5-5` -> `claude`. Coarse on purpose: the memo's
    rule is about model families, not versions."""
    m = (model or "").lower()
    for name in ("claude", "opus", "sonnet", "haiku", "fable"):
        if name in m:
            return "claude"
    for name, fam in (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
                      ("gemini", "google"), ("kimi", "moonshot")):
        if name in m:
            return fam
    return m or "unknown"


def extract_json(text: str) -> dict | None:
    """The last JSON object in a reply: a fenced block if there is one, else
    the widest `{...}` span that parses."""
    fenced = _FENCED_JSON.findall(text or "")
    for block in reversed(fenced):
        try:
            return json.loads(block)
        except ValueError:
            pass
    start = (text or "").find("{")
    end = (text or "").rfind("}")
    while start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            end = text.rfind("}", start, end)
    return None


def majority(labels: list[str], *, unknown: str = "unknown") -> tuple[str, float]:
    """(label, agreement). Strict majority of the votes cast, else `unknown`."""
    votes = [v for v in labels if v]
    if not votes:
        return unknown, 0.0
    label, count = Counter(votes).most_common(1)[0]
    share = count / len(votes)
    return (label if share > 0.5 else unknown), round(share, 3)


@dataclass
class ClaudeClient:
    """`claude --print` as a judge. `k` samples per call; `tools` on/off.

    `runner` is injectable so tests exercise everything but the process.
    """
    model: str = "opus"
    k: int = 3
    tools: bool = False
    cwd: Path | None = None
    timeout_s: int = 900
    claude: str | None = None
    runner: object = None
    calls: list = field(default_factory=list)

    def _argv(self) -> list[str]:
        claude = self.claude or shutil.which("claude") or "claude"
        argv = [claude, "--print", "--model", self.model,
                "--setting-sources", "", "--disable-slash-commands",
                "--no-session-persistence", "--strict-mcp-config",
                "--mcp-config", json.dumps({"mcpServers": {}})]
        if self.tools:
            argv += ["--tools", TOOLS, "--permission-mode", "bypassPermissions",
                     "--permission-prompts", "none"]
        else:
            argv += ["--tools", ""]
        return argv

    def _env(self) -> dict:
        return {k: v for k, v in os.environ.items() if k not in _PARENT_SESSION_VARS}

    def complete(self, prompt: str) -> str:
        """One sample."""
        if self.runner is not None:
            text = self.runner(prompt)
        else:
            proc = subprocess.run(self._argv(), input=prompt, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  cwd=str(self.cwd) if self.cwd else None,
                                  env=self._env(), timeout=self.timeout_s)
            if proc.returncode != 0:
                raise RuntimeError(f"claude exited {proc.returncode}: "
                                   f"{(proc.stderr or proc.stdout)[-600:]}")
            text = proc.stdout
        self.calls.append({"chars_in": len(prompt), "chars_out": len(text or "")})
        return text

    def samples(self, prompt: str) -> list[str]:
        """k independent samples. A sample that errors is recorded as "" and
        counts as no vote, so a flaky call lowers agreement instead of voting."""
        out = []
        for _ in range(max(1, self.k)):
            try:
                out.append(self.complete(prompt))
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                self.calls.append({"error": str(exc)[:300]})
                out.append("")
        return out

    # The clarity_gate protocol: `judge(text)` -> the reply text. One sample:
    # the gate takes its own consensus across judges.
    def judge(self, text: str) -> str:
        return self.complete(text)

    def __call__(self, text: str) -> str:
        return self.complete(text)

    def provenance(self, subject_model: str | None = None) -> dict:
        mine = family(self.model)
        return {"judge_model": self.model, "judge_family": mine, "k": self.k,
                "tools": TOOLS if self.tools else "",
                "same_family_as_subject": (None if subject_model is None
                                           else mine == family(subject_model))}
