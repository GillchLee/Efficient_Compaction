# KernelBench → Harbor adapter (vendored)

Origin: Harbor PR #999 "[WIP] KernelBench Adapter" (simonguozirui/harbor-kb-adaptor, branch
kernelbench-adapter, commit 8f827389, files adapters/kernelbench/*), fetched 2026-11-13.
KernelBench itself: https://github.com/ScalingIntelligence/KernelBench (MIT), pinned in the
Dockerfile at commit 423217d9 (main, 2026-03-05). Problems: KernelBench/level{1,2,3}/*.py
(100 / 100 / 50), level 4 (20 HF models) not supported by the adapter.

Changes from the PR:
- Dockerfile: nvidia/cuda:12.8.1-devel-ubuntu22.04 + torch cu128 (host driver is CUDA 13.2,
  GPUs are RTX PRO 6000 Blackwell, sm_120) + tmux/procps for terminus-2; the KernelBench repo is
  cloned into the image (/opt/KernelBench) so task generation and evaluation need no network.
- One shared, pre-built image (`optlimit/kernelbench-env:cu128`) referenced by task.toml
  `docker_image` instead of a per-task Dockerfile build (harbor `delete: true` would otherwise
  rebuild it after every trial).
- Tasks are generated inside that image (scripts/prep_kernelbench.sh) from the local problem
  files (`source=local`), not from the HuggingFace dataset.
- environment/docker-compose.yaml pins the GPU with `device_ids: ["${KB_GPU:-0}"]` so a run can
  pick a free GPU on a shared host (`KB_GPU=2 bash scripts/run_job.sh ...`). Harbor's own
  `gpus = 1` reservation is then skipped (explicit Compose GPU selection wins).
- task.toml: version "1.0" layout used by the other Harbor-0.25 benchmarks here, agent budget
  7200 s (make_config --hours overrides), verifier 1800 s, cpus 4 / 16 GB.
- eval_kernel.py: reward = speedup when compiled & correct (0 otherwise), plus `correct`,
  `fast_1` fields; exceptions are caught and written to reward.json.

KernelBench has no official agentic protocol (the upstream eval is single-shot generation +
eval, metric fast_p); the PR's agent timeout was 600 s. Numbers from this adapter are therefore
not comparable to the KernelBench leaderboard.
