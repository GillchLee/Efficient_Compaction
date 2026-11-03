# 통합 전후 비교 (2026-11-03)

두 코드베이스를 하나로 합쳤습니다.

- **A. GitHub `Efficient_Compaction` main (5de181a)** — Mac + Daytona, LHTB 1.0 9개 과제, GPT-6 luna 40k/80k/120k/native. Harbor 패치로 토큰을 trajectory 에 기록, `scripts/` 에 실행·집계.
- **B. 로컬 `optimize_limit`** — Linux + Docker, LHTB 2.0 3개 과제, Luna/Haiku/Sonnet sweep. vendored harbor, litellm 콜백 usage 로그, `optlimit` 패키지(비용 모델·proxy), HTML 차트.

결과는 **A 를 기준으로** B 의 기능을 옮긴 것이고, 이 브랜치(`refactor`)의 `git diff main` 이 전체 변경입니다. 주요 기능은 그대로입니다: (1) harbor 로 LHTB 를 돌린다, (2) 턴별로 원하는 값을 추적한다, (3) 압축 한도를 동적으로 바꿀 수 있다 — (3)만 proxy 가 아니라 에이전트 안의 훅으로 바뀌었습니다.

## 파일 대응표

| 전 (A main / B optimize_limit) | 후 | 비고 |
|---|---|---|
| A `setup.sh` | `setup.sh` | LHTB 를 shallow clone, configs/scripts 를 LHTB 안으로 **복사하지 않음**(job 은 저장소 루트에서 실행), venv 는 `./.venv` 에 harbor + optlimit + pytest, git-lfs 없어도 진행 |
| A `patches/harbor-token-and-summary-tracking.patch` (5 파일) | 같은 파일 | `llms/chat.py` 에 `Chat.last_usage` 한 줄 추가(직전 호출의 API usage). 나머지 hunk 동일 |
| B `third_party/lhtb_harbor/` (vendored 복사본 + `_record_phase_reward` 패치) | 삭제 | A 의 `interim_verifier.jsonl` 패치가 같은 역할(제출 번호·시각·점수·timeout 여부까지 기록) |
| B `.venv_lhtb/.../optlimit_usage_log.py` + `.pth` (litellm 콜백 usage 로그) | 삭제 | 패치가 trajectory metrics 에 `cache_creation_tokens` 를 직접 기록. 예전 로그는 `token_turns.py --usage` 로 읽어 호환 |
| A `scripts/run_job.sh` | `scripts/run_job.sh` | 모델 접두사(anthropic/openai)에 맞는 키 확인, docker/daytona 둘 다 확인, 로그 `jobs/logs/`, 끝나면 `analysis/token_turns.py` |
| A `scripts/run_luna_ctx_sweep.sh` (luna 창 이름 고정) | `scripts/run_sweep.sh` | 임의의 설정 파일 목록을 PARALLEL 개씩; `run_job.sh` 재사용 |
| A `scripts/run_claude_long3.sh` | 삭제 | `run_job.sh configs/examples/claude_long3.yaml` 과 동일 |
| B `scripts/run_lhtb_t2.sh`, `lhtb_t2_sweep.sh`, `run_lhtb_t2_guarded.sh`, `lhtb_watchdog.py`, `run_t2*.sh`, `lhtb_*.sh` | 삭제 | `run_job.sh`/`run_sweep.sh` 로 대체. 가드·watchdog 은 넣지 않음 |
| A `configs/sweep/*.yaml`, `smoke/`, `examples/` | 유지 (`path: ./LHTB/tasks` 로 경로만 수정) | `--native-threshold` 방식의 예전 sweep 재현용 |
| B `configs/lhtb/sweep/*.yaml` (모델·창마다 복제, Anthropic 1.53배 보정 산수) | `configs/make_config.py` 로 생성 | 모델·압축 조건·환경·과제·시간이 인자. 보정 산수 불필요 |
| B `configs/prices.json`, `priors.json` | `configs/` 로 이동 | priors 에 `terminus2-sonnet-5-5`, `terminus2-gpt-6-luna` 추가(측정값 적합) |
| A `scripts/token_turns.py` | `analysis/token_turns.py` | 열 추가: `compaction`, `rejected`, `has_cmd`, `ol_limit/ol_g_hat/ol_compact`; summary 에 `model`, `condition`, `end`, `cmd_turns`, `duration_min`, `first_completion_min`; `--usage` 로 예전 로그 호환; 예전 `phase_rewards*.jsonl` 도 submissions 로 |
| B `scripts/t2_turns.py`, `t2_export_turns.py`, `t2_sweep_report.py` | 삭제 | `token_turns.py` + `sweep_summary.py` 가 같은 수치를 냄(아래 검증) |
| A `scripts/sweep_summary.py` | `analysis/sweep_summary.py` | 조건 라벨을 job 이름 대신 summary.csv 의 `condition` 에서 |
| A `scripts/trajectory_issues.py` | `analysis/trajectory_issues.py` | 변경 없음 |
| B `scripts/t2_prefill_stats.py` | `analysis/prefill_stats.py` | 입력을 `turns.csv` 로 |
| B `scripts/t2_public_feedback.py` | `analysis/public_feedback.py` | job 디렉터리 인자 |
| B `scripts/t2_chart.py` + `lhtb_turn_cache.tpl.html` | `analysis/chart.py` + `chart.tpl.html` | 입력을 token_turns CSV 로; 조건·과제·단가가 데이터에서 나옴(템플릿의 하드코딩 제거) |
| B `optlimit/` (cost_model, estimator, controller, dialects, proxy, cli, analysis) | `optlimit/` | 그대로 + `controller.config_from_spec()`(yaml 의 `optlimit:` 블록 → ControllerConfig, prior/prices 참조) + **`harbor_agent.py`** |
| (없음) | `optlimit/harbor_agent.py` | terminus-2 서브클래스. API prompt_tokens 로 압축 시점 결정, native `_summarize()` 호출, `optlimit_decisions.jsonl` |
| (없음) | `analysis/fit_priors.py` | turns.csv → g, L0, Lp, φ, m, γ, summary_out → priors.json |
| (없음) | `analysis/replay.py` | 궤적 리플레이로 nocompact / fixed:L / formula 비용 비교 + 실측 대조 |
| B `tests/` 4개 | `tests/` 6개 | + `test_harbor_agent.py`(훅: fixed 모드 트리거·window 경계·off 모드), `test_token_turns.py` |
| B `README.md`(optlimit 유도식 검토), A `README.md` | `README.md` | A 의 구성을 유지하고 optlimit 절 추가 |
| B `cost_model_derivation2.pdf` | `docs/` | |
| B `runs/` (27 trial), A `jobs/` | 저장소 밖 | 집계 스크립트는 job 디렉터리를 인자로 받으므로 어디 있어도 됨 |

