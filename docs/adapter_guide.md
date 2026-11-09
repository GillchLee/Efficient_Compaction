# 다른 harness / 다른 API 에 optlimit 붙이기

압축 시점 결정은 `optlimit/controller.py` 하나에 있고 표준 라이브러리만 씁니다. harness 가 할 일은
네 가지뿐이고, 그 네 가지를 모아 둔 것이 `optlimit/adapters/` 입니다.

| 단계 | 컨트롤러 호출 | harness 에서 가져올 것 |
|---|---|---|
| 1. 모델 호출이 끝날 때마다 | `d = ctl.on_prompt("main", "main", prompt, cached=cached, window_start_hint=boundary)` | 그 호출의 **API usage** (`optlimit.usage.normalize_usage` 가 Anthropic / OpenAI / vLLM / harbor 형식을 통일) |
| 2. `d.compact_now` 이면 | harness 의 요약(압축)을 실행하고 요약 호출마다 `ctl.note_summary_usage("main", prompt, cached, completion)` | harness 의 압축 함수, 요약 호출의 usage |
| 3. 압축 직후 | `ctl.note_kind("main", "summary")` 와 다음 `on_prompt(..., window_start_hint=True)` | 없음 (경계 표시) |
| 4. 시작 시 | `config_from_spec({"mode": "formula", "prior": ..., "prices": ...})` 또는 `ctl.update_prices(alpha, beta)` | 단가 비율 α = write/read, β = output/read (`configs/prices.json`) |

`d` 는 `Decision` 이고 `limit`(지금의 L\*), `g_hat`, `window_idx`, `reason` 등을 담으니 그대로 로그에 쓰면
`analysis/token_turns.py` 와 같은 형식(`optlimit_decisions.jsonl`)이 됩니다.

## 참조 구현 두 개

- `optlimit/adapters/chat_loop.py` — 100 줄짜리 일반 채팅 루프. `call(messages) -> (text, usage)` 와
  `summarize(messages) -> (new_messages, [usage])` 만 주입하면 되므로 OpenAI 호환 클라이언트든
  Anthropic SDK 든 그대로 감쌀 수 있습니다. `tests/test_chat_loop.py` 가 가짜 모델로 계약을 검증합니다.
- `optlimit/adapters/harbor_terminus2.py` — Harbor terminus-2 어댑터(실험에 쓴 것). terminus-2 가 매
  에피소드 시작에 부르는 `_check_proactive_summarization()` 하나를 오버라이드해 1–3 단계를 하고,
  압축은 terminus-2 자신의 `_summarize()` 를 부릅니다. 손 넘김 프롬프트 변형은
  `terminus2_handoff.py`(mixin) 로 분리돼 있습니다. `optlimit.harbor_agent` 는 같은 클래스를 내보내는 shim 입니다.

## 새 harness 에 붙일 때 확인할 것

1. **usage 를 어디서 얻나.** 대부분의 harness 는 응답 객체에서 usage 를 버립니다. Harbor 는 그래서
   `patches/lhtb-7ca0cac/01-usage-tracking.patch` 로 `Chat.last_usage` 를 추가했습니다. 다른 harness 에서는 클라이언트
   래퍼에서 마지막 응답의 usage 를 보관하면 됩니다.
2. **cache read 가 보고되나.** Anthropic `cache_read_input_tokens`, OpenAI/vLLM
   `prompt_tokens_details.cached_tokens`. vLLM 은 `--enable-prefix-caching` 일 때만 옵니다. 없으면
   cached = 0 으로 넣어도 결정은 되지만(prompt 만 쓰므로) Lp·γ 학습은 꺼 두세요.
3. **압축이 몇 번의 호출인가 (m).** terminus-2 는 3회(요약·질문·답)라 m ≈ 2.2. 한 번이면 m = 1 로 prior 를
   잡습니다 (`configs/priors.json` 의 `-single` 행처럼).
4. **prior.** g, Lp, L0, m, γ, O_s 는 모델×harness 마다 다릅니다. 처음에는 비슷한 행과
   `g_prior_weight=0`(g 는 관측만)으로 한 번 돌리고 `analysis/fit_priors.py` 로 행을 만드세요.
5. **단가.** 달러가 없는 로컬 모델은 시간을 단가로 씁니다 (`scripts/measure_vllm_prices.py`). 수식은 비율만 쓰므로 단위는 상관없습니다.
