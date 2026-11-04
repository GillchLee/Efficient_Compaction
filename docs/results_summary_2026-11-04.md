# 적응형 효율 압축(Adaptive Efficient Compaction) — 결과 정리 (2026-11-04)

모델 GPT-6 Luna(α 10, β 50), 하네스 terminus-2(LHTB/Harbor), 가격 단위 = cache-read 단가.
코드: `optlimit/controller.py`, `optlimit/harbor_agent.py`; 상세 문서: `docs/formulas.md`, `docs/floor_and_handoff_design.md`, `docs/adaptive_limit_plan.md`.

## 1. 질문과 답

| 질문 | 답 |
|---|---|
| 작은 창이 성공률을 보장하지 않더라도, **보장해야 할 최소 크기**는? | 재획득(re-acquisition) 문턱이 정한다. Luna·terminus-2 에서 **L_min ≈ 23K, 창당 턴 K ≥ 14**. 15K(K 8)는 무너지고 25K 부터는 60K 와 같다. |
| 바닥 위에서 **L 은 어떻게 고르나?** | reward 가 L 에 둔감하므로 **비용으로**: L\* = L0 + Lp − g + √(2gC). 과제별 g 에 맞춰 26–39K 를 고르고, 60K 대비 10–12%, 160K 대비 35% 싸다. |
| **Lp(손 넘김 크기)는?** | 늘리지 않는다. 관측값(4–7K)을 그대로 쓴다. 4K → 6K 로 키워도 재획득이 줄지 않고 비용만 7% 올랐다. |
| terminus-2 의 3회 호출(요약·질문·답변)을 **1회로 바꾸면?** | 과제·시간당 5% 절감, 재획득·reward 는 분산 안. 버릴 근거도, 바꿔서 손해볼 근거도 없다. |
| 가격비(α, β)가 바뀌면? | 정책이 α, β 를 그대로 받으므로 `prices.json` 을 바꾸면 다음 결정부터 L\* 가 바뀐다(`prices_reload`). β(출력/읽기)가 L\* 를 지배한다(탄력성 0.38 vs α 0.05). |
| DTAP(balance) 규칙은? | 구현·리플레이 검증 완료(`mode: balance`). 상수 g 에서 L\* 와 같고, 모든 궤적에서 formula 와 ±1%. 버스트에는 늦고 level 규칙이 빠르다. |

## 2. 최종 규칙 (정확도 없이 관측 가능한 양만 사용)

```
입력   α = write/read, β = output/read        ← configs/prices.json (hot reload)
       g  = 턴당 prompt 증가 (온라인 추정)
       Lp = 압축 직후 prompt − cached, O_s = 요약 호출 출력 합, γ = 1 + (α−1)·u   ← learn_* (수축 평균)
       ρ  = 압축당 재획득 턴 (analysis/reacquisition.py; Luna 1.4), ε = 0.1

고정비  C  = (α−1)(Lp−g) + mγ(L0+Lp−g) + β·O_s + ρ·f(L*)
최적    L* = L0 + Lp − g + √(2gC),  K* ≈ √(2C/g)
바닥    K_min = ρ/ε,  L_min = L0 + Lp + g(K_min−1)
결정    L = max(L*, L_min);  prompt + g ≥ L 이면 압축   (balance 모드: Σ(P_i − P_start) + 다음 초과 ≥ C 이고 K ≥ K_min)
```

Luna 실측 사전(`terminus2-gpt-6-luna-1.0`: g 1,275, Lp 5,252, O_s 3,943, m 2.34, γ 2.04)에서 L\* 33.8K, ρ 1.4 포함 37.4K, L_min 24.1K.
C 의 구성: β·O_s 가 87% — 요약 **출력** 토큰이 압축 비용의 대부분이다.

## 3. 실험 A — 창 하한과 손 넘김 (7조건 × 3과제, 2시간, `jobs/luna10c_*`, `jobs/luna10_l60k`)

과제: spot-scheduler-traces, apex-ib244-matter, apex-law433-matter. ρ = 압축당 재획득 턴, 비용은 3과제 합(과제·시간 6).

| 조건 | 창 / K | ρ (ib244 / law433 / spot) | $/과제·h | $/턴 | 요약 비중 | 최종 reward (ib244 / law433 / spot) |
|---|---|---|---|---|---|---|
| l15k stock | 15K / 8 | **5.01 / 2.77** / 0.82 | 0.297 | 0.00094 | 44% | 0.014 / 0.425 / 0.895 |
| l25k stock | 25K / 16 | 1.36 / 1.48 / 0.87 | 0.275 | 0.00084 | 28% | 0.993 / 0.442 / 0.976 |
| l40k stock | 40K / 27 | 1.21 / 1.08 / 0.38 | 0.274 | 0.00088 | 19% | 0.966 / **0.829** / 0.942 |
| l60k stock | 60K / 41 | 1.25 / 1.32 / 0.79 | 0.301 | 0.00099 | 13% | 0.842 / 0.812 / 0.978 |
| formula stock | 26–39K / 20 | 0.96 / 0.70 / 0.35 | 0.266 | 0.00084 | 23% | 0.993 / 0.428 / 0.958 |
| formula single 800w (m 1) | 18–26K / 14 | 0.72 / 1.62 / 0.62 | **0.253** | **0.00067** | 16% | 0.952 / 0.425 / 0.967 |
| formula single 2000w (m 1) | 25–37K / 19 | 1.47 / 1.38 / 0.01 | 0.272 | 0.00076 | 17% | 0.836 / 0.661 / 0.972 |

