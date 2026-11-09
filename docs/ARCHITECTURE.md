# 구조

이 저장소는 세 축을 서로 모르게 나눈다. 실험 하나는 각 축에서 하나씩 고른 조합이다.

| 축 | 질문 | 어디에 있나 | 고르는 법 |
|---|---|---|---|
| **정책** (언제 압축하나) | fixed L / formula L\* / balance(집세 ≥ C) / off | `optlimit/controller.py`, `cost_model.py`, `estimator.py` | `--limit N\|native`, `--proxy-fixed N`, `--formula PRIOR`, `--balance PRIOR` (+ `--prices ROW`) |
| **하네스** (압축이 어떻게 일어나나) | terminus-2(in-process), Claude Code·Codex(CLI, 프록시) | `optlimit/harnesses.py`(프로필), `optlimit/adapters/`, `optlimit/proxy.py`·`dialects.py` | `--harness terminus-2\|claude-code\|codex` |
| **벤치마크** (무엇을 어떤 조건으로) | LHTB 1.0, LHTB 2.0, Terminal-Bench 4.0, 임의의 Harbor 데이터셋 | `configs/benchmarks.yaml`(프리셋) | `--benchmark lhtb1\|lhtb2\|tb4`, 또는 `--tasks-dir DIR` / `--dataset org/name@ver` |

## 층

```
configs/make_config.py  ── 세 축을 받아 Harbor job yaml 한 개를 쓴다 (# env:, # proxy: 머리줄 포함)
        │
scripts/run_job.sh      ── .env, 키·환경 확인, # proxy: 가 있으면 프록시를 먼저 띄움(run_harness_job.sh), Harbor 실행/이어서 실행
        │
Harbor (LHTB 7ca0cac + patches/01–04)
        │  agents: optlimit.adapters.*            ← 하네스 축
        ▼
┌──────────────────────────────┐        ┌──────────────────────────────────────┐
│ in-process (terminus-2)       │        │ out-of-process (Claude Code, Codex)   │
│ harbor_terminus2.py 가        │        │ CLI → optlimit.proxy → API            │
│ _summarize() 를 직접 호출     │        │ 프록시가 usage 신호 / 400 으로        │
│                               │        │ 하네스 자신의 압축을 일으킴           │
└──────────────┬───────────────┘        └──────────────────┬───────────────────┘
               └──── on_prompt / note_summary_usage / note_kind ──┘
                                        ▼
                     optlimit.controller (정책 축, 하네스·벤치마크를 모름)
```

## 축별 파일

### 정책 (하네스·벤치마크 무관, 표준 라이브러리만)
- `optlimit/cost_model.py` — f(L), L\*, 압축 1회 비용 C, 짧은 과제 규칙
- `optlimit/estimator.py` — 턴당 증가 g 추정
- `optlimit/controller.py` — `LimitController`(formula / balance / fixed / off, Lp·O_s·γ 학습), `config_from_spec()`
- `optlimit/usage.py` — 공급자별 usage → (prompt, cached, written, completion)
- `configs/priors.json`(하네스 × 모델 사전값), `configs/prices.json`(단가 → α, β)

### 하네스
- `optlimit/harnesses.py` — **하네스 프로필 한 곳**: Harbor 에이전트 경로, 연결 방식(in-process / proxy), 프록시 트리거,
  보고 대역(low_cap / high / limit_max), 최소 창, 자체 임계값 손잡이, 요약 요청·새 창 표지 문구, 기본 사전값
- `optlimit/adapters/harbor_terminus2.py`(+ `terminus2_handoff.py`) — terminus-2 in-process 훅
- `optlimit/adapters/harbor_installed.py` — Claude Code / Codex Harbor 서브클래스(자체 임계값, 같은 대화 이어가기)
- `optlimit/proxy.py`, `optlimit/dialects.py`, `optlimit/serve.py` — API 방언(Anthropic / OpenAI Chat / Responses), 트리거
- `optlimit/adapters/chat_loop.py` — Harbor 밖 에이전트용 참조 루프


#### 하네스에 L 을 거는 두 길: 프록시 vs 자체 임계값

| | 프록시 (`--balance` / `--formula` / `--proxy-fixed`) | 자체 임계값 (`--limit N`) |
|---|---|---|
| 판단 근거 | API 가 돌려준 **실제** prompt·cache 토큰 | 하네스 자신의 추정(Claude Code 는 명목값보다 ~25% 일찍) |
| L | 호출마다 바꿀 수 있음(balance·formula) | 시작 때 한 번 고정 |
| 필요한 것 | 호스트의 프록시 프로세스 + 컨테이너→호스트 네트워크, 하네스별 트리거 | 없음(환경변수·`-c` 플래그) |
| 정확도(검증) | Claude Code 40K → 40.1–40.3K, Codex 40K → 41.1K | Claude Code 40K → ~30K |

자체 임계값이 남아 있는 이유: (1) 하네스가 원래 쓰는 생산 경로라서 "하네스 기본 동작" 대조군이 된다,
(2) 프록시를 못 쓰는 곳(인터넷 차단 과제, 원격 샌드박스에서 호스트에 닿지 않을 때)의 근사 대안이다(보정 계수를 곱해 씀),
(3) 프록시 모드에서도 하네스 자체 임계값을 대역(low_cap < native < high) 안 높은 곳에 두어 프록시만 결정하게 하는 데 쓴다.
동적 최적화가 목적이면 프록시가 기본이다.

### 벤치마크
- `configs/benchmarks.yaml` — 프리셋: 과제 위치(로컬 경로 또는 Harbor Hub 데이터셋), 기본 예산, 이어가기 방식, 주의(인터넷·GPU·메모리)
- `LHTB/`(setup.sh 가 만드는 체크아웃): LHTB 1.0 `tasks/`, 2.0 `tasks-2.0/`, 패치된 Harbor
- 벤치마크 고유 동작(검증 거부 뒤 이어가기)은 Harbor 패치와 과제의 `continue_until_timeout` 에 있고, 코드는 이를 `# env:` 로만 켠다

### 실행·분석 (세 축 공통)
- `scripts/run_job.sh`(한 job), `run_sweep.sh`(여러 job), `run_harness_job.sh`(프록시 기동; run_job.sh 가 필요할 때 부름)
- `analysis/token_turns.py`(Harbor trajectory → 턴 표), `analysis/proxy_turns.py`(프록시 로그 → 같은 형식), `replay.py`, `fit_priors.py`, …

## 새로 붙일 때

- **새 하네스**: `optlimit/harnesses.py` 에 프로필 하나(요약 표지 문구, 트리거, 대역, 자체 임계값 손잡이) +
  필요하면 `adapters/` 에 Harbor 서브클래스. 프록시·컨트롤러·생성기는 그대로.
- **새 벤치마크**: `configs/benchmarks.yaml` 에 프리셋 한 개. Harbor 가 받는 데이터셋이면 코드 변경 없음.
- **새 정책**: `controller.py` 의 mode 하나. 하네스·벤치마크 코드는 그대로.
