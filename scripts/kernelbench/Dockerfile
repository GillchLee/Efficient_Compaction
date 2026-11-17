# Shared environment image for the KernelBench Harbor tasks (scripts/kernelbench/SOURCE.md).
# Build once: bash scripts/prep_kernelbench.sh --build-only   (tag optlimit/kernelbench-env:cu128)
FROM nvidia/cuda:12.8.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.10 python3.10-dev python3.10-venv python3-pip git ninja-build build-essential \
      curl ca-certificates tmux procps \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1

# PyTorch built against CUDA 12.8 (matches the image's nvcc; sm_120 Blackwell supported).
ARG TORCH_VERSION=2.11.0
RUN python -m pip install --upgrade pip && \
    python -m pip install "torch==${TORCH_VERSION}" --index-url https://download.pytorch.org/whl/cu128

# KernelBench at a pinned commit; problems live in /opt/KernelBench/KernelBench/level*/.
ARG KB_COMMIT=423217d9fda91e0c2d67e4a43bf62f96f6d104f1
RUN git clone https://github.com/ScalingIntelligence/KernelBench.git /opt/KernelBench && \
    cd /opt/KernelBench && git checkout -q "${KB_COMMIT}" && \
    python -m pip install -e . && \
    python -c "import kernelbench, torch; print('kernelbench ok, torch', torch.__version__, 'cuda', torch.version.cuda)"

ENV KB_PATH=/opt/KernelBench/KernelBench
RUN mkdir -p /workspace
WORKDIR /workspace
