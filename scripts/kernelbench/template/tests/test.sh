#!/bin/bash
# KernelBench verifier: compile /workspace/solution.py, check correctness against the reference,
# time both, write /logs/verifier/reward.json (reward = speedup if correct else 0).
set -e
mkdir -p /logs/verifier
echo '{"reward": 0.0}' > /logs/verifier/reward.json
cp /tests/reference.py /workspace/reference.py
cd /workspace
python /tests/eval_kernel.py
