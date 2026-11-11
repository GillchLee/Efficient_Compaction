# 현재 상황 정리와 실험 계획 (2026-11-11)

## 1. 주장과 지금까지의 근거

주장: 압축 시점을 비용 모델로 고르면(공식 L\* / balance 규칙) 고정 한도보다 비용이 낮고 reward 는 같다. 모델·하네스·벤치마크가 바뀌어도 가격비(α, β)와 관측값(g, Lp, O_s)만으로 따라간다.

| 축 | 근거 상태 | 강도 |
|---|---|---|
| LHTB, GPT-6 Luna, terminus-2 포크 | formula −10% (60K) ~ −35% (160K), reward 동일; K_min ≈ 14 바닥; 손 넘김 키워도 ρ 안 줄음 | 강함 (docs/results_summary_2026-11-04.md) |
| TB4, Haiku 5.5, terminus-2 포크 (61태스크) | formula $20.0 vs 80K $22.4 (−10%), solved 6 vs 4, 테스트 통과율 0.69 vs 0.65. 단, 턴당 비용은 1.17 vs 1.15 m$ 로 같고 절감은 턴 수 차이에서 나옴. 압축 직후 10턴의 출력이 20–30% 큼 | 중간. native 팔 없음, 고정 L 하나뿐 |
| SWE-Marathon, Haiku, Claude Code 프록시 (4태스크) | balance vs 80K: 호출당 비용 −32~35% (2태스크), 총비용은 궤적 길이가 지배. CC 조기 자가 종료가 실패 원인 | 약함 (n 4, 단일 시드) |
| SWE-Marathon 체인 (진행 중) | t2·CC × balance/30K/80K/native, 11태스크, 3h. 03:57 재시작 | — |
| FrontierSWE v2 | 1h 스모크만 (spice, qubit, git-to-zig) | 없음 |
| Codex | OpenAI 키 만료로 라이브 검증 불가 | 없음 |

약점: (1) Haiku 는 α 12.5·β 50 에 g 가 작아 L\* 가 66–101K 로 나와 limit_max 90K 가 묶임 → "balance vs 80K" 는 거의 null 비교. 정책의 효과는 30K·native 와의 거리에서 봐야 한다. (2) 모든 비교가 태스크당 1 trial. (3) 100K 초과 요금 구간 미모델. (4) 재획득 ρ 가 reward 와 −0.5 상관인데 개입 실험이 없다.

## 2. 실행 중 / 결정 대기

- SWE-Marathon 체인 14잡(~45h, ~$35): 첫 잡 10:00 KST 종료 예정. 완료 시 세션 수 = trial 수 확인.
- 결정 대기: CUA 채점 4태스크(Opus 4.7 검증 비용) 포함 여부; CC native 팔(1M 창, 런당 $1–5) 유지 여부.

## 3. 계획

### P1. SWE-Marathon 분석 (체인 완료 직후, 비용 0)
산출: 하네스별 U자 곡선(30K / balance / 80K / native 의 $/태스크·h, $/턴, partial score), balance 결정 로그(rent, C, limit 추이), 압축당 재획득.
판정: balance 가 고정 팔 중 최저 비용 ±5% 이내이고 partial score 가 떨어지지 않으면 성공.

### P2. 가격비 이동 검증 — Sonnet 5.5 TB4 (~$300, 2일)
α 25·β 100 으로 L\* 가 62K 로 내려오는지, 비용 절감이 Haiku 보다 큰지. 20태스크 × {balance, 90K}. 선행: prices.json 100K 초과 구간, max_tokens 32768, 1–2태스크 스모크로 balance 결정 확인. 포크 terminus-2, HB_NO_HUMAN_NOTE+HB_RESUBMIT_GUARD 만.

### P3. 세 번째 벤치마크 — FrontierSWE v2 CPU (~$45, 4일)
21태스크 중 메모리 128GB 2개 제외. terminus-2-proxy 먼저 {balance, 30K, 80K} 3팔, 3h. CC 는 P1 에서 CC 결과가 의미 있을 때만.

### P4. 분산 — 헤드라인 비교 재시드 (~$15)
P1 에서 가장 큰 차이가 난 비교(balance vs 최저 고정)를 11태스크 × 2시드로 반복. 태스크 쌍 Wilcoxon + 부트스트랩 CI.

### P5. 기제 — 재획득 개입 (~$40)
files-of-record 다이제스트 패치(요약에 2창 이상 읽은 파일의 경로·크기·마지막 읽은 턴·변경 여부·발췌 ≤2K) + "파일을 다시 확인하라" 헤지 제거. TB4 12태스크(Haiku formula) + LHTB 3태스크(Luna) A/B. 지표: 압축당 재획득, ρ, reward.

### P6. Codex 팔 (키 갱신 후, ~$20)
SWE-Marathon 11태스크 × {balance, 80K}.

### 코드 선행 작업
prices.json >100K 구간; note_summary_usage 에 cache_write 반영; terminus-2-proxy 의 model_info 기반 native 고정 L 팔; run_harness_job 종료 시 세션 수 ≠ trial 수면 경고; proxy_turns 세션→태스크명 매핑.

우선순위: P1 → 코드 선행 → P2 → P3 → P4 → P5 → P6.
