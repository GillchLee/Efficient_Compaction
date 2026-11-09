# Harbor 0.25 프로필 벤치마크: SWE-Marathon, FrontierSWE v2

`setup.sh --profile harbor25` 환경(stock Harbor 0.25, 패치 없음)에서 돌리는 벤치마크와, 2026-11-10 스모크에서 확인한 사실.

## 준비

```bash
bash setup.sh --profile harbor25                       # ./.venv-h25
git clone --depth 1 https://github.com/abundant-ai/swe-marathon data/swe-marathon
git clone --depth 1 https://github.com/Proximal-Labs/frontier-swe-v2 data/frontier-swe-v2   # 768 MB
python configs/make_config.py --harness claude-code --model anthropic/claude-haiku-5-5 \
    --proxy-fixed 30k --benchmark swe_marathon --tasks zstd-decoder --hours 1
bash scripts/run_job.sh configs/sweep/<job>.yaml
```

| | SWE-Marathon | FrontierSWE v2 |
|---|---|---|
| 태스크 | 20 (GPU 5: embedding-eval, jax-pytorch-rewrite, parameter-golf, post-train-ifeval-gpu, trimul-cuda; H100 고정) | 34 (GPU 11; CPU 23) |
| 네트워크 | 환경 public(빌드·설치), 에이전트 allowlist(16개) 또는 public(excel/mastodon/s3/slack-clone), 검증기 no-network | 환경 전체 allowlist(deny-all) |
| 이미지 | Dockerfile 빌드 | 공개 레지스트리(us-west1) digest 고정, 검증기 이미지 별도. 이 서버에서 0.4 GB 받는 데 28분 |
| 예산 | task.toml 4~10 h (프리셋 기본 5 h) | 20 h (프리셋 기본 20 h; 실험에서는 `--hours`로 줄이고 편차로 명시) |
| 공식 실행 | Modal, `claude-code` + Opus 4.8, `disallowed_tools="WebSearch WebFetch"` | Modal(px-eval), Proximus 또는 claude-code |

## 하네스 경로

- **claude-code**: `optlimit.adapters.harbor_installed:OptlimitClaudeCode`. Harbor 0.25 는 에이전트 kwargs 를 `options_model` 로 검증하므로
  어댑터가 `compaction` 필드를 선언한다. CLI 설치가 컨테이너 안에서 220 MB 바이너리를 받으므로 `override_setup_timeout_sec`(프로필 기본 1200;
  Harbor 기본 360 은 느린 회선에서 모자람). egress 가 막힌 태스크를 위해 `extra_allowed_hosts` 에 설치 호스트(downloads.claude.ai, apt 미러)를
  넣고, `run_harness_job.sh` 가 프록시 호스트를 `--allow-agent-host`/`--allow-environment-host` 로 더한다.
- **terminus-2-proxy**: stock terminus-2(`name: terminus-2`), LLM 호출을 프록시로(`ANTHROPIC_API_BASE`). 프록시의 `prompt is too long` 400 →
  `ContextLengthExceededError` → terminus-2 의 3단계 hand-off. **`llm_call_kwargs.num_retries` 를 주면 안 된다**: litellm 이 400 을 스스로
  재시도해 하네스가 오류를 보지 못하고, 프록시는 3회 거부 뒤 fail-open 한다(스모크에서 확인). 질문 서브에이전트 호출은 빈 이력으로 시작해
  프록시가 다른 세션으로 기록한다(비용 로그에는 있고, 학습 C 에는 안 들어감).
- 한 턴에 큰 도구 출력이 들어오면 prompt 가 한도를 훌쩍 넘긴 뒤(117K 관측) 다음 요청에서 트리거된다. 프록시는 사후 트리거라 이를 막지 못한다.

## 스모크 (2026-11-10, Haiku 5.5, 프록시 고정 30K, 1 h, 이 서버 Docker)

| | Claude Code | terminus-2-proxy |
|---|---|---|
| SWE-Marathon zstd-decoder | reward 1.0, 25분 만에 완료 선언, 압축 18, max prompt 63K, $0.25 | reward 1.0, 1 h 소진, 압축 9, max prompt 37K, $0.42 |
| FrontierSWE spice-circuit-simulator-in-rust | reward 0.030 (3/99), 압축 7, max prompt 117K, $0.15 | reward 0.061 (6/99), 압축 7, max prompt 32K, $0.21 |

둘 다 네트워크 정책은 공식 그대로(컨테이너 안에서 github/pypi/apt 차단, 프록시 호스트만 통과 확인).
