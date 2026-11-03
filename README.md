# Efficient Compaction

[Long-Horizon Terminal-Bench (LHTB)](https://github.com/zli12321/LHTB)에서 **컨텍스트 압축(compaction) 창 크기**가 장기 에이전트의 점수와 토큰·캐시 비용에 미치는 영향을 측정하는 실험 코드입니다. 실험 데이터(`jobs/`)는 포함하지 않고, 같은 실험을 처음부터 다시 만들 수 있는 코드만 담았습니다.

- 에이전트: terminus-2 (Harbor 내장)
- 모델: `openai/gpt-6-luna` (컨텍스트 922k)
- 실행 환경: Daytona 클라우드 샌드박스
- 조건: 압축 창 40k / 80k / 120k / native, 태스크당 3시간, 검증 거부 시 같은 대화로 이어가기

## 구성

```
patches/harbor-token-and-summary-tracking.patch   LHTB에 포함된 Harbor 수정 (5개 파일)
configs/sweep/luna_ctx{40k,80k,120k,native}.yaml  압축 창 조건별 job 설정
configs/sweep/luna_ctx80k_rerun_law433.yaml       80k apex-law433 재실행
configs/smoke/luna_ctx6k_smoke.yaml               6k 창 · 15분 동작 확인용
configs/examples/*.yaml                           Claude(Anthropic API) 예시 설정
scripts/run_luna_ctx_sweep.sh                     창 조건 여러 개를 PARALLEL개씩 실행/이어서 실행
scripts/run_job.sh                                설정 파일 하나로 job 실행/이어서 실행
scripts/token_turns.py                            턴별 prompt · cache read/write · output · 비용, 제출별 중간 채점 점수 집계
scripts/sweep_summary.py                          조건별 점수 · 토큰 · 요약 비용 비교표
scripts/trajectory_issues.py                      검증 거부 · 재제출 · 응답 거부 · 오류 등 행동 지표
setup.sh                                          작업 환경 재구성
```

## 재구성

필요한 것: `git`, `git-lfs`, [`uv`](https://docs.astral.sh/uv/), OpenAI API 키, Daytona API 키.

```bash
git clone https://github.com/GillchLee/Efficient_Compaction
cd Efficient_Compaction
bash setup.sh            # ./LHTB 에 LHTB(7ca0cac) 클론 + 패치 + 설정/스크립트 복사 + .venv
vi LHTB/.env             # OPENAI_API_KEY, DAYTONA_API_KEY 입력
```

`setup.sh`가 하는 일:

1. LHTB를 실험에 쓴 커밋 `7ca0cacea9bf6a0694eaad7306bed794eaaaabce`으로 체크아웃하고 LFS 파일을 받습니다.
2. `patches/`의 Harbor 패치를 적용합니다(이미 적용돼 있으면 건너뜁니다).
3. `configs/`, `scripts/`를 LHTB 안의 같은 위치로 복사합니다.
4. Python 3.12 가상환경을 만들고 패치된 Harbor를 `harbor[daytona]`로 설치합니다.

## 실행

```bash
cd LHTB

# 동작 확인: 6k 창, spot-scheduler-traces 한 개, 15분 (압축이 여러 번 일어남)
bash scripts/run_job.sh configs/smoke/luna_ctx6k_smoke.yaml

# 압축 창 조건 실행 (잠자기 방지 + 터미널을 닫아도 계속)
nohup caffeinate -ims bash scripts/run_luna_ctx_sweep.sh 80k native > jobs/sweep_logs/launcher.log 2>&1 &
nohup caffeinate -ims bash scripts/run_job.sh configs/sweep/luna_ctx120k.yaml > jobs/sweep_logs/launcher_120k.log 2>&1 &

# 집계
source .venv/bin/activate
python scripts/token_turns.py jobs/lhtb-luna-ctx80k          # -> jobs/lhtb-luna-ctx80k/token_turns/{turns,summary,submissions}.csv
python scripts/sweep_summary.py jobs/lhtb-luna-ctx80k jobs/lhtb-luna-ctxnative --out jobs/sweep_luna_ctx.csv
python scripts/trajectory_issues.py jobs/lhtb-luna-ctx80k jobs/lhtb-luna-ctxnative --out jobs/trajectory_issues.csv
```

같은 명령을 다시 실행하면 기존 job을 이어서 진행합니다. 중단은 `pkill -INT -f "harbor run -c configs/sweep"`입니다.

## 실험 설계

- **압축 창.** terminus-2는 남은 컨텍스트가 `proactive_summarization_threshold`보다 적어지면 대화를 요약하고 이어받습니다. 창 크기 W는 `threshold = 922000 − W`로 만듭니다(80k → 842000, 120k → 802000, 40k → 882000). native는 기본값 8000이라 3시간 안에는 사실상 압축이 일어나지 않습니다. `model_info`로 컨텍스트 한도를 바꾸는 방법은 LiteLLM 전역 상태를 바꿔 같은 프로세스의 trial끼리 섞이므로 쓰지 않습니다.
- **검증 거부와 이어가기.** 모든 태스크가 `continue_until_timeout = true`입니다. 에이전트가 완료를 선언하면 Harbor가 에이전트를 멈추고 숨겨진 채점기를 돌리고, 점수가 1.0 미만이면 이유 없는 거부 메시지를 붙여 계속 진행시킵니다. `HB_CONTINUE_MODE=same_conversation`으로 이 거부를 **같은 대화**에 이어 붙입니다(스크립트가 자동 설정).
- **태스크.** games(2048, chess-mate, sokoban), systems-engineering(spot-scheduler-traces, vector-db-iterative-build, duckdb-optimizer-closure), tool-use/APEX(apex-ib244, apex-investment-banking, apex-law433). 현재는 앞의 6개(sokoban, vector-db, duckdb 제외)로 비교합니다.

## Harbor 패치 내용

| 파일 | 변경 |
|---|---|
| `llms/lite_llm.py` | cache write 토큰 추출: Anthropic `usage.cache_creation_input_tokens`, OpenAI `prompt_tokens_details.cache_write_tokens` |
| `models/metric/usage_info.py` | `UsageInfo.cache_creation_tokens` 필드 |
| `llms/chat.py` | 누적 `total_cache_creation_tokens` |
| `agents/terminus_2/terminus_2.py` | 턴마다 `metrics.extra.cache_creation_tokens` 기록, 문맥 초과 시의 short summary 호출도 `_run_subagent`로 보내 토큰·비용을 `trajectory.summarization-N-short.json`에 기록 |
| `trial/trial.py` | 완료 선언(제출)마다 돌리는 중간 채점 결과를 `<trial>/interim_verifier.jsonl`에 기록(제출 번호, 시각, 남은 시간, 점수, 통과 여부). Mac 쪽에만 남고 에이전트에게는 보이지 않으며, 에이전트가 받는 것은 여전히 이유 없는 거부 메시지뿐입니다 |

trajectory 위치: `jobs/<job>/<task>__<id>/agent/trajectory.json`(본 대화), `trajectory.summarization-N-{summary,questions,answers,short}.json`(압축 때의 요약 호출), `interim_verifier.jsonl`(제출별 중간 채점), `verifier/scorecard.json`(최종 채점 상세).

## 알아둘 점

- **Daytona 메모리 한도.** 기본 등급은 계정 전체 동시 메모리가 10GiB입니다. vector-db, duckdb는 각각 8GB라 동시에 두 개를 띄울 수 없으니 `PARALLEL=1`로 실행하세요.
- **잠자기.** 에이전트 루프는 로컬에서 돌기 때문에 실행 중에 컴퓨터가 잠들거나 재시동되면 trial이 끊깁니다. `caffeinate`를 쓰고, macOS 자동 업데이트 설치를 꺼 두세요.
- **`harbor run -i`.** 태스크 필터 `-i/-x`는 `-p <tasks 경로>` 없이 쓰면 오류가 납니다. 태스크를 고를 때는 설정 파일의 `task_names`를 쓰세요.
- **업스트림 테스트.** LHTB 7ca0cac의 Harbor에는 원래부터 `tests/unit/agents/terminus_2/test_same_conversation_resume.py`(문법 오류)와 tmux 테스트 5개가 실패합니다. 이 패치와 무관합니다.
- **터미널 기록 파일 폭주.** 이유 없는 거부를 많이 받으면 에이전트가 채점 정보를 찾으러 `/logs`를 뒤지기도 합니다. 이때 `cat /logs/agent/terminus_2.pane`(지금 터미널을 기록하는 파일)을 실행하면 출력이 같은 파일에 다시 쌓이면서 디스크가 가득 찹니다. 실제로 120k spot-scheduler에서 4GB 디스크가 1분 만에 찼고, Harbor가 실패 처리 중에 멈췄습니다. 조건 간 비교를 위해 하네스는 고치지 않았습니다. 생기면 멈춘 Harbor를 `kill -INT`로 끝내고, 그 trial 폴더를 옮긴 뒤 같은 명령으로 이어서 실행하세요. 켜진 채 남은 샌드박스는 Daytona에서 중지하세요.
- **비용.** `cost_usd`는 LiteLLM 요금표 기준 추정치입니다.

## 라이선스

LHTB와 Harbor는 각 저장소의 라이선스를 따릅니다. 패치는 Harbor 코드를 수정한 것입니다.
