# 적응형 compaction 한도 — 준비 문서 (2026-11-04)

목표: 가격비 **α = write/read**, **β = output/read** 가 바뀌어도(모델 교체, 캐시 TTL 변경, 공급자 가격 개정, 캐시 적중률 변화) 압축 한도 L\* 와 정책이 따라 움직이게 한다. 이 문서는 (1) 문헌·가격 데이터, (2) 민감도 분석, (3) 구현(완료분), (4) 실험 계획을 담는다.

## 1. 식과 적응 변수

`optlimit/cost_model.py` (docs/cost_model_derivation2.pdf + 요약 호출 항):

    f(L) = (L0 + Lp + L)/2 + (α−1)(g + (Lp−g)/K) + (mγL + β·O_s)/K,   K = (L − L0 − Lp)/g + 1
    L*   = L0 + Lp − g + √(2 g C),   C = (α−1)(Lp−g) + mγ(L0+Lp−g) + β·O_s

C 는 "압축 한 번의 고정비(캐시 읽기 단가 단위)" 이고 L\* 는 그것과 "창 안에서 매 턴 다시 읽는 비용" 의 균형점이다. 가격이 들어가는 자리는 α(prefill 재작성, 요약 읽기의 γ = 1 + (α−1)·u), β(요약 출력) 둘뿐이므로, 적응이란 **C 의 세 항을 현재 가격과 현재 관측값으로 유지**하는 것이다.

| 항 | 가격 의존 | 관측으로 갱신 가능 | 현재 구현 |
|---|---|---|---|
| (α−1)(Lp−g) | α | Lp: 창 시작 prompt − cached; g: 증분 | `learn_lp`, g 추정기 |
| mγ(L0+Lp−g) | α (γ 를 통해) | u = 요약 호출 입력 중 cache miss 비율 → γ | `learn_gamma` |
| β·O_s | β | O_s: 요약 호출 completion 합 | `learn_summary_out` |
| α, β 자체 | 가격표 | prices.json 변경 → `update_prices` | `prices_reload` |

## 2. 가격 지형 (2026-11-04 공개 가격, $/MTok)

