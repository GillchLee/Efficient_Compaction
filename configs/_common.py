"""Shared by the config generators: the benchmark axis (which tasks, default budget, continue mode)
and small helpers. Benchmarks are presets in configs/benchmarks.yaml; a preset never touches the
policy or the harness."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_BENCHMARK = "lhtb2"      # what --tasks-dir defaulted to before presets existed
DEFAULT_HOURS = 3.0


def load_benchmarks() -> dict:
    return yaml.safe_load((HERE / "benchmarks.yaml").read_text()) or {}


def add_benchmark_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--benchmark", choices=sorted(load_benchmarks()),
                   help=f"preset from configs/benchmarks.yaml (default {DEFAULT_BENCHMARK} when no tasks source is given)")
    g.add_argument("--tasks-dir", help="local Harbor task directory")
    g.add_argument("--dataset", metavar="ORG/NAME@VERSION", help="Harbor Hub dataset, e.g. terminal-bench/terminal-bench@4.0.0")
    ap.add_argument("--tasks", nargs="*", default=[], help="task names (default: every task)")
    ap.add_argument("--hours", type=float, help="agent budget per task (default: the benchmark's, else 3)")
    ap.add_argument("--continue-mode", choices=["same_conversation", "phases"],
                    help="for continue_until_timeout tasks (default: the benchmark's)")


def dataset_spec(text: str) -> dict:
    """'org/name@ver' -> Harbor DatasetConfig fields (Hub package); 'name@ver' -> legacy registry."""
    name, _, ver = text.partition("@")
    if "/" in name:
        return {"name": name, "ref": ver or "latest"}
    return {"name": name, **({"version": ver} if ver else {})}


def resolve_benchmark(a: argparse.Namespace) -> tuple[dict, float, str | None, str]:
    """-> (dataset block, hours, continue_mode or None, benchmark label)."""
    presets = load_benchmarks()
    preset: dict = {}
    label = "custom"
    if a.tasks_dir:
        dataset = {"path": a.tasks_dir}
    elif a.dataset:
        dataset = dataset_spec(a.dataset)
    else:
        label = a.benchmark or DEFAULT_BENCHMARK
        preset = presets[label]
        dataset = {"path": preset["tasks_dir"]} if "tasks_dir" in preset else dataset_spec(preset["dataset"])
    if a.tasks:
        dataset["task_names"] = a.tasks
    hours = a.hours if a.hours is not None else float(preset.get("hours", DEFAULT_HOURS))
    cont = a.continue_mode or preset.get("continue_mode")
    return dataset, hours, cont, label


def continue_env_line(mode: str | None) -> str | None:
    if mode is None:
        return None
    return "HB_CONTINUE_MODE=" + ("same_conversation" if mode == "same_conversation" else "")


def short_model(model: str) -> str:
    m = model.split("/")[-1]
    m = re.sub(r"^(claude|gpt)-", "", m)
    return re.sub(r"[^a-z0-9]+", "", m.lower())[:12]


def repo_relative(path: Path) -> str:
    """Path as written in the yaml header: relative to the repo root when inside it."""
    root = HERE.parent
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def parse_tokens(v: str) -> int:
    v = v.lower().strip()
    mult = 1000 if v.endswith("k") else 1
    return int(float(v.rstrip("k")) * mult)


def parse_sets(items: list[str]) -> dict:
    out = {}
    for kv in items:
        k, _, v = kv.partition("=")
        try:
            out[k] = yaml.safe_load(v)
        except yaml.YAMLError:
            out[k] = v
    return out
