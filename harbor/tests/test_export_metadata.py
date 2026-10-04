"""The adapter's own published files: adapter_metadata.json, run_alkera.yaml,
the CLI, and the ignore rule for generated output."""
from __future__ import annotations

import json
import subprocess
import sys

import test_export_support as support
from alkera_harbor.constants import REPO
from alkera_harbor.main import build_parser

HARBOR = REPO / "harbor"


def test_adapter_metadata_follows_harbors_template():
    data = json.loads((HARBOR / "adapter_metadata.json").read_text())
    assert isinstance(data, list) and len(data) == 1
    entry = data[0]
    assert set(entry) == {"adapter_name", "adapter_builders", "original_benchmark",
                          "harbor_adapter"}
    (original,) = entry["original_benchmark"]
    assert set(original) == {"split", "size", "harness", "supported_agents",
                             "adaptable", "notes"}
    (adapted,) = entry["harbor_adapter"]
    assert set(adapted) == {"split", "adapted_benchmark_size", "parity_benchmark_size",
                            "parity_sampling_rate", "registry_benchmark_size",
                            "added_agents", "parity_matching_agents",
                            "parity_unmatching_agents", "parity_costs", "notes"}
    assert original["size"] == adapted["adapted_benchmark_size"] == 28


def test_run_config_is_a_valid_harbor_job_config():
    script = (
        "import json, sys, warnings, yaml\n"
        "warnings.simplefilter('error', DeprecationWarning)\n"
        "from harbor.models.job.config import JobConfig\n"
        "cfg = JobConfig.model_validate(yaml.safe_load(sys.stdin.read()))\n"
        "a = cfg.agents[0]\n"
        "print(json.dumps({'agent': a.name, 'model': a.model_name,\n"
        "  'hosts': a.extra_allowed_hosts, 'path': str(cfg.datasets[0].path)}))\n")
    r = subprocess.run([support.harbor_python(), "-c", script],
                       input=(HARBOR / "run_alkera.yaml").read_text(),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {"agent": "claude-code", "model": "anthropic/claude-opus-5-5",
                   "hosts": ["api.anthropic.com"], "path": "harbor/dist/alkera-de"}


def test_cli_flags():
    parser = build_parser()
    args = parser.parse_args(["--output-dir", "x", "--task-ids",
                              "GT001-aov-halving:decision", "GT004-doubling-window",
                              "--limit", "3", "--overwrite", "--reward", "training",
                              "--agent-network", "public", "--validate"])
    assert args.task_ids == ["GT001-aov-halving:decision", "GT004-doubling-window"]
    assert (args.limit, args.overwrite, args.reward, args.agent_network,
            args.validate, args.dataset_name) == (3, True, "training", "public",
                                                  True, "alkera/de-bench")


def test_cli_list_runs_without_output_dir():
    r = subprocess.run([sys.executable, str(HARBOR / "export.py"), "--list",
                        "--task-ids", "GT009"], capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, r.stderr
    assert "3 exportable arm(s)" in r.stdout
    assert "alkera/gt009-settlement-cutoff__live-consumer" in r.stdout


def test_cli_requires_output_dir_and_rejects_unknown_ids():
    r = subprocess.run([sys.executable, str(HARBOR / "export.py")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 2 and "--output-dir is required" in r.stderr
    r = subprocess.run([sys.executable, str(HARBOR / "export.py"), "--list",
                        "--task-ids", "GT999"], capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 1 and "matches nothing" in r.stderr


def test_generated_output_is_gitignored():
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q",
                        "harbor/dist/alkera-de/task.toml"])
    assert r.returncode == 0