(law433 의 0.42–0.66 은 제출 없이 끝난 부분 점수. 2시간 안에 제출한 것은 l40k(102분)뿐, 60K 는 3시간 실행에서 75분.)

읽는 법.
1. **ρ 는 K ≥ 13 에서 1 ± 0.5 로 평평하고 K 8 에서만 2.8–5.0 으로 뛴다.** 손실은 창에 비례하지 않고 문턱이다. 15K 는 apex 두 과제에서 2시간 동안 제출이 없었다.
2. **25K–60K 사이 reward 차이는 trial 분산(law433 제출 여부)이 지배**하고 창 크기와 단조 관계가 없다.
3. 비용: formula 가 가장 싸고 고정 25K·40K 가 +3%, 60K +13%, 15K +12%(요약 비용 44%).
4. single 800w 는 턴당 −20% 지만 창이 짧아 시간당 턴이 19% 많다 → 시간 예산 기준 −5%. stock 의 질문·답변이 ρ 를 줄이는 신호는 배치 2 에서만 보였다.
5. 손 넘김을 키운 single 2000w 는 ρ 를 줄이지 못했다(1.47 / 1.38). 1K 추가 비용 ≈ 62K 읽기 단가 vs 재획득 1턴 ≈ 39K: 길이로는 손해.

학습·적합값(온라인 학습이 사전값을 교정): stock Lp 4.3–6.0K, O_s 3.8–5.3K, γ 2.0; single Lp = 1,146 + 0.13·L, m 1.10; single2k Lp = 2,030 + 0.14·L, m 1.07. 1,000턴 넘게 4% 안에서 안정.

## 4. 실험 B — 6과제 3시간 sweep (`jobs/luna10_{l60k,l140k,l160k,formula_nog}`)

formula 는 초기 설정(g 관측만, Lp 3.2K 고정 사전값, 학습·바닥 없음).

| 과제 | l60k | l140k | l160k | formula (L\* / K) |
|---|---|---|---|---|
| apex-ib244 | 0.842 | 0.993 | 0.993 | **0.993** (30K / 15) |
| apex-law433 | 0.812 | 0.849 | 0.849 | 미실행 (docker pull 네트워크 리셋) |
| apex-investment-banking | 0.408 | 0.268 | 0.296 | **0, 제출 없음** (34K / 12) |
| spot-scheduler | 0.978 | 0.977 | 0.978 | 0.974 (25K / 21) |
| 2048 | 0.513 | 0.454 | 0.450 | 0.438 (17K / 34) |
| chess-mate | 0.150 | 0.113 | 0 | 0.175 (18K / 27) |
| **$/과제·h** | 0.317 | 0.417 | 0.441 | **0.285** |
| $/턴 | 0.00080 | 0.00117 | 0.00129 | **0.00069** |
| 요약 비중 / 압축 | 10% / 118 | 4% / 37 | 3% / 29 | 25% / 288 |

- formula 가 60K 대비 −10%, 140K −32%, 160K −35%. 140K·160K 는 압축이 거의 없어도 읽기 비용이 커 가장 비싸다.
- investment-banking formula trial 은 창과 무관하게 루프에 빠진 궤적(기저 반복률 0.81 vs 0.63–0.71)이지만, g 2.1K 로 가장 큰 과제에서 formula 가 고른 K 12 는 K_min 14 **아래** 다. 바닥 규칙이 있었다면 34K → 40K. trial 1개라 원인 분리는 안 된다.
- 2048·chess-mate 는 게임 루프라 어느 조건에서도 제출이 거의 없고, 반복률 지표도 무의미.

## 5. 이전 결과 (LHTB 2.0, gdal/sqlite/seismo)

- formula(g 관측) gdal: L\* ≈ 26K, 48회 압축, 0.00070 $/턴 vs l40k 0.00079, l120k 0.00166, native 0.00601; reward 동일(0.65 = 모든 모델이 못 넘는 숨은 검사 `nodata_bucket`).
- 같은 경과 시간(3h) 총비용: l120k 0.71 < formula 0.75 < l40k 0.81 ≪ native 5.52. 포화는 12분/$0.045.
- 가격 민감도: Luna 에 맞춘 26K 를 Sonnet 5.5 가격에 쓰면 후회 +3%, Fable +11–15%, GLM +44%. Lp 3.2K → 10K 는 L\* 25.9K → 43.6K.

