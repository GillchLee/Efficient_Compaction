# Efficient Compaction

[Long-Horizon Terminal-Bench (LHTB)](https://github.com/zli12321/LHTB)에서 **컨텍스트 압축(compaction) 창 크기**가 장기 에이전트의 점수와 토큰·캐시 비용에 미치는 영향을 측정하고, 비용 모델로 구한 최적 임계값 L\*을 에이전트 안에서 **동적으로** 적용하는 실험 코드입니다. 실험 데이터(`jobs/`)와 LHTB 체크아웃(`LHTB/`)은 포함하지 않고, 같은 실험을 처음부터 다시 만들 수 있는 코드만 담았습니다.

- 에이전트: terminus-2 (Harbor 내장) 또는 그 서브클래스 `optlimit.harbor_agent:OptlimitTerminus2`
- 모델: LiteLLM 모델 id 아무거나 (`openai/gpt-6-luna`, `anthropic/claude-sonnet-5-5`, …)
- 실행 환경: Docker 또는 Daytona 클라우드 샌드박스 (설정 파일에서 선택)
- 조건: 압축 창 고정(40k / 80k / 120k / native) 또는 optlimit 가 매 턴 고르는 L\*; 과제당 3시간; 검증 거부 시 같은 대화로 이어가기

## 구성

기능별 디렉터리입니다. `LHTB/`(체크아웃 + 패치된 Harbor)만 외부 코드입니다.

```
setup.sh                      작업 환경 재구성: LHTB 핀 커밋 + Harbor 패치(순서대로) + .venv
patches/                      Harbor 패치, 번호 순서로 적용 (아래 표)
  01-usage-tracking.patch     API usage 기록 (Chat.last_usage, cache write 토큰)
  02-terminus2-prompts-and-short-summary.patch
                              요약 프롬프트 함수 분리, short summary 호출 기록
  03-interim-verifier-log.patch
                              제출별 중간 채점 결과를 interim_verifier.jsonl 에 기록
  04-verifier-feedback-option.patch
                              HB_VERIFIER_FEEDBACK=false 옵션 (채점 결과를 에이전트에 숨김)
configs/
  make_config.py              모델·압축 조건·환경(docker/daytona)·과제·시간·실행 옵션을 받아 job yaml 생성
  sweep/ smoke/ examples/     생성된(또는 손으로 쓴) job 설정
  mac/                        macOS + Daytona 에서 돌린 stock terminus-2 설정 (native-threshold 방식)
  prices.json                 모델별 단가 ($/M 또는 로컬 모델은 s/M: cache read / cache write / output)
  priors.json                 모델별 비용 모델 파라미터 (analysis/fit_priors.py 가 채움)
scripts/
  run_job.sh                  설정 파일 하나로 job 실행/이어서 실행 (.env 로드, 키·환경 확인, `# env:` 옵션, 끝나면 턴 표)
  run_sweep.sh                설정 여러 개를 PARALLEL 개씩 실행, 끝날 때마다 조건 비교표
  measure_vllm_prices.py      vLLM 서버의 prefill/cached/decode 시간을 재서 prices.json 행으로 기록
  make_luna_chart.sh          Luna sweep 전체 차트 (docs/luna_sweeps_intro.html + chart.py)
  archive/                    끝난 실험의 일회성 체인 스크립트
analysis/                     실행이 끝난 job 디렉터리를 읽는 집계 (입력은 전부 jobs/<job>)
  token_turns.py              턴별 prompt · cache read/write · output · 비용, 제출별 중간 점수 -> token_turns/*.csv
  sweep_summary.py            조건별 점수 · 토큰 · 요약 비용 비교표
  prefill_stats.py            compaction 직후 prefill(cache write), L_before / L_p, 요약 호출 토큰 통계
  trajectory_issues.py        검증 거부 · 재제출 · 응답 거부 · 오류 등 행동 지표
  public_feedback.py          (LHTB 2.0) 에이전트가 본 공개 검사 점수의 턴별 추이
  chart.py + chart.tpl.html   턴별 캐시 구성 · compaction · 거절 · 중간 reward 를 한 HTML 로
  reward_curve.py             시간·턴·비용 축의 reward 곡선 (제출별 중간 채점 → 곡선 아래 면적, 최고점 도달 시각)
  fit_priors.py               측정값에서 비용 모델 파라미터(g, L0, Lp, φ, m, γ, summary_out) 적합 -> priors.json
  replay.py                   기록된 궤적을 컨트롤러에 다시 먹여 고정 L / L* 정책의 비용을 비교
optlimit/                     압축 임계값 결정 (pip install -e .); harness 와 무관한 부분은 표준 라이브러리만 씀
  cost_model.py               f(L), 닫힌 해, 수치 최적화, 단기 규칙
  estimator.py                온라인 g 추정 (prior 로 수축한 평균 / 중앙값 / EMA)
  controller.py               세션 상태: L0 관측, g 갱신, L* 계산, "지금 압축" 결정; config_from_spec()
  usage.py                    Anthropic / OpenAI / vLLM / harbor usage 형식 통일
  adapters/                   harness 별 어댑터 (docs/adapter_guide.md)
    harbor_terminus2.py       terminus-2 서브클래스: API 가 보고한 prompt 토큰으로 압축 시점 결정
    terminus2_handoff.py      손 넘김 프롬프트 변형 (handoff: 블록) mixin
    chat_loop.py              참조 구현: 임의의 채팅 클라이언트를 감싸는 100줄 루프
  harbor_agent.py             shim (`optlimit.harbor_agent:OptlimitTerminus2` 경로 유지)
tests/                        비용 모델 · 컨트롤러 · 훅 · 턴 집계 · chat_loop 테스트 (pytest, API 불필요)
docs/                         수식(formulas.md), 설계·결과 문서, adapter_guide.md, cost_model_derivation2.pdf
```

## 재구성

필요한 것: `git`, [`uv`](https://docs.astral.sh/uv/), Docker 또는 Daytona API 키, 모델의 API 키. `git-lfs` 는 있으면 쓰고 없으면 건너뜁니다(일부 과제 자산만 LFS).

```bash
git clone https://github.com/GillchLee/Efficient_Compaction
cd Efficient_Compaction
bash setup.sh            # ./LHTB 에 LHTB(7ca0cac) 클론 + 패치, ./.venv 에 harbor[daytona] + optlimit
vi .env                  # OPENAI_API_KEY / ANTHROPIC_API_KEY / DAYTONA_API_KEY
```

`setup.sh`가 하는 일:

1. LHTB를 실험에 쓴 커밋 `7ca0cacea9bf6a0694eaad7306bed794eaaaabce`으로 체크아웃합니다 (`--filter=blob:none`).
2. `patches/NN-*.patch` 를 번호 순서로 적용합니다(이미 적용된 것은 건너뜁니다).
3. `.venv`에 패치된 Harbor(`harbor[daytona]`), 이 저장소(`optlimit`), pytest 를 설치합니다.

LHTB 안으로 복사하는 것은 없습니다. job 은 이 디렉터리에서 실행하고, 설정은 과제를 `./LHTB/tasks`(LHTB 1.0) 또는 `./LHTB/tasks-2.0`(LHTB 2.0 reconstruction 과제)로 가리킵니다.

## 실행

```bash
source .venv/bin/activate

# 1. 설정 만들기 (모델 · 압축 조건 · 환경 · 과제 · 시간)
python configs/make_config.py --model anthropic/claude-sonnet-5-5 --limit 80000 \
    --tasks-dir ./LHTB/tasks-2.0 --tasks gdal_antimeridian_clip__01 sqlite_wal_recovery_audit__02 \
    --env docker --hours 3                        # -> configs/sweep/sonnet-5-5_l80k.yaml
python configs/make_config.py --model openai/gpt-6-luna --limit native --env daytona --tasks 2048 chess-mate
python configs/make_config.py --model openai/gpt-6-luna --formula terminus2-gpt-6-luna --prices gpt-6-luna \
    --set g_prior_weight=0 --tasks-dir ./LHTB/tasks-2.0 --tasks gdal_antimeridian_clip__01 ...   # 동적 L*, Lp 고정, g 는 관측만

# 2. 실행 (같은 명령을 다시 실행하면 기존 job 을 이어서 진행)
bash scripts/run_job.sh configs/sweep/sonnet-5-5_l80k.yaml
setsid nohup bash scripts/run_job.sh configs/sweep/sonnet-5-5_l80k.yaml >/dev/null 2>&1 &   # Linux, 터미널과 분리
nohup caffeinate -ims bash scripts/run_job.sh configs/mac/luna_ctx80k.yaml &                # macOS, 잠자기 방지
PARALLEL=1 bash scripts/run_sweep.sh configs/sweep/luna10_*.yaml                             # 여러 조건을 차례로

# 3. 집계 (run_job.sh 가 끝나면 token_turns 는 자동으로 씁니다)
python analysis/token_turns.py jobs/sonnet-5-5_l80k        # -> jobs/<job>/token_turns/{turns,summary,submissions}.csv
python analysis/sweep_summary.py jobs/sonnet-5-5_* --out jobs/sweep_summary.csv
python analysis/prefill_stats.py jobs/sonnet-5-5_* --md jobs/prefill.md
python analysis/trajectory_issues.py jobs/sonnet-5-5_* --out jobs/issues.csv
python analysis/chart.py jobs/sonnet-5-5_* jobs/lhtb-luna-ctx* --out jobs/chart.html
python analysis/reward_curve.py jobs/luna10_* --cond luna10_l60k=l60k --out jobs/reward_curve.csv --md jobs/reward_curve.md
```

중단은 `pkill -INT -f "harbor run -c configs/sweep"` 또는 `harbor jobs resume` 프로세스에 `kill -INT`. 로그는 `jobs/logs/<job>.log`.

## 다른 서버에서 그대로 돌리기

저장소만 있으면 됩니다. 이 호스트에 묶인 경로는 없습니다.

```bash
# Linux + Docker (실험에 쓴 구성)
git clone https://github.com/GillchLee/Efficient_Compaction && cd Efficient_Compaction
bash setup.sh                                   # git, uv, docker 필요; PY=3.12 python -> PY=/usr/bin/python3.12 로 바꿀 수 있음
cp .env.example .env && vi .env                 # 쓰는 모델의 키만 채우면 됨
source .venv/bin/activate && python -m pytest -q # API 없이 30개 테스트
python configs/make_config.py --model openai/gpt-6-luna --limit 60000 --tasks-dir ./LHTB/tasks \
    --tasks spot-scheduler-traces apex-ib244-matter --hours 3 --concurrent 2
setsid nohup bash scripts/run_job.sh configs/sweep/gpt-6-luna_l60k.yaml >/dev/null 2>&1 &
tail -f jobs/logs/gpt-6-luna_l60k.log
```

### 로컬 모델 (vLLM)

Harbor 는 `hosted_vllm/<served-model-name>` 과 `api_base` 를 받습니다. 세 단계입니다.

```bash
# 0. 서빙 (다른 서버여도 됨). prefix caching 이 켜져야 cache read 가 usage 에 보고됩니다 (최근 vLLM 은 기본 on)
vllm serve Qwen/Qwen3-32B --served-model-name Qwen3-32B --max-model-len 131072 --enable-prefix-caching --port 8000

# 1. 단가: 달러 대신 서버에서 잰 시간 (prefill uncached / prefill cached / decode, s/M tokens). 수식은 비율만 씁니다
python scripts/measure_vllm_prices.py --api-base http://HOST:8000/v1 --model Qwen3-32B --name vllm-qwen3-32b
#    -> configs/prices.json models["vllm-qwen3-32b"]; cached_tokens 가 안 오면 경고를 냅니다

# 2. 설정과 실행. --context-window 가 Harbor 의 model_info 를 채우고, formula 모드에선 limit_max 를 컨텍스트 아래로 묶습니다
python configs/make_config.py --model hosted_vllm/Qwen3-32B --api-base http://HOST:8000/v1 --context-window 131072 \
    --limit 40000 --tasks-dir ./LHTB/tasks --tasks spot-scheduler-traces --hours 1 --job-name qwen_l40k
python configs/make_config.py --model hosted_vllm/Qwen3-32B --api-base http://HOST:8000/v1 --context-window 131072 \
    --formula terminus2-gpt-6-luna-1.0 --prices vllm-qwen3-32b --set g_prior_weight=0 \
    --tasks-dir ./LHTB/tasks --tasks spot-scheduler-traces --hours 1 --job-name qwen_formula
bash scripts/run_job.sh configs/sweep/qwen_l40k.yaml     # run_job.sh 가 ${api_base}/models 에 접속되는지 먼저 확인
python analysis/fit_priors.py jobs/qwen_* --prices vllm-qwen3-32b --name terminus2-qwen3-32b --write configs/priors.json
```

첫 실행 뒤 `jobs/<job>/token_turns/turns.csv` 의 `cache_read` 열이 0 이 아닌지 보세요. 0 이면 vLLM 이 `prompt_tokens_details.cached_tokens` 를 안 보내는 것이므로 prefix caching 을 켜야 합니다. `api_base` 를 설정에 쓰지 않으면 `.env` 의 `HOSTED_VLLM_API_BASE` 를 씁니다. `openai/` 접두사로 vLLM 을 가리키는 방식은 litellm 이 OpenAI 키를 요구하므로 `hosted_vllm/` 을 쓰세요.

### macOS + Daytona

Docker 없이 클라우드 샌드박스로 돌린 구성입니다(`jobs/lhtb-luna-ctx*`). 설정은 `configs/mac/` 에 있고 stock terminus-2 + `--native-threshold` 방식입니다. 새로 만들려면 `make_config.py --env daytona` 에 같은 옵션을 주면 됩니다.

```bash
bash setup.sh && vi .env                       # DAYTONA_API_KEY 와 모델 키
nohup caffeinate -ims bash scripts/run_job.sh configs/mac/luna_ctx80k.yaml &      # 잠자기 방지, 로그 jobs/logs/
python configs/make_config.py --model openai/gpt-6-luna --env daytona --limit 80000 --tasks-dir ./LHTB/tasks --tasks 2048 chess-mate --concurrent 1
```

Daytona 의 동시 메모리 한도(10GiB) 때문에 `--concurrent 1`. 턴 속도는 docker 의 절반 정도(호출당 고정 비용 ~11 s vs ~4.5 s)라 같은 3 h 에 턴 수가 적습니다.

## 실행 옵션 (설정 파일에 기록됨)

trial 루프 옵션은 환경변수(`HB_*`)인데, `make_config.py` 가 yaml 머리의 `# env:` 줄에 적고 `run_job.sh` 가 그 줄을 읽어 export 하므로 job 디렉터리의 설정만으로 조건이 재현됩니다. 기본값은 지금까지의 실험과 같습니다.

| 옵션 | 환경변수 | 기본 | 효과 |
|---|---|---|---|
| `--verifier-feedback false` | `HB_VERIFIER_FEEDBACK` | true | 제출마다 숨은 채점은 그대로 돌고 `interim_verifier.jsonl` 에 남지만, 에이전트에게는 "거부" 대신 결과를 알리지 않는 중립 이어가기 메시지를 줍니다 (패치 04). 채점 결과 전달이 시간축 정확도에 주는 영향을 보는 스위치 |
| `--continue-mode phases` | `HB_CONTINUE_MODE` | same_conversation | 거부 때마다 새 대화로 시작하는 리더보드 방식 |
| (직접) `HB_PROCESS_REWARD=1` | `HB_PROCESS_REWARD*` | off | LHTB 의 활동 시간 기준 체크포인트. 에이전트에 시간 메시지가 들어가고 docker 에선 거부되므로 별도 조건으로만 |

채점 결과를 숨겨도 통과(1.0)하면 trial 은 끝납니다(기존과 같음). 곡선은 `analysis/reward_curve.py` 로 봅니다.

## 압축 조건을 만드는 세 가지 방법

| `make_config.py` | 에이전트 | 압축 시점 | 용도 |
|---|---|---|---|
| `--limit N` | `OptlimitTerminus2` (mode fixed) | 직전 호출에서 API 가 보고한 `prompt_tokens` ≥ N | 창 크기 sweep. 어떤 제공자든 정확히 N 에서 압축 |
| `--limit native` | `OptlimitTerminus2` (mode off) | terminus-2 기본값(남은 컨텍스트 < 8000) | 기준선. 턴별 결정 로그는 남음 |
| `--formula PRIOR --prices ROW` | `OptlimitTerminus2` (mode formula) | 매 턴 L\* = argmin f_B(L) 를 다시 계산, P_t + ĝ ≥ L\* 이면 압축 | 동적 임계값. `--set g_prior_weight=0` 이면 g 는 사전 정보 없이 세션에서 관측한 증분의 평균만 쓰고, 첫 증분을 보기 전에는 압축하지 않음 |
| `--native-threshold N` | stock terminus-2 | litellm 로컬 토큰 수 기준 `proactive_summarization_threshold` | 예전 sweep 재현용 |

`OptlimitTerminus2`(`optlimit/adapters/harbor_terminus2.py`) 는 terminus-2 가 매 에피소드 시작에 부르는 `_check_proactive_summarization()` 하나만 바꿉니다. 컨트롤러가 "지금 압축"이라고 하면 terminus-2 의 `_summarize()`(3단계 요약 호출)를 그대로 부르므로 압축 메커니즘·비용 구조는 native 와 같고 **시점**만 달라집니다. 판단에 쓰는 값은 Harbor 패치가 `Chat.last_usage` 에 남기는 API 응답의 `prompt_tokens`·`cache_read` 라서, litellm 의 로컬 토크나이저가 Anthropic 프롬프트를 약 1.5배 적게 세는 문제(`--native-threshold` 방식의 L_before ≈ 1.5 × 명목값)가 없습니다. 기본 임계값(8000)은 안전망으로 그대로 둡니다. 결정은 `<trial>/agent/optlimit_decisions.jsonl` 에 턴마다 기록되고 `token_turns.py` 가 `ol_limit`, `ol_g_hat`, `ol_compact` 열로 붙입니다.

설정 파일에서는 이렇게 보입니다 (`ControllerConfig` 의 모든 필드를 `optlimit:` 아래에 쓸 수 있고, `--set key=value` 로도 줍니다):

```yaml
agents:
  - import_path: optlimit.harbor_agent:OptlimitTerminus2
    model_name: anthropic/claude-sonnet-5-5
    kwargs:
      parser_name: json
      enable_summarize: true
      proactive_summarization_threshold: 8000
      optlimit: {mode: fixed, fixed_limit: 80000, lookahead: 0}
      # optlimit: {mode: formula, prior: terminus2-sonnet-5-5, prices: claude-sonnet-5-5, lookahead: 1}
```

## 비용 모델 파라미터와 리플레이

`docs/cost_model_derivation2.pdf` 의 모델 A(창 반복의 읽기·쓰기 비용)에 요약 호출 비용을 더한 모델 B 를 씁니다(`optlimit/cost_model.py` 머리말). 파라미터는 측정값에서 뽑습니다.

```bash
python analysis/fit_priors.py jobs/sonnet-5-5_* --prices claude-sonnet-5-5 --name terminus2-sonnet-5-5 --write configs/priors.json
python analysis/replay.py jobs/sonnet-5-5_* --prior terminus2-sonnet-5-5 --prices claude-sonnet-5-5 --limits 40000 80000 120000
```

- `fit_priors.py`: g(턴당 prompt 증가, 중앙값), L0(압축 직후 턴의 cache read), Lp(그 턴의 cache write; L0 + Lp 가 압축 직후 prompt), φ(Lp 의 L 의존, 창 크기가 2개 이상일 때), m(요약 호출 입력 / L), γ(요약 입력의 실효 단가 배수), summary_out.
- `replay.py`: 기록된 턴별 증분을 컨트롤러에 다시 먹여 nocompact / fixed:L / formula 정책의 턴당 비용을 같은 단가로 계산합니다. `real_cost_per_turn` 은 그 궤적의 실제 집계이므로, 실제로 창 L 로 돌린 궤적의 `fixed:L` 리플레이가 실측과 맞는지로 비용 모델을 검증합니다 (Sonnet 80K · Luna 40K/120K 궤적에서 2% 이내).

## 적응형 한도 (가격비 α, β 가 바뀔 때)

L\* = L0 + Lp − g + √(2gC), C = (α−1)(Lp−g) + mγ(L0+Lp−g) + β·O_s 이므로 가격은 α(write/read)·β(output/read) 로만 들어온다. 정책이 가격·관측을 따라가게 하는 네 가지 스위치(`optlimit:` 블록):

| 키 | 효과 |
|---|---|
| `prices_reload: true` | 결정마다 `configs/prices.json` 의 변경을 감지해 `prices` 행을 다시 읽고 α, β 를 갱신(결정 로그에 `prices_reloaded`) |
| `learn_lp: true` | Lp 를 창 시작의 prompt − cached 관측으로 갱신(사전값 가중 `learn_weight` 회) |
| `learn_summary_out: true` | O_s 를 요약 호출들의 completion 합으로 갱신 |
| `learn_gamma: true` | γ = 1 + (α−1)·u, u 는 요약 호출 입력의 cache miss 비율 관측 |
| `reacq_turns: ρ`, `reacq_eps: ε` | 창 하한 K_min = ρ/ε, L_min = L0 + Lp + g(K_min − 1) (`floor_min_window`); ρ 는 `analysis/reacquisition.py` 의 압축당 재획득 턴(Luna 1.4). C 에 ρ·f(L\*) 를 더한다 |

분석: `analysis/price_sensitivity.py --prior <prior>` 가 공개 가격표별 L\*, α×β 격자, 고정 창의 후회, 탄력성을 낸다(`docs/price_sensitivity_{luna,sonnet}.md`). `analysis/replay.py --price-shift TURN:ROW` 는 궤적 중간의 가격 변경을 재생한다. 배경·문헌·실험 계획은 `docs/adaptive_limit_plan.md`. 창 하한(재획득 문턱 ≈ 23K, K ≥ 14)과 손 넘김 크기(Lp 4–7K 유지, 3회 호출 vs 1회 호출)의 실험 결론은 `docs/floor_and_handoff_design.md` §6 (7개 조건 × 3과제, 2026-11-04).

## 실험 설계

- **압축 창.** `--limit N` 은 API 가 보고한 prompt 가 N 에 닿으면 terminus-2 의 요약을 부릅니다. 예전 방식(`--native-threshold`)은 terminus-2 가 남은 컨텍스트 = 모델 한도 − litellm 로컬 토큰 수 < threshold 일 때 요약하므로 창 W 를 `threshold = 한도 − W` 로 만들었습니다(gpt-6-luna 922k: 80k → 842000). `model_info`로 컨텍스트 한도를 바꾸는 방법은 LiteLLM 전역 상태를 바꿔 같은 프로세스의 trial끼리 섞이므로 쓰지 않습니다.
- **검증 거부와 이어가기.** 모든 태스크가 `continue_until_timeout = true`입니다. 에이전트가 완료를 선언하면 Harbor가 에이전트를 멈추고 숨겨진 채점기를 돌리고, 점수가 1.0 미만이면 이유 없는 거부 메시지를 붙여 계속 진행시킵니다. `HB_CONTINUE_MODE=same_conversation`으로 이 거부를 **같은 대화**에 이어 붙입니다(`run_job.sh` 가 설정; 비우면 리더보드 방식인 phase 마다 새 대화). 거부마다 돌린 채점 결과는 `<trial>/interim_verifier.jsonl` 에 남고 에이전트에게는 보이지 않습니다.
- **태스크.** LHTB 1.0: games(2048, chess-mate, sokoban), systems-engineering(spot-scheduler-traces, vector-db-iterative-build, duckdb-optimizer-closure), tool-use/APEX(apex-ib244, apex-investment-banking, apex-law433). LHTB 2.0(`tasks-2.0/`): gdal_antimeridian_clip__01, sqlite_wal_recovery_audit__02, seismo_cross_correlation_lag__01 등 reconstruction 과제. 과제는 실행마다 `--tasks` 로 고릅니다.

## Harbor 패치 내용

| 파일 | 변경 |
|---|---|
| `llms/lite_llm.py` | cache write 토큰 추출: Anthropic `usage.cache_creation_input_tokens`, OpenAI `prompt_tokens_details.cache_write_tokens` |
| `models/metric/usage_info.py` | `UsageInfo.cache_creation_tokens` 필드 |
| `llms/chat.py` | 누적 `total_cache_creation_tokens`; 직전 호출의 usage 를 `Chat.last_usage` 에 보관 (`OptlimitTerminus2` 가 읽음) |
| `agents/terminus_2/terminus_2.py` | 턴마다 `metrics.extra.cache_creation_tokens` 기록, 문맥 초과 시의 short summary 호출도 `_run_subagent`로 보내 토큰·비용을 `trajectory.summarization-N-short.json`에 기록 |
| `trial/trial.py` (03) | 완료 선언(제출)마다 돌리는 중간 채점 결과를 `<trial>/interim_verifier.jsonl`에 기록(제출 번호, 시각, 남은 시간, 점수, 통과 여부, feedback 모드). 에이전트가 받는 것은 여전히 이유 없는 거부 메시지뿐입니다 |
| `trial/trial.py` (04) | `HB_VERIFIER_FEEDBACK=false` 면 거부 메시지 대신 결과를 알리지 않는 중립 이어가기 메시지(같은 대화 / phase 방식 둘 다). 기본값은 기존 동작 |

trajectory 위치: `jobs/<job>/<task>__<id>/agent/trajectory.json`(본 대화), `trajectory.summarization-N-{summary,questions,answers,short}.json`(압축 때의 요약 호출), `optlimit_decisions.jsonl`(턴별 L\*·ĝ·결정), `<trial>/interim_verifier.jsonl`(제출별 중간 채점), `verifier/scorecard.json`(최종 채점 상세).

## 알아둘 점

- **Daytona 메모리 한도.** 기본 등급은 계정 전체 동시 메모리가 10GiB입니다. vector-db, duckdb는 각각 8GB라 동시에 두 개를 띄울 수 없으니 `--concurrent 1`, `PARALLEL=1`로 실행하세요.
- **Docker 호스트.** 에이전트 루프는 로컬에서 돌므로 셸이 죽으면 trial 이 끊깁니다. Linux 에서는 `setsid nohup`, macOS 에서는 `caffeinate` 로 띄우고, 같은 명령으로 이어서 실행하세요. Docker 의 기본 주소 풀은 네트워크 약 30개까지라 오래된 harbor 네트워크를 지워야 할 때가 있습니다.
- **`harbor run -i`.** 태스크 필터 `-i/-x`는 `-p <tasks 경로>` 없이 쓰면 오류가 납니다. 태스크를 고를 때는 설정 파일의 `task_names`(`make_config.py --tasks`)를 쓰세요.
- **업스트림 테스트.** LHTB 7ca0cac의 Harbor에는 원래부터 `tests/unit/agents/terminus_2/test_same_conversation_resume.py`(문법 오류)와 tmux 테스트 5개가 실패합니다. 이 패치와 무관합니다.
- **터미널 기록 파일 폭주.** 이유 없는 거부를 많이 받으면 에이전트가 채점 정보를 찾으러 `/logs`를 뒤지기도 합니다. 이때 `cat /logs/agent/terminus_2.pane`(지금 터미널을 기록하는 파일)을 실행하면 출력이 같은 파일에 다시 쌓이면서 디스크가 가득 찹니다. 생기면 멈춘 Harbor를 `kill -INT`로 끝내고, 그 trial 폴더를 옮긴 뒤 같은 명령으로 이어서 실행하세요.
- **같은 대화 이어가기의 제출 루프.** 점수가 1.0 미만인 과제는 "완료 선언 → 거부 → 재제출"이 30초 간격으로 반복되어 3시간 동안 수백 phase 가 쌓입니다. `trajectory_issues.py` 의 `quick_resubmits` 로 양을 볼 수 있고, 점수는 보통 첫 몇 번의 제출 안에 포화합니다(`chart.py` 의 reward 패널).
- **비용.** `cost_usd`는 LiteLLM 요금표 기준 추정치입니다. `chart.py`, `replay.py` 는 `configs/prices.json` 의 단가를 씁니다(장문 할증 미적용).

## 라이선스

LHTB와 Harbor는 각 저장소의 라이선스를 따릅니다. 패치는 Harbor 코드를 수정한 것입니다.
