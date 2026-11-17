#!/usr/bin/env python3
"""Generate Harbor task directories from KernelBench problems (runs INSIDE the
optlimit/kernelbench-env image, where the kernelbench package and the problem files exist).

    python adapter.py --output-dir /out [--level 1] [--limit 5] [--problem-ids 1,5,42]
                      [--representative] [--backend cuda] [--prompt-option one_shot] [--precision fp32]

Based on Harbor PR #999 adapters/kernelbench/adapter.py (see SOURCE.md); uses KernelBench's own
prompt constructor (instruction parity) and reads problems from KB_PATH (local source).
"""
import argparse
import os
import re
import shutil
from pathlib import Path

from kernelbench.dataset import construct_kernelbench_dataset, get_representative_dataset
from kernelbench.prompt_constructor_toml import get_prompt_for_backend

TEMPLATE_DIR = Path(__file__).resolve().parent / "template"
KB_PATH = os.environ.get("KB_PATH", "/opt/KernelBench/KernelBench")

HARBOR_ADDENDUM = """

---

Your solution will be evaluated from `/workspace/solution.py`. This file must be a complete, self-contained Python module that defines a `ModelNew` class. It will be compared against the original `Model` for correctness and performance (CUDA event timing on the GPU attached to this container).

Do not include any testing or benchmarking code in your solution.
"""


def make_task_id(problem) -> str:
    """kernelbench-level1-001-square-matrix-multiplication"""
    name_part = "_".join(problem.name.replace(".py", "").split("_")[1:])
    slug = re.sub(r"[^a-z0-9]+", "-", name_part.lower()).strip("-")
    return f"kernelbench-level{problem.level}-{problem.problem_id:03d}-{slug}"


def generate_task(problem, out: Path, backend: str, prompt_option: str, precision: str) -> Path:
    task_id = make_task_id(problem)
    task_dir = out / task_id
    shutil.rmtree(task_dir, ignore_errors=True)
    task_dir.mkdir(parents=True)
    for sub in ("environment", "solution", "tests"):
        shutil.copytree(TEMPLATE_DIR / sub, task_dir / sub)
    toml = (TEMPLATE_DIR / "task.toml").read_text()
    desc = f"KernelBench level {problem.level} problem {problem.problem_id}: {problem.name.replace('.py', '')}"
    toml = toml.replace("__TASK_ID__", task_id).replace("__DESCRIPTION__", desc.replace('"', "'"))
    (task_dir / "task.toml").write_text(toml)
    instruction = get_prompt_for_backend(
        ref_arch_src=problem.code, backend=backend, option=prompt_option, precision=precision
    ) + HARBOR_ADDENDUM
    (task_dir / "instruction.md").write_text(instruction)
    (task_dir / "solution" / "reference.py").write_text(problem.code)
    (task_dir / "tests" / "reference.py").write_text(problem.code)
    (task_dir / "solution" / "solve.sh").chmod(0o755)
    (task_dir / "tests" / "test.sh").chmod(0o755)
    return task_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--level", type=int, choices=[1, 2, 3], help="one level only (default: 1, 2, 3)")
    ap.add_argument("--limit", type=int, help="max tasks per level")
    ap.add_argument("--problem-ids", help="comma-separated problem ids (with --level)")
    ap.add_argument("--representative", action="store_true", help="KernelBench's curated subset (18 + 8 + 9)")
    ap.add_argument("--backend", default="cuda", choices=["cuda", "triton", "cute", "tilelang", "hip"])
    ap.add_argument("--prompt-option", default="one_shot", choices=["zero_shot", "one_shot", "few_shot"])
    ap.add_argument("--precision", default="fp32", choices=["fp32", "fp16", "bf16"])
    a = ap.parse_args()

    ids = {int(x) for x in a.problem_ids.split(",")} if a.problem_ids else None
    out = a.output_dir
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for level in ([a.level] if a.level else [1, 2, 3]):
        if a.representative:
            ds = get_representative_dataset(level=level, source="local")
        else:
            ds = construct_kernelbench_dataset(level=level, source="local", base_path=KB_PATH)
        problems = list(ds)
        if ids is not None:
            problems = [p for p in problems if p.problem_id in ids]
        if a.limit is not None:
            problems = problems[: a.limit]
        for p in problems:
            generate_task(p, out, a.backend, a.prompt_option, a.precision)
            n += 1
    print(f"generated {n} tasks in {out}")


if __name__ == "__main__":
    main()