## 5b. 실험 C — LHTB 2.0 과제 2개, formula / native / 80K, 3시간 (`jobs/luna20_*`, 17:20–20:20)

과제별 차이를 보려고 2.0 재구성 과제(duckdb_recursive_hierarchy_report__03, pcap_http2_tls_fingerprint__04)에서 같은 비교를 했다. 채점은 6단계 사다리(reconstruct.py 전 <0.5, 숨은 시나리오 불일치 0.5–0.67, 전부 통과 1.0).

| 조건 | 과제 | 턴 | 압축 | 0.5 도달 | best (시각, $) | final | 1.0 | $/과제·h | $/턴 | ρ |
|---|---|---|---|---|---|---|---|---|---|---|
| formula (L\* 28–30K) | pcap | 1,085 | 39 | 19 분 / $0.07 | 0.621 (152 분, $0.63) | 0.621 | ✗ | 0.252 | 0.00070 | 1.41 |
| native | pcap | 333 | 0 | 4 분 / $0.02 | 0.651 (57 분, $0.21) | 0.651 | ✗ | 0.451 | 0.00394 | – |
| l80k | pcap | 522 | 7 | 2 분 / $0.01 | 0.651 (11 분, $0.05) | 0.591 | ✗ | 0.207 | 0.00119 | 1.41 |
| native | duckdb | 335 | 0 | 2 분 / $0.01 | 0.644 (5 분, $0.02) | 0.640 | ✗ | 0.732 | 0.00655 | – |
| formula | duckdb | 89 | 3 | – | trial 17 분에 사망 | – | – | – | – | – |
| l80k | duckdb | 147 | 1 | 1 분 / $0.01 | 0.663 (28 분, $0.11) | trial 46 분에 사망 | – | – | 0.00129 | 1.45 |

- 결론: **2.0 과제에서는 사다리 단계가 L 과 무관하다.** 살아남은 5 trial 전부 몇 분 만에 0.59–0.66 단계에 오른 뒤 3시간 동안 그 단계에 머물렀고, 1.0 은 없었다. 단계는 모델 능력이 정하고, L 은 그 단계에 머무는 비용만 바꾼다. $/턴 formula 0.00070 < l80k 0.00119 < native 0.0039–0.0066 (1.0 과제와 같은 순서). formula 는 같은 시간에 l80k 의 2.1배 턴을 돌려 $/과제·h 는 비슷(0.25 vs 0.21).
- ρ 는 1.41–1.45 로 1.0 과제와 같다(바닥 K_min 14 위).
- duckdb 의 formula·l80k trial 은 harbor tmux/docker exec 오류("failed to send non-blocking keys: None")로 죽었다(오늘 3회째). 재실행은 사용자 결정.
- reward 가 L 에 반응하는 곡선이 필요하면 1.0 과제(spot-scheduler, apex-ib244)가 맞다. 2.0 에서 쓰려면 Luna 가 1.0 에 도달하는 과제를 골라야 하는데(seismo, sqlite 는 이미 통과, gdal 은 0.65 천장), 그 외 후보는 HF 리더보드 per-task 표로 확인해야 한다.

## 6. 쓰는 법

```yaml
optlimit:
  mode: formula            # 또는 balance
  prior: terminus2-gpt-6-luna-1.0
  prices: gpt-6-luna
  g_prior_weight: 0        # g 는 관측만
  learn_lp: true
  learn_summary_out: true
  learn_gamma: true
  prices_reload: true      # configs/prices.json 변경을 매 결정마다 반영
  reacq_turns: 1.4         # 압축당 재획득 턴 (analysis/reacquisition.py)
  reacq_eps: 0.1           # K_min = 14
handoff: {mode: single, summary_words: 800}   # 선택: 1회 호출 손 넘김 (m 1)
```
예: `configs/examples/luna10_adaptive.yaml`. 분석: `analysis/sweep_summary.py`, `saturation_cost.py`, `reacquisition.py`, `fit_priors.py`, `replay.py --price-shift`, `price_sensitivity.py`.

## 7. 남은 것

- lp10k(Lp ≈ 10K, 3회 호출) 6과제 4조건 실행 중(12:41 시작, ~12시간; l60k·l140k 완료, l160k 진행 중).
- LHTB 2.0 duckdb formula/l80k trial(tmux 오류로 사망) 재실행 여부(사용자 결정).
- formula 미실행 law433 trial, 루프 궤적 investment-banking trial 재실행 여부(사용자 결정).
- ρ 의 온라인 추정(에이전트가 자기 명령 이력의 반복률을 세어 K_min 을 갱신).
- β 가 작은 가격(GLM 류)에서 바닥이 실제로 L 을 올리는지 실험; OpenAI cache-write 과금 조건 확인(prices.json write 0.1 vs 공개표 0.125/0.25).
- trial 1개씩이라 reward 비교는 분산 안: 결론은 비용·ρ 에서 나왔고, reward 는 "나빠지지 않음" 까지만 말할 수 있다.