## 동작이 바뀐 곳

1. **압축 트리거.** 전: terminus-2 의 `proactive_summarization_threshold = 한도 − W` (litellm 로컬 토큰 수; Anthropic 은 실제 prompt 가 명목값의 ~1.5배에서 압축). 후: `OptlimitTerminus2` 가 직전 호출의 `prompt_tokens`(API 보고값) ≥ W 에서 native `_summarize()` 호출. `--native-threshold` 로 전 방식도 남겨 둠.
2. **동적 L\*.** 전: proxy 가 usage 조작/오류 응답으로 하네스 압축을 유도(terminus-2 에는 미적용). 후: 같은 컨트롤러를 훅이 직접 호출(`mode: formula`). proxy 는 Codex·Claude Code 용으로 유지.
3. **토큰 집계.** 전(B): venv 안의 litellm 콜백 로그를 시간창으로 턴에 매칭. 후: 패치가 호출마다 trajectory metrics 에 기록 → 결정적. 예전 로그는 `--usage` 로 읽음.
4. **설정.** 전: 조건마다 yaml 복제. 후: `make_config.py` 한 번.

## 검증

- `pytest`: 27 passed (비용 모델·컨트롤러·프록시 기존 18 + 훅 3 + 턴 집계 3; 훅 테스트는 패치된 harbor 를 import).
- 예전 27 trial 을 `token_turns.py --usage` 로 다시 집계한 값이 B 의 `t2_turns.py` 결과와 일치: Sonnet 80K gdal 1032턴·compaction 8회(턴 76/164/298/…)·거절 297회·L_before 77–80K·write_c 9.8K/9.5K/8.9K; sqlite 948턴·6회; Luna 40K gdal 1021턴·16회 등. cache_write 는 모든 main 호출과 요약 호출(24/24)에 채워짐.
- `fit_priors.py` (Sonnet 4 trial, 20 compaction): g 중앙값 409 / 평균 574, L0 0, Lp 9,365, m 2.14, γ 2.10, summary_out 6,857. (Luna 9 trial, 45 compaction): g 717 / 840, L0 1,258, Lp 4,399 (φ 0.027), m 2.18, γ 1.51, summary_out 4,242.
- `replay.py`: 실제로 창 L 로 돌린 궤적을 `fixed:L` 로 리플레이한 턴당 비용이 실측과 2% 이내 (Sonnet 80K gdal 0.01477 vs 0.01501, sqlite 0.01473 vs 0.01502; Luna 40K gdal 0.00076 vs 0.00077, 120K sqlite 0.00192 vs 0.00193 $/턴). formula 정책의 L\* 평균은 Sonnet 44–49K, Luna 23–29K 로 fixed 80K 대비 턴당 7–9% 절감 예측.
- smoke (`configs/smoke/sonnet_l20k_seismo_15min.yaml`, Sonnet, 창 20k, seismo, 15분): 아래 "smoke 결과" 참고.

## smoke 결과

(실행 후 채움)
