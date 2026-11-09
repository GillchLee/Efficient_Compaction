# optlimit 을 terminus-2 밖의 압축 방식에 붙이기 — Claude Code · Codex · API 서버 측 압축

목표: `optlimit` 의 압축 **시점** 최적화(비용 모델 f(L), formula L\*, balance 규칙)를 terminus-2 의
in-process 훅 없이도 쓰게 한다. 기존 코드(컨트롤러·비용 모델·terminus-2 어댑터)는 바꾸지 않고,
하네스 바깥에서 붙는 어댑터만 더한다.

## 1. 압축 방식 조사

### 1.1 Claude Code (CLI, Agent SDK)

출처: [context-window](https://code.claude.com/docs/en/context-window.md),
[model-config](https://code.claude.com/docs/en/model-config.md),
[env-vars](https://code.claude.com/docs/en/env-vars.md),
[hooks](https://code.claude.com/docs/en/hooks.md),
[prompt-caching](https://code.claude.com/docs/en/prompt-caching.md),
[headless](https://code.claude.com/docs/en/headless.md) (2026-11-09 확인).

| 항목 | 내용 |
|---|---|
| 자동 압축 시점 | 기본은 모델의 창 한계에서(네이티브 1M 모델은 약 967K, 1M 비활성·인식 못 한 모델 id 는 200K). **스모크 결과(2.1.319): API 가 보고한 usage 로는 압축하지 않음** — 950K 를 보고해도 같은 실행 안의 다음 호출을 그대로 보냄(자체 추정을 쓰는 것으로 보임). 반면 `prompt is too long` 400 에는 **반응형 압축**을 실행 |
| 정적 손잡이 | `CLAUDE_CODE_AUTO_COMPACT_WINDOW`(100K–1M, 가장 우선), `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`(1–100, 창의 %로 **낮추기만**), `--autocompact <tokens>`, `autoCompactWindow`/`modelSettings.<m>.autoCompactWindow`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `DISABLE_COMPACT` |
| 세션 중 변경 | `/autocompact <size>` 만 즉시 반영(대화형). 설정 파일·환경변수의 중간 변경은 미확인 |
| 압축 내용 | 요약 요청 **1회**(시스템 프롬프트·도구·이력을 다시 보내고 요약 지시로 끝남 → m = 1). 캐시가 살아 있으면 접두를 캐시에서 읽음(γ ≈ 1), TTL(기본 5분)이 지나면 다시 처리(γ → α) |
| 압축 뒤 문맥 | 요약 + 시스템 프롬프트 + CLAUDE.md·메모리 재로딩 + git 상태 + 최근 수정 파일 최대 5개(각 5K 토큰 이하) + 스킬 본문(최대 25K) → **Lp 가 크다**(Harbor Hub 궤적에서 새로 쓰는 부분 ~15K) |
| 관측 | `--output-format stream-json` 의 호출별 `message.usage`(input / cache_read / cache_creation / output; output 은 자리표시), 마지막 `result.usage`·`modelUsage`(압축 호출 포함 여부 미확인) |
| 훅 | `PreCompact`(matcher manual/auto, **exit 2 로 자동 압축을 막을 수 있음**), `PostCompact`(요약 텍스트, 관측만), `SessionStart` matcher `compact`. 훅이 압축을 **일으키는** 방법은 문서에 없음 |
| 수동 압축 | `/compact [지시]`. `-p`·stream-json 입력·SDK 에서 `/compact` 를 보낼 수 있는지는 미확인 |
| 요약 요청의 모양(2.1.319) | 마지막 user 메시지(도구 결과) **뒤에** "CRITICAL: Respond with TEXT ONLY… <analysis>… <summary>…\n\nYour task is to create a detailed summary of the conversation so far…" 를 덧붙임 → 접두가 아니라 포함 여부로 인식해야 함 |
| 폭주 방지 | 압축 뒤 3턴 안에 다시 한계에 닿는 일이 3번 연속이면 "Autocompact is thrashing" 으로 실행을 끝냄 → 창은 4턴 이상 유지 |

### 1.2 Codex CLI

출처: `openai/codex` 소스(commit 8703d32, 2026-11-09; 경로는 `codex-rs/` 기준),
[config reference](https://learn.chatgpt.com/docs/config-file/config-reference),
[issue #16033](https://github.com/openai/codex/issues/16033).

| 항목 | 내용 |
|---|---|
| 자동 압축 시점 | `context_manager/history.rs` `get_total_token_usage()`: 마지막 `response.completed` 의 `total_tokens`(= input + output, input 에 cached 포함) + 그 뒤 기록된 항목의 로컬 추정. 이것이 `min(model_auto_compact_token_limit, 창 × 0.9)` 이상이면 압축(`openai_models.rs:579`). 창의 95% 는 하드 상한 |
| 검사 위치 | `session/turn.rs`: 턴 시작 전(`run_pre_sampling_compact`)과, 도구 호출이 남은 샘플링 직후(`needs_follow_up`). 턴 끝 검사는 `model_post_turn_compact_threshold_percent > 0` 일 때만 |
| 정적 손잡이 | `model_auto_compact_token_limit`(스레드 시작/재개 때 고정, `-c` 로 줄 수 있음), `model_auto_compact_token_limit_scope = total \| body_after_prefix`(압축 직후 기준선 위의 증가만 셈; 90% 클램프 없음), `compact_prompt`, `model_providers.<id>.capabilities.remote_compaction` |
| 세션 중 변경 | 불가(config reload 가 이 값을 다시 읽지 않음, `turn/start` 에 필드 없음) |
| 로컬 압축(사용자 정의 provider 기본) | 요약 요청 **1회**(m = 1): 전체 이력 + "You are performing a CONTEXT CHECKPOINT COMPACTION…" 프롬프트, **도구 목록 없이** 보내므로 캐시 접두가 어긋날 수 있음(γ > 1 가능 → `learn_gamma`). 새 이력 = 초기 문맥 + 최근 user 메시지 최대 20K + 요약("Another language model started to solve this problem…") |
| 원격 압축 v2(OpenAI 기본 provider) | 일반 `/responses` 요청에 `{"type":"compaction_trigger"}` 항목을 붙여 암호화된 `compaction` 항목을 받음. 이력에 메시지 최대 64K 유지. 그 호출의 usage 는 rollout `token_usage_record` 에만 |
| 외부에서 압축 | app-server JSON-RPC `thread/compact/start {threadId}`(턴 사이에서만; 턴 중이면 그 턴을 중단). `codex exec` 에는 압축 플래그 없음 |
| 관측 | `codex exec --json` 의 `turn.completed.usage` 는 **스레드 누적값**. 호출별 값은 rollout `~/.codex/sessions/**/rollout-*.jsonl` 의 `token_usage_record`(압축 호출 포함), `compacted` 항목이 압축 표시. app-server 는 `rawResponse/completed` |
| 훅 | `PreCompact`/`PostCompact` (`continue:false` 는 압축 건너뛰기가 아니라 턴 중단) |
| 버그 | #16033(열림): 문맥 초과 뒤 `fill_to_context_window` 가 `total_tokens` 를 0 근처로 만들어 자동 압축이 다시 안 걸림; `model_context_window` 를 설정하면 깨진다는 보고(#16068). 그래서 어댑터는 창 크기를 기본으로 설정하지 않는다 |

Harbor 의 codex 에이전트는 `OPENAI_BASE_URL` 이 있으면 사용자 정의 provider(`wire_api = responses`)를
만들므로 프록시 아래의 Codex 는 **로컬 압축**을 쓴다(압축 호출이 프록시를 지나가 관측된다).

### 1.3 API 서버 측 압축 (하네스 무관)

[Anthropic compaction](https://platform.claude.com/docs/en/build-with-claude/compaction):

- 임계 방식: `anthropic-beta: compact-2026-01-12`, `context_management.edits[] = {type: "compact_20260112", trigger: {type: "input_tokens", value: N}, pause_after_compaction, instructions}`. N 은 **요청마다** 정할 수 있음(최소 50K, 기본 150K). 요약 비용은 `usage.iterations` 의 `compaction` 항목(최상위 usage 에는 빠짐).
- 요청형: `anthropic-beta: compact-2026-09-04`, 최상위 `compaction: {type: "summarize"}` — 한 번 요약해서 `compaction` 블록만 돌려줌(m = 1, 요약 출력 = O_s).
- 문맥 편집(요약 아님): `clear_tool_uses_20250919`(trigger·keep·clear_at_least) — 캐시를 깨므로 α 항이 커짐.

## 2. 무엇을 바꿀 수 있나 — optlimit 의 네 계약을 어디에 붙이나

`docs/adapter_guide.md` 의 계약: (1) 호출마다 usage → `on_prompt`, (2) `compact_now` 이면 하네스의 압축,
(3) 압축 호출 usage → `note_summary_usage` + 창 경계, (4) 단가 α, β.

| 하네스 | (1) usage | (2) 압축 일으키기 | (3) 압축 호출 인식 | 정적 L |
|---|---|---|---|---|
| terminus-2 | `Chat.last_usage`(패치 01) | `_summarize()` 직접 호출 | 서브에이전트 usage | `proactive_summarization_threshold` |
| Claude Code | 프록시가 응답 usage 를 읽음 | **트리거 error**: 결정 다음의 main 요청에 `prompt is too long` 400 → 반응형 압축(usage 트리거는 듣지 않음) | 마지막 user 메시지에 "Your task is to create a detailed summary…" 포함 | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` × `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` |
| Codex | 프록시(Responses SSE 의 `response.completed.usage`) | **트리거 usage**(보고 `total_tokens` = 258K ≥ 자체 임계값 200K) | "You are performing a CONTEXT CHECKPOINT COMPACTION", `x-codex-window-id` 헤더 변화 | `model_auto_compact_token_limit` |
| Anthropic API 서버 측 | 응답 usage | `trigger.value` 를 요청마다 L\* 로 | `usage.iterations[compaction]` | 고정 `trigger.value` |

### 트리거 usage 의 원리 (프록시)

Codex 는 "직전 응답의 prompt 크기 ≥ 자기 임계값" 이면 다음 요청 전에 스스로 압축한다(Claude Code 는 그렇지 않아 trigger error 를 쓴다; 스모크 결과 참고). 프록시는 실제 usage 를
기록·컨트롤러에 넘긴 뒤 하네스에게는 **min(P, low_cap)** 을 보고하고, 컨트롤러가 압축을 결정한 뒤에는(창이
`min_window_turns` 를 채운 때부터) 하네스가 실제로 압축할 때까지 **high** 를 보고한다. 하네스 자체 임계값은
low_cap < 임계값 < high 로 둔다. 트리거 error 는 같은 시점에 다음 main 요청 하나를 `prompt is too long` 400 으로
돌려준다(하네스의 반응형 압축; 그 압축 요청은 통과). 그러면

- 하네스는 컨트롤러가 고른 그 턴에만, 자기 압축 코드(요약 프롬프트·재첨부·창 재구성)로 압축하고,
- 비용은 프록시 로그(실제 usage)로 계산되며, 하네스 내부 상태(보이는 "남은 문맥")만 바뀐다.

실제 prompt 는 하네스에 숨겨지므로 L\* 상한(`limit_max`)을 하네스 임계값과 API 문맥 한계 아래로 둔다.

## 3. balance 규칙을 쓰는 이유 (formula 가 어려운 경우)

formula 는 L\* = L0 + Lp − g + √(2gC) 로 g(턴당 증가) 추정이 필요하다. Claude Code·Codex 는

- 한 "턴"이 도구 호출 여러 번이고 병렬 도구·서브에이전트가 섞여 g 가 울퉁불퉁하며,
- 압축 뒤 Lp 가 재첨부 파일에 따라 크게 달라진다(Claude Code).

balance(DTAP) 규칙은 **g 없이** 창 안에서 실제로 다시 읽은 토큰(집세)이 압축 1회 비용 C 에 이르면 압축한다:

    rent_t = Σ_{i≤t} (P_i − P_start),   압축 ⇔ rent_t + (P_t − P_start) ≥ C,   단 K ≥ K_min

C = (α−1)(Lp−g) + mγ(L0+Lp−g) + β·O_s 는 하네스의 **압축 1회 값**이라 압축을 몇 번 관측하면(`learn_lp`,
`learn_summary_out`, `learn_gamma`) 그 하네스·모델 값으로 수렴한다. 성장열과 무관하게 오프라인 최적의 2배 이내
(Dooly–Goldman–Scott 2001). 그래서 이 문서의 실험은 **balance 를 주 조건**으로 하고 formula 는 보조로 둔다.

사전값(`configs/priors.json`, Harbor Hub 공개 궤적에서 적합): `claude-code-hub`(L0 16.9K, Lp 15K, m 1, O_s 4.4K),
`codex-hub`(L0 11.8K, Lp 4.5K, O_s 4.5K), `codex-luna-hub`(L0 12K, Lp 2.9K). 이 값과 단가로 계산한 기준점:

| 하네스 · 단가 | α | β | C (읽기 단가 단위) | L\* | 기본 압축 시점 |
|---|---|---|---|---|---|
| Claude Code · Sonnet 5.5 | 25 | 100 | 779K | 87K | ~967K (1M 창) |
| Codex · GPT-6 luna (codex-hub) | 10 | 50 | 263K | 46K | ~245K (272K × 0.9) |
| Codex · GPT-6 luna (codex-luna-hub) | 10 | 50 | 257K | 35K | ~245K |

두 하네스 모두 기본 시점이 비용 최적보다 3–10배 늦다. 이 차이가 실험에서 볼 효과다.

## 4. 구현 (추가 파일만)

| 파일 | 역할 |
|---|---|
| `optlimit/proxy.py`, `optlimit/dialects.py` | 투명 프록시(Anthropic Messages / OpenAI Chat / Responses, 스트리밍 포함): usage 기록, 컨트롤러, 트리거 usage·error·off. 리팩터 전 구현(cbabe80)을 되살리고 (a) 오류 트리거의 연속 거부 처리를 프록시 안으로, (b) 압축 호출 usage → `note_summary_usage`(balance 의 C 학습), (c) OpenAI `cache_write_tokens` 반영, (d) 요약 요청을 접두가 아니라 포함 여부로도 인식(Claude Code 2.1.3xx), (e) 최소 창 가드(`min_window_turns`)와 압축 대기 중 신호 유지 |
| `optlimit/serve.py` | `python -m optlimit.serve --trigger usage --spec-file … --log …` (`--model-rewrite`: Harbor 의 claude-code 는 사용자 정의 base URL 이면 `anthropic/<id>` 를 그대로 보냄) |
| (프록시) API 서버 측 압축 | 요청이 이미 `context_management` 의 `compact_*` trigger(Anthropic) 나 `compaction` `compact_threshold`(OpenAI Responses) 를 쓰면 그 값을 매 요청 컨트롤러의 L\*(≥ 50K) 로 바꾸고, 응답 `usage.iterations` 의 `compaction` 항목을 요약 비용으로 학습. 그런 요청이 아니면 아무것도 안 함 |
| `optlimit/adapters/harbor_installed.py` | `OptlimitClaudeCode`, `OptlimitCodex`: `compaction:` kwargs 로 자체 임계값 설정, `resume_after_verifier_rejection()` 으로 같은 대화 이어가기(terminus-2 실험과 같은 프로토콜) |
| `optlimit/harnesses.py` | 하네스 프로필(에이전트 클래스, 트리거, 보고 band, 최소 창, 자체 임계값 knob, 요약 요청 표지, 기본 prior). 하네스별 상수는 여기에만 |
| `configs/make_harness_config.py` (`make_config.py --harness` 가 위임) | `--limit N\|native`(정적), `--proxy-fixed N`(대조: 같은 L 을 프록시로), `--balance/--formula PRIOR --prices ROW [--learn]`. 하네스별 기본: Claude Code trigger error·최소 창 4, Codex trigger usage·최소 창 3 |
| `scripts/run_harness_job.sh` | `run_job.sh` 가 `# proxy:` 줄을 보면 자동으로 부르는 내부 도우미: 프록시를 띄우고 하네스를 가리키게 한 뒤 `run_job.sh` 로 돌아감 |
| `analysis/proxy_turns.py` | 프록시 로그 → 호출별·세션별 표(실제 prompt / cache read·write / output / 비용, 결정, 압축 수). `--job-dir` 이면 `token_turns/{turns,summary}.csv` 형식도 써서 `replay.py`·`prefill_stats.py`·`fit_priors.py` 를 그대로 쓴다 |
| `scripts/fake_llm_upstream.py` | 키·비용 없이 트리거 경로를 확인하는 가짜 Anthropic/Responses 서버(짧은 답 + 큰 usage). `OPTLIMIT_*_UPSTREAM` 으로 프록시 뒤에 둔다 |
| `configs/priors.json` | `claude-code-hub`, `codex-hub`, `codex-luna-hub` 행 추가 |
| `pyproject.toml` | 선택 의존성 `[proxy]` (httpx, starlette, uvicorn) |

### 스모크 결과 (2026-11-09, 실제 CLI + 가짜 상위 서버)

API 키 없이 트리거 경로만 확인하려고 `scripts/fake_llm_upstream.py`(짧은 답 + 큰 usage, Anthropic 은 턴당 Bash 도구 호출 3번)를
프록시 뒤에 두고, LHTB 2.0 `seismo_cross_correlation_lag__01` 컨테이너(Docker Desktop, macOS) 안에서 실제 CLI 를 15분 돌렸다.
고정 L 20K 를 프록시로(`--proxy-fixed 20k --set Lp=0 --set L0_mode=fixed --set L0_fixed=0`), 검증 거부 → 같은 대화 이어가기.

| 하네스(버전) | 트리거 | 결과 |
|---|---|---|
| Codex(최신, 로컬 압축) | usage(보고 258K) | 신호 175 → **하네스 자체 요약 174회**(`CONTEXT CHECKPOINT COMPACTION`), 다음 호출마다 새 창으로 인식. 패턴 `CSCS…`. 이어가기(`exec resume --last`)는 같은 스레드 |
| Claude Code 2.1.319 | usage(보고 950K) | **압축하지 않음**(같은 실행 안의 다음 호출을 그대로 보냄) |
| Claude Code 2.1.319 | error, 최소 창 1 | 반응형 압축 3회 성공 뒤 "Autocompact is thrashing" 으로 실행 종료 |
| Claude Code 2.1.319 | error, 최소 창 4(프록시 가드) | 결정 78 → 400 77 → **반응형 압축 77회 성공**, 폭주 0. 패턴 `C m m m R S` 반복 |

과정에서 고친 것: Claude Code 요약 요청 인식(접두 → 포함), 설치형 에이전트의 완료 표시(`termination_reason`), 프록시 쪽 최소 창 가드와
"압축 대기 중에는 신호를 계속 보냄", Codex 의 `model_context_window` 미설정.

### 실제 API 검증 (2026-11-09, Claude Code 2.1.3xx + `anthropic/claude-haiku-5-5`)

`seismo_cross_correlation_lag__01`, 20분, Docker Desktop. 같은 L = 40K 를 두 경로로.

| | 동적: 프록시 `--proxy-fixed 40k` (trigger error, 최소 창 4) | 정적: `--limit 40k` (창 100K × 40%, 프록시는 관측만) |
|---|---|---|
| 압축 직전 main prompt | 40,068 / 40,247 / 40,092 / 40,315 — **설정값에서 정확히** | 29.5–32.0K (11회) — Claude Code 자체 추정의 여유분 때문에 **약 25% 이르게** |
| 압축 | 4회(400 4회 → 반응형 압축 4회), 폭주 0, API 오류 0 | 11회, 폭주 0 |
| 비용(그중 요약 호출) | $0.118 ($0.011) | $0.130 ($0.032) |
| 캐시 적중 | 98.0% | 94.8% |
| reward | 0.5 | 0.5 |

측정된 Claude Code + Haiku 5.5 파라미터: 압축 직후 prompt 20.6–25.4K 중 캐시 적중 13.5K(→ L0 ≈ 13.5K, Lp ≈ 7–12K),
요약 호출 1회(m = 1)의 입력은 압축 직전 문맥 + 약 2K, 그중 93–95% 캐시 적중, 출력 O_s 3.4–5.5K.
정적 손잡이로 L 을 정하려면 목표 L 의 약 1.3배를 설정해야 하고, 프록시 경로는 API 가 보고한 값으로 그대로 맞는다.

실제 API 에서 발견해 고친 버그: 프록시가 `Accept-Encoding` 을 지운 뒤 httpx 기본값(gzip)으로 상위에 요청하고 압축된 SSE 를
그대로 넘겨, Claude Code 가 스트림을 해석하지 못하고 매 턴 비스트리밍으로 다시 보냈다(호출 2배, usage 누락). 가짜 서버는 압축하지
않아 스모크에서 보이지 않았다. `Accept-Encoding: identity` + 디코딩된 바이트로 중계(커밋 e25e682).

### 실제 API 검증 (2026-11-10, Codex + `openai/gpt-6-luna`)

같은 과제(`seismo_cross_correlation_lag__01`), 0.34 h, Docker Desktop, 같은 L = 40K.

| | 동적: 프록시 `--proxy-fixed 40k` (trigger usage, 최소 창 3) | 정적: `--limit 40k` (`model_auto_compact_token_limit`, 프록시는 관측만) |
|---|---|---|
| 압축 직전 main prompt | 41,102 (신호 1회 → 압축 1회) | 35.8–46.6K, 대부분 39.2–40.0K (6회) |
| 요약 호출 | 1회, 입력 41.5K(캐시 99%), 출력 626 | 6회, 입력 40.6–46.6K, 출력 561–841 |
| 새 창 첫 prompt | 10.7K (그중 캐시 10.4K) | 10.9–11.0K |
| 비용(그중 요약) | $0.0226 ($0.0008), 23 호출 | $0.0918 ($0.0065), 89 호출 |
| 결과 | reward 1.0, 첫 제출에서 통과(5분 53초) | 1차 검증 실패 → 같은 대화로 이어감 → 시간 만료, reward 0.65 |

- usage 트리거가 실제 Codex 에서 동작한다: 신호를 받은 바로 다음 턴에 Codex 가 자기 로컬 압축(도구 없는 요약 1회, m = 1)을 했다.
- Codex 자체 임계값은 Claude Code 와 달리 거의 정확하다(마지막 응답의 total_tokens 기준이라 설정값 근처에서 발동).
  따라서 Codex 에서는 정적 손잡이가 좋은 근사·대조군이고, 프록시의 이점은 L 을 매 턴 바꾸는(balance·formula) 데 있다.
- **balance 모드 (같은 과제·예산, 2026-11-10, 단일 진입점 `make_config.py --harness codex --balance --learn` → `run_job.sh`)**:
  사전값 `codex-luna-hub` 로 C = 247K(캐시 읽기 토큰 환산), L* = 43.4K. 14번째 호출(P = 41.5K, 누적 rent 228.7K)에서 압축 결정
  → 신호 1회 → Codex 요약 1회 → 새 창 시작. 이 압축에서 요약 출력(0.6K, 사전값 4.5K)을 학습해 C = 172K, L* = 39.5K 로 내려감.
  reward 1.0(6분 24초), $0.026, 캐시 적중 89.4%. 프록시는 run_job.sh 가 띄우고 끝나면 정리됨.
- 두 실행의 reward·비용 차이는 과제 진행이 달라서이지(n = 1) 경로 차이가 아니다.
- 측정값: L0 ≈ 10.4K(새 창 캐시), 요약 출력 O_s ≈ 0.6–0.8K(Claude Code 의 약 1/6).
- 실행 중 스크립트를 고쳐 두 실행의 마무리 단계(토큰 표·프록시 종료)가 bash 오류로 건너뛰어졌다. 결과에는 영향 없음, 표는 수동으로 다시 만듦.

### Harbor 연동에서 확인한 것 (스모크)

- 설치형 에이전트는 `termination_reason` 을 남기지 않아 Harbor 의 `same_conversation` 루프가 첫 실행 뒤 중간 채점·이어가기를 하지 않고
  끝난다(trial.py: `reason != "confirmed_task_complete"` 이면 중단). 어댑터가 CLI 정상 종료를 완료 선언으로 기록한다.
- 백그라운드로 띄운 `harbor run` 은 SIGINT 를 무시한다(비대화형 셸). 멈출 때는 SIGTERM.
- 컨테이너 안 설치(`curl … claude.ai/install.sh`)가 SSL EOF 로 실패할 수 있다(amd64 에뮬레이션). 재시도로 통과.
- 고정 모드의 안전 조건 "prompt > L0 + Lp" 때문에 성장이 거의 없는 가짜 서버 스모크에서는 `--set Lp=0 --set L0_mode=fixed --set L0_fixed=0` 이 필요하다
  (실험의 L 은 L0+Lp 보다 훨씬 커서 해당 없음).

## 5. 실험 설계

질문: 하네스가 자기 방식으로 압축할 때도, optlimit 이 고른 시점(balance, 학습된 C)이 기본 시점·정적 L 보다
**같은 점수에서 과제당 비용이 낮은가**. 그리고 그 차이가 압축 방식(Claude Code: 큰 Lp·m 1·캐시 재사용,
Codex: 작은 Lp·도구 없는 요약 호출)에 따라 어떻게 달라지는가.

### 5.1 조건 (하네스마다)

| 조건 | 생성 | 압축 시점 | 무엇을 분리하나 |
|---|---|---|---|
| A native | `--limit native --observe` | 하네스 기본(CC ~967K, Codex ~245K) | 기준선. 프록시는 관측만(trigger off) |
| B static L\* | `--limit <사전 L*> --observe` | 하네스 자체 임계값(CC 87K, Codex 35–46K) | 시점만 바꾼 효과(프록시 개입 없음) |
| C proxy-fixed L\* | `--proxy-fixed <같은 L>` | 프록시 신호, 같은 L | B 와 같은 L 이므로 트리거 경로(Codex usage 조작, Claude Code 400)의 부작용 확인 |
| D balance | `--balance <prior> --prices <row> --learn` | 집세 ≥ C (C 는 관측한 압축으로 학습) | 주 조건: g 없이 하네스·모델에 맞춰 적응 |
| E formula (선택) | `--formula <prior> --prices <row> --set g_prior_weight=0` | L\* = L0+Lp−g+√(2gC), g 는 관측만 | D 와 비교(성장이 울퉁불퉁할 때 차이) |

B 와 C 가 같으면 프록시 트리거가 하네스 행동을 바꾸지 않는다는 근거가 되고, D 의 효과를 "시점"으로 해석할 수 있다.

### 5.2 과제·모델·예산

- 과제: 컨테이너가 프록시(호스트)에 닿아야 하므로 `allow_internet` 이 켜진 과제만. LHTB 2.0 재구성 과제(30개 모두 기본값 true)
  중 terminus-2 실험과 겹치는 `gdal_antimeridian_clip__01`, `sqlite_wal_recovery_audit__02`, `seismo_cross_correlation_lag__01`
  을 먼저(비교 가능), 여유가 있으면 3개 추가. LHTB 1.0 의 `allow_internet = false` 과제(spot-scheduler, APEX 등)는 불가.
- 모델: Codex × `openai/gpt-6-luna`(싸다: 3시간 trial ≈ $0.5–1.5), Claude Code × `anthropic/claude-haiku-5-5`(주 격자),
  `anthropic/claude-sonnet-5-5`(확인용 1회; 3시간 ≈ $10/trial).
- 예산: 과제당 3시간, `HB_CONTINUE_MODE=same_conversation`(어댑터의 resume 로 같은 대화), 이진 피드백.
- 반복: 같은 조건에서도 점수 변동이 크다(80k apex-law433 0.610 → 0.822). 주 비교(A vs D)는 (과제, 조건)당 3회.
  격자: 3 과제 × 4 조건(A–D) × 3 회 × 2 하네스 = 72 trial(동시 실행은 Docker 메모리 한도 안에서).

### 5.3 순서 (돈을 아끼는 순서)

1. **스모크**(15분, 고정 20K 을 프록시로): 하네스가 신호에 맞춰 압축하는지, 압축 호출이 `kind=summary` 로 잡히고 다음 호출이
   창 시작으로 잡히는지, resume 가 같은 대화를 잇는지.
2. **native 1회씩**(A): 프록시 로그로 하네스별 L0, Lp, g, O_s, γ 를 잰다(`proxy_turns.py --job-dir` → `fit_priors.py`)
   → `configs/priors.json` 에 `claude-code-haiku-5-5`, `codex-gpt-6-luna` 행.
3. **오프라인 리플레이**: 같은 궤적에 `replay.py --limits … `(fixed, formula, balance)를 돌려 조건 B–E 의 L 과 예상 절감을
   정한다. 리플레이로 차이가 1–2% 안이면 라이브 조건을 줄인다(최적 근처는 평평하다).
4. **라이브 격자**(B–D, 그다음 반복).

### 5.4 측정

- 점수: 최종 reward, 제출별 중간 점수(`interim_verifier.jsonl`) → 시간·비용 축 곡선(`reward_curve.py`).
- 비용: 프록시 로그의 실제 usage × `prices.json`(과제당 $, 턴당 $, 요약 호출 몫), 캐시 적중, 압축 횟수, 압축 직후 prefill.
- balance 진단: 압축 때의 집세와 C, 학습된 Lp·O_s·γ 의 수렴(사전값 대비), K(창당 턴).
- 정보 손실: 압축 뒤 명령 반복(`trajectory_issues.py`/재획득), 하네스의 "남은 문맥" 표시가 바뀐 부작용(C vs B).

### 5.5 위험과 대응

| 위험 | 대응 |
|---|---|
| 하네스가 보고된 usage 외에 로컬 추정을 더함(Codex: 마지막 usage 이후 항목 추정) | low_cap 50K + 추정이 자체 임계값(200K)을 넘지 않게 대역을 넓게; 넘으면 `proxy_turns` 의 compactions 와 signals 가 어긋나므로 바로 보임 |
| Codex #16033(압축이 다시 안 걸림) | `model_context_window` 를 설정하지 않음; signal 을 하드 상한(창 95%) 아래 258K 로 |
| Claude Code 가 usage 신호를 듣지 않음(확인됨) | trigger=error 가 기본(`make_harness_config.py`). 400 이 한 번 하네스에 보이는 것이 부작용(로그에 `rejected`) |
| Claude Code 폭주 방지에 걸림 | `min_window_turns` 4(기본); L 은 압축 직후 크기 L0+Lp 보다 충분히 크게 |
| 실제 prompt 가 하네스에 숨겨짐 | `limit_max` 180K 로 L\* 상한, 하네스 임계값과 API 한계 아래 |
| 설치 단계 네트워크 실패(curl SSL) | 재시도; 반복되면 이미지에 미리 설치 |
| Daytona 샌드박스는 이 호스트의 프록시에 닿지 않음 | Docker(Linux 연구 서버 또는 Docker Desktop) 사용, 또는 `OPTLIMIT_PROXY_HOST` 로 공개 터널 |

### 5.6 다음 단계(코드)

- Codex app-server 어댑터: `thread/compact/start` 로 프록시 없이 직접 압축(턴 사이에만 가능), `rawResponse/completed` 로 usage.
- API 서버 측 압축: 프록시에 구현됨(요청의 `trigger.value`/`compact_threshold` 를 매 요청 L\* 로, `usage.iterations` 를 요약 비용으로).
  단위 테스트만 있고 실제 API 호출로는 아직 확인 못 함(키 만료) — 이 기능을 쓰는 에이전트로 스모크 필요.
- Claude Code `PreCompact` 훅으로 거부권만 쓰는 방식(임계값을 낮게 두고 컨트롤러가 "아직" 이면 exit 2): 프록시 없이 가능하지만
  훅이 usage 를 못 보므로 컨트롤러 상태를 파일로 넘겨야 한다.