출처: [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [Gemini pricing](https://aistudio.google.com/docs/pricing). `analysis/price_sensitivity.py` 의 `PRICE_SHEETS` 에 같은 값.

| 공급자 / 모델 | read | write | output | α | β | 비고 |
|---|---|---|---|---|---|---|
| Anthropic Fable 5.1 | 0.25 | 12.5 (5m) / 20 (1h) | 50 | 50 / 80 | 200 | read 0.025× |
| Anthropic Opus 5.5, Sonnet 5.5 | 0.20 / 0.10 | 5 / 2.5 (5m), 8 / 4 (1h) | 20 / 10 | 25 / 40 | 100 | read 0.05× |
| Anthropic Sonnet 5, 4.6, Haiku 5.5 | 0.1× input | 1.25× (5m) / 2× (1h) | 5× | 12.5 / 20 | 50 | 표준 0.1× |
| OpenAI gpt-6-luna | 0.01 | 0.10 (input) / 0.125 (30m write) / 0.25 (12h) | 0.5 | 10 / 12.5 / 25 | 50 | write 과금 조건은 문서에 없음 |
| OpenAI gpt-6.1-sol | 0.10 | 2.5 (30m) / 5 (12h) | 10 | 25 / 50 | 100 | read 0.05× |
| Google Gemini 3.8 Flash | 0.075 | 0.75 | 3.75 | 10 | 50 | + 저장료 0.5/MTok·h |
| Zhipu GLM-5.3-flash (OpenRouter) | 0.03 | 0.0457 | 0.0742 | 1.5 | 2.5 | 캐시 할인 거의 없음 |

요점: 같은 모델 안에서도 **캐시 TTL 하나로 α 가 1.6–2배** 바뀌고, 공급자 간에는 α 1.5–80, β 2.5–200 이다. OpenAI 는 GPT-6 세대에 write 가격을 도입했는데(luna 0.125/0.25) 자동 적용인지 명시가 없다. 지금 `configs/prices.json` 의 luna 는 write 0.1 로 두었고, 실제 청구가 0.125 면 기록 비용이 write 토큰에 한해 25% 과소다. 확인 항목으로 남긴다.

## 3. 민감도 (`jobs/price_sensitivity_luna.md`, prior terminus2-gpt-6-luna, g 1,021)

- **β 가 지배한다.** 기준점(α 10, β 50)에서 탄력성 d ln L\*/d ln x: α 0.05, β 0.38, g 0.37. C 의 분해가 prefill 19.6K + 요약 읽기 11.3K + 요약 출력 212K 이기 때문이다(β·O_s = 50 × 4,242). α 가 10 → 50 으로 5배 뛰어도 L\* 는 25.7K → 30.1K 에 그친다.
- **공급자별 L\*** (같은 하네스·같은 Lp 기준): Luna/Gemini 25.7K, Sonnet 5/Haiku 5.5 26.0K, Sonnet 5.5/Opus 5.5/gpt-6.1-sol 35.2K(1h: 36.4K), Fable 5.1 48.3K(1h: 50.0K), GLM 9.7K.
- **고정 창의 후회.** Luna 에 맞춘 26K 창을 그대로 쓰면 Sonnet 5.5 가격(α 25, β 100)에서 +3.1%, Fable(β 200)에서 +11–15%, GLM(β 2.5)에서 +44% 의 턴당 비용 손실. 즉 적응이 돈이 되는 경우는 β 가 크게 바뀔 때(모델 교체·읽기 할인율 변경)이고, TTL 변경(α 만)은 1% 안쪽이다.
- **Lp 변화가 가격 변화보다 크다.** Lp 3.2K → 10K(요약 길이 지시) 는 L\* 25.9K → 43.6K 로, 어떤 가격 변동보다 큰 이동이다. 그래서 `learn_lp`/`learn_summary_out` 이 가격 적응과 같은 비중으로 필요하다.
- **리플레이로 확인** (`analysis/replay.py --price-shift 300:<row>`, Luna gdal 1,078턴): 300턴에 가격이 Sonnet 5.5 비율로 바뀌면 fixed 26K 0.00837 vs 적응 0.00828 $/턴(−1%); GLM 비율로 바뀌면 fixed 26K 0.00067 vs 적응 0.00054(−19%, 압축 46 → 133회). 짧은 trial(seismo 9턴)에서는 적응 정책이 2회 압축해 비용이 올라가므로(β 가 작아 L\* 9.7K) `min_window_turns`/`limit_min`/`horizon` 바닥이 필요하다.

## 4. 문헌

압축 **시점**을 가격의 함수로 닫힌 꼴로 푼 논문은 찾지 못했다. 가까운 것들:

- [Beyond Token Savings: A Systematic Study of Context Compression in LLM Agents](https://arxiv.org/abs/2609.32961) (2026-09): SWE-bench Verified·Terminal-Bench, ~35K run. 임계값(모델·벤치마크별 피크 문맥의 5/15/25 백분위, 예: Qwen SWE-bench 10K/15K/20K)과 깊이 D 를 sweep. 임계값 10K → 20K 에서 해결률 50.7 → 62.7%, 입력 비용 1.10× → 0.92×, 압축 4.85 → 1.15회/시도. 비용은 cached 0.1× 가정 + 요약 입력은 fresh 로 계산(출력 제외). "임계값은 워크로드·모델별로 보정하라" 가 결론이고 적응 규칙은 제안하지 않는다. 우리 식은 이 보정을 가격·g·Lp 로 계산하는 셈이다.
- [The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization](https://arxiv.org/abs/2508.21433) (NeurIPS 2025): 관측 마스킹(창 10턴) vs 요약(31턴마다, 최근 10턴 유지). 비용 −50% 대, 요약 호출 비용을 빼면 두 방식 차이가 거의 없음. 요약 호출은 캐시를 거의 못 쓴다는 지적 = 우리 m·γ 항. 하이브리드(마스킹 후 43턴에 요약) 가 7–11% 추가 절감.
- [What Does Context Compression Cost an Agent?](https://arxiv.org/abs/2608.16370) (2026-08): 압축 후 **재획득 호출**(retrieval) 이 21 → 64 로 느는 등, 완료율에 안 보이는 상호작용 비용. 우리 "압축 뒤 턴 수 증가" 와 같은 현상이며, 총비용 비교 때 턴당 비용만 보면 안 되는 이유.
- [Context Compaction Theory](https://arxiv.org/abs/2608.01326) (2026-08): 압축을 one-way 통신 복잡도로 형식화. 요약 예산의 하한. 가격은 다루지 않지만 "요약(generation) 이 선택(selection) 보다 적은 예산으로 되는 질의가 있다" 는 결과는 Lp 를 줄일 여지의 이론적 근거.
- 방법론 계열: [ACON](https://arxiv.org/abs/2510.00615)(요약 프롬프트 최적화), [Context-Folding](https://arxiv.org/abs/2510.11967)(하위 궤적 접기, 활성 문맥 10× 축소), [ACM](https://arxiv.org/abs/2607.23809)(에이전트가 압축 시점을 스스로 결정 + 외부 메모리), ReSum(2509.13313), MEM1(2506.15841), AgentFold(2510.24699). 모두 "무엇을 남길지" 이고 "언제" 는 고정 규칙이거나 학습된 행동이다. 우리 정책은 이들의 요약기와 직교하므로 어떤 요약기에도 Lp·O_s 만 다시 재면 적용된다.
- 캐시 경제: [Anthropic prompt caching 문서](https://platform.claude.com/docs/en/about-claude/pricing) 의 손익분기(5m 쓰기 = 읽기 1회, 1h = 2회) 와 블로그들의 "write/read 비율 진단" 은 우리 γ 추정(요약 호출 cache miss 비율)과 같은 양이다.

## 5. 구현 (이 커밋)

- `optlimit/controller.py`: `update_prices(alpha, beta, note)`; `note_summary_usage()`; `learn_lp / learn_summary_out / learn_gamma / learn_weight`(창 시작마다 관측을 사전값과 가중 평균); `Decision` 에 alpha/beta/gamma/Lp/summary_out 기록; `load_prices()`.
- `optlimit/harbor_agent.py`: `prices_reload: true` 면 결정마다 `configs/prices.json` 의 mtime 을 보고 바뀌면 행을 다시 읽어 `update_prices` (결정 로그에 `prices_reloaded`); `_update_subagent_metrics` 를 가로채 요약 호출 usage 를 컨트롤러에 전달.
- `analysis/replay.py --price-shift TURN:ROW`: 궤적 중간에 가격이 바뀌는 상황을 재생(고정 창은 그대로, formula 는 적응).
- `analysis/price_sensitivity.py`: 공개 가격표 → (α, β, γ, L\*, K), α×β 격자, 고정 창 후회, 탄력성.
- `configs/examples/luna10_adaptive.yaml`: formula + g 관측 + learn 3종 + prices_reload 예시.
- 테스트 24개(가격 갱신이 L\* 를 키우고 기록되는지, 학습 항이 관측을 따라가는지, 파일 변경이 에이전트 결정에 반영되는지).

## 6. 실험 계획

1. **가격 교체 리플레이(완료, 3절)** — 기록 궤적 전부(Luna 9 trial, Sonnet 4 trial)에 대해 α×β 격자의 후회표. 결과는 `jobs/price_sensitivity_*.md`.
2. **학습 항 라이브 검증** — `configs/examples/luna10_adaptive.yaml` 로 spot-scheduler·apex-ib244 를 돌려 결정 로그의 Lp/summary_out/gamma 가 `fit_priors.py` 사후 적합값으로 수렴하는지(목표: 5회 압축 안에 ±15%). lp10k 설정과 함께 돌리면 "투영값 10K vs 실측" 차이를 정책이 흡수하는지도 본다.
3. **가격 변동 라이브** — 한 trial 도중 `configs/prices.json` 의 luna 행을 Sonnet 5.5 비율로 바꿔(실제 과금은 그대로) 결정 로그에서 L\* 가 25.9K → 35K 대로 옮겨 가고 압축 간격이 늘어나는지 확인. 실제 비용은 리플레이로 평가.
4. **β 가 작은 영역의 바닥** — GLM 류(β 2.5)에서 L\* 9.7K·8턴마다 압축은 품질에 해롭다. `min_window_turns` 를 10–15 로, 또는 압축당 품질 손실을 비용에 더하는 항(2608.16370 의 재획득 호출 수 × 턴 비용)을 C 에 추가하는 안을 리플레이로 비교.
5. **모델 간 전이** — Sonnet 5.5 실측(α 25, β 100)에서 Luna 사전값 + 가격만 바꾼 L\*(35.2K) 와 Sonnet 자체 적합 L\*(44–49K, Lp 9.4K) 의 차이를 분해: 가격이 설명하는 몫 vs Lp·O_s 차이가 설명하는 몫.

## 7. 열린 질문

- OpenAI write 과금 조건(자동/명시 캐시) — 청구서 또는 usage 의 `cache_creation` 필드로 확인.
- Gemini 의 캐시 저장료($/MTok·h)는 식에 없다. 창이 길수록 저장량이 커지므로 f(L) 에 시간항을 추가해야 한다.
- Anthropic 1h 캐시는 α 를 1.6배 올리지만 턴 간격이 5분을 넘는 작업에서만 의미가 있다. TTL 선택 자체를 정책에 넣을 수 있다(α 후보 둘 중 f 가 작은 쪽).
