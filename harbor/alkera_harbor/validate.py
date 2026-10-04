"""`--validate`: load every generated task with a REAL Harbor install.

Harbor needs Python >= 3.12 and is not a dependency of the generator, so the
check runs in a subprocess with the interpreter of a Harbor install
(`--harbor-python`, else $ALKERA_HARBOR_PYTHON, else the interpreter behind a
`harbor` executable on PATH). Per task it runs:

  * `TaskConfig.model_validate_toml(task.toml)` and a STRICT key check: Harbor
    silently ignores unknown task.toml keys (pydantic extra="ignore"), so every
    key we wrote must survive `model_dump(exclude_unset=True)`; free-form
    tables (`metadata`, `*.env`) are exempt;
  * `Task(path)` (what `harbor run` constructs) and `Task.is_valid_dir`;
  * `strip_canary` parity: sha256 of `Task.instruction` (what the agent is
    sent) is returned so the caller can compare it with the harness prompt;
  * `Packager.compute_content_hash`, which must equal our digest.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ENV_VAR = "ALKERA_HARBOR_PYTHON"

_SCRIPT = r'''
import hashlib, json, sys, tomllib
from importlib.metadata import version
from pathlib import Path
from harbor.models.task.config import TaskConfig
from harbor.models.task.task import Task
from harbor.publisher.packager import Packager

FREE_FORM = ("metadata",)

def unknown(raw, dumped, path=""):
    out = []
    if isinstance(raw, dict):
        if not isinstance(dumped, dict):
            return [path or "<root>"]
        for k, v in raw.items():
            p = f"{path}.{k}" if path else k
            if k not in dumped:
                out.append(p)
            elif p in FREE_FORM or k == "env":
                continue
            else:
                out += unknown(v, dumped[k], p)
    elif isinstance(raw, list) and isinstance(dumped, list):
        for i, (a, b) in enumerate(zip(raw, dumped)):
            out += unknown(a, b, f"{path}[{i}]")
    return out

request = json.loads(sys.stdin.read())
result = {"harbor_version": version("harbor"), "tasks": {}}
for d in request["tasks"]:
    r = {"ok": False}
    try:
        p = Path(d)
        text = (p / "task.toml").read_text()
        cfg = TaskConfig.model_validate_toml(text)
        raw = tomllib.loads(text)
        dumped = cfg.model_dump(mode="json", exclude_unset=True)
        r["unknown_keys"] = unknown(raw, dumped)
        task = Task(p)
        r["name"] = task.name
        r["valid_dir"] = Task.is_valid_dir(p)
        r["instruction_sha256"] = hashlib.sha256(task.instruction.encode()).hexdigest()
        h, files = Packager.compute_content_hash(p)
        r["digest"] = "sha256:" + h
        r["n_files"] = len(files)
        r["ok"] = True
    except Exception as exc:
        r["error"] = f"{type(exc).__name__}: {exc}"
    result["tasks"][d] = r
print(json.dumps(result))
'''


_DATASET_SCRIPT = r'''
import json, sys
from harbor.models.dataset.manifest import DatasetManifest
m = DatasetManifest.from_toml_file(sys.stdin.read().strip())
print(json.dumps({"name": m.dataset.name, "version": m.dataset.version,
                  "tasks": [{"name": t.name, "digest": t.digest} for t in m.tasks],
                  "content_hash": m.compute_content_hash()}))
'''


class ValidationError(Exception):
    pass


def find_harbor_python(explicit: str | None = None) -> str:
    """The interpreter of a Harbor install, or ValidationError."""
    candidates = [explicit, os.environ.get(ENV_VAR)]
    harbor = shutil.which("harbor")
    if harbor:
        try:
            first = Path(harbor).read_text(errors="replace").split("\n", 1)[0]
            if first.startswith("#!"):
                candidates.append(first[2:].strip().split()[0])
        except OSError:
            pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            r = subprocess.run([candidate, "-c", "import harbor"], capture_output=True)
            if r.returncode == 0:
                return candidate
    raise ValidationError(
        f"no Python with harbor importable: pass --harbor-python or set {ENV_VAR} "
        f"(harbor >= 0.23 needs Python >= 3.12)")


def run_harbor(task_dirs: list[Path], harbor_python: str | None = None) -> dict:
    """Raw per-directory results from the Harbor subprocess."""
    python = find_harbor_python(harbor_python)
    request = {"tasks": [str(Path(d).resolve()) for d in task_dirs]}
    r = subprocess.run([python, "-c", _SCRIPT], input=json.dumps(request),
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise ValidationError(f"harbor validation subprocess failed: {r.stderr[-2000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def validate_tasks(task_dirs: list[Path], *, harbor_python: str | None = None,
                   instruction_sha256: dict[str, str] | None = None) -> dict[str, dict]:
    """{task dir name: verdict}. A verdict is `ok` only when Harbor loaded the
    task, no unknown key was dropped and the name matches the directory;
    `problems` lists anything else. `digest` is Harbor's, for the caller to
    compare with its own."""
    raw = run_harbor(task_dirs, harbor_python)
    out: dict[str, dict] = {}
    for d in task_dirs:
        verdict = dict(raw["tasks"].get(str(Path(d).resolve()), {}))
        verdict["harbor_version"] = raw.get("harbor_version")
        problems = []
        if verdict.get("ok"):
            if verdict.get("unknown_keys"):
                problems.append(f"task.toml keys Harbor ignores: {verdict['unknown_keys']}")
            if not verdict.get("valid_dir"):
                problems.append("Task.is_valid_dir is False")
            if verdict.get("name", "").split("/", 1)[-1] != Path(d).name:
                problems.append(f"[task].name {verdict.get('name')!r} does not match "
                                f"the directory")
            expected = (instruction_sha256 or {}).get(Path(d).name)
            if expected is not None and verdict.get("instruction_sha256") != expected:
                problems.append("the instruction Harbor sends the agent (after "
                                "strip_canary) differs from the harness prompt")
        verdict["problems"] = problems
        out[Path(d).name] = verdict
    return out


def validate_dataset(dataset_toml: Path, *, harbor_python: str | None = None) -> dict:
    """Parse dataset.toml with Harbor's DatasetManifest (names and digest
    formats are validated there); returns what Harbor read."""
    python = find_harbor_python(harbor_python)
    r = subprocess.run([python, "-c", _DATASET_SCRIPT],
                       input=str(Path(dataset_toml).resolve()),
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise ValidationError(f"harbor rejected {dataset_toml}: {r.stderr[-2000:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])
