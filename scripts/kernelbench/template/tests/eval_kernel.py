"""KernelBench evaluation for Harbor (uses kernelbench.eval for parity with the upstream eval)."""
import json
import os
import traceback
from pathlib import Path

import torch
from kernelbench.eval import eval_kernel_against_ref, get_torch_dtype_from_string

REWARD = Path("/logs/verifier/reward.json")      # Harbor: every value must be a finite number
DETAILS = Path("/logs/verifier/eval_details.json")  # free-form diagnostics (gpu, metadata, error)


def write(d):
    numeric = {k: (int(v) if isinstance(v, bool) else v) for k, v in d.items() if isinstance(v, (bool, int, float))}
    REWARD.write_text(json.dumps(numeric, indent=2))
    DETAILS.write_text(json.dumps(d, indent=2, default=str))
    print("Evaluation complete:")
    for k, v in d.items():
        if k != "metadata":
            print(f"  {k}: {v}")


ref_code = Path("/workspace/reference.py").read_text()
sol_path = Path("/workspace/solution.py")
if not sol_path.exists():
    write({"reward": 0.0, "speedup": 0.0, "compiled": False, "correct": False, "fast_1": False,
           "error": "/workspace/solution.py not found"})
    raise SystemExit(0)
sol_code = sol_path.read_text()

try:
    result = eval_kernel_against_ref(
        original_model_src=ref_code,
        custom_model_src=sol_code,
        measure_performance=True,
        num_correct_trials=int(os.environ.get("KB_NUM_CORRECT_TRIALS", "5")),
        num_perf_trials=int(os.environ.get("KB_NUM_PERF_TRIALS", "100")),
        precision=get_torch_dtype_from_string(os.environ.get("KB_PRECISION", "fp32")),
        backend=os.environ.get("KB_BACKEND", "cuda"),
        timing_method=os.environ.get("KB_TIMING_METHOD", "cuda_event"),
        device=torch.device("cuda:0"),
        build_dir="/workspace/.kb_build",
    )
except Exception:
    write({"reward": 0.0, "speedup": 0.0, "compiled": False, "correct": False, "fast_1": False,
           "error": traceback.format_exc()[-4000:]})
    raise SystemExit(0)

compiled = bool(result.compiled) if result else False
correct = bool(result.correctness) if result else False
if not compiled or not correct:
    speedup = 0.0
elif not result.runtime or result.runtime <= 0:
    speedup = 1.0
else:
    speedup = result.ref_runtime / result.runtime

write({
    "reward": round(speedup, 4),
    "speedup": round(speedup, 4),
    "compiled": compiled,
    "correct": correct,
    "fast_1": bool(correct and speedup > 1.0),
    "runtime_us": round(result.runtime, 2) if result and result.runtime else -1,
    "ref_runtime_us": round(result.ref_runtime, 2) if result and result.ref_runtime else -1,
    "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    "metadata": (result.metadata if result else {}) or {},
})
