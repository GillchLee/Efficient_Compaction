# 병렬 재파악: 압축 직후 재조회를 한 턴에 몰기

제안: 압축 직후 에이전트가 파일을 하나씩 다시 읽는 대신(`optlimit/reread.py` 가 재는 재읽기 3–4턴),
**필요한 정보를 먼저 떠올리고 한 응답에서 도구 호출 여러 개로 한꺼번에 가져오게** 한다. 그리고 그 재파악 단계를
압축 비용 C 에 포함해 L\* 를 정한다.

상태: 문구와 측정은 이 저장소 밖의 독립 하네스에서 했다(2026-11-19). 프록시 경로(Codex)에는 문구를 붙이는 옵션이
들어갔다: `make_config.py --reorient parallel|sequential` (= `--set reorient=NAME`, `optlimit/reorient.py`). 프록시가
압축 요약 바로 뒤에 문구를 user 메시지로 넣고(carry 블록이 있으면 그 뒤), 창이 끝날 때까지 같은 자리에 유지한다.
프록시 로그의 `new_tool_calls` 가 직전 응답의 호출 수(턴의 폭)다. in-process terminus-2 와 Claude Code 에는 아직 없다.
3절의 회계(C', Lp_eff)는 설계안이고 컨트롤러에 들어가 있지 않다. 2절은 측정 결과다.

## 1. 구현

### 1.1 "병렬 도구 호출"이 뜻하는 것

도구 호출 API(Anthropic `tool_use`, OpenAI `tool_calls`)에서 모델은 **한 응답에 호출 블록을 여러 개** 낼 수 있다.
하네스는 그 블록들을 모두 실행한 뒤 결과를 **한 메시지**로 돌려준다. 즉 "병렬"은 모델 왕복 1회에 호출 N개를
싣는다는 뜻이고, 실행 자체가 동시일 필요는 없다(측정에 쓴 하네스는 순서대로 실행했다).

    순차:  모델 → cat kernel.py → 모델 → cat problem.py → 모델 → run_tests → 모델(작업 시작)     왕복 4회
    병렬:  모델 → [timer.sh, cat kernel.py, cat run_tests.sh, read problem.py] → 모델(작업 시작)  왕복 2회

실제 기록(RE_PAR, 압축 직후 첫 턴, 호출 2개에 조회 4건):

    bash       bash /app/timer.sh; echo ---; cat /app/kernel.py; echo ---; cat /app/run_tests.sh; ...
    read_file  /app/problem.py

모델은 두 방식을 섞는다: 호출 블록을 여러 개 내거나, 한 bash 호출 안에 명령을 `;` 로 이어 붙인다. 둘 다 왕복을
줄인다는 점에서 같다. 하네스별로는

| 하네스 | 한 턴에 여러 조회를 싣는 방법 |
|---|---|
| Claude Code, Codex, 도구 호출 API 직접 사용 | 한 응답에 호출 블록 여러 개 |
| terminus-2 | 한 응답의 `commands` 배열에 키 입력 여러 개 (화면 출력이 한 번에 돌아옴) |

### 1.2 문구

압축 뒤 문맥은 `과제 지시문 + 요약 + "Continue the work from where you left off." + 조건별 문구` 이다.
하네스 로직은 바꾸지 않았고 문구만 다르다. 강제 장치는 없다.

RE_PAR(병렬 재파악):

    Before continuing, re-orient yourself: the summary above may be incomplete or wrong.
    1. Think about everything you need to know to continue correctly (the task constraints, the current
       contents of /app/kernel.py and any helper files, the current verified cycle count, the relevant
       parts of the simulator, the remaining budget).
    2. Decide which of these are missing or uncertain in the summary.
    3. Fetch ALL of them in ONE message by issuing all the needed tool calls in parallel. Do not call
       tools one at a time while re-orienting.
    Then continue the work.

RE_SEQ(대조군, 병렬 지시 없음):

    Before continuing, re-orient yourself: the summary above may be incomplete or wrong. Use the tools
    to check the current state of the workspace, then continue.

1번의 괄호 안 목록은 과제에 맞춘 것이다. 다른 과제에 쓸 때는 "과제 제약, 작업 중인 파일의 현재 내용, 마지막으로
검증된 결과, 남은 예산" 같은 일반형으로 바꿔야 한다.

## 2. 측정 (2026-11-19)

설정: SWE-Marathon `vliw-kernel-optimization`(시작 147,734 cycles, 통과 < 1,250), `claude-haiku-5-5`,
입력+출력 32K 초과 시 같은 모델이 요약, 모델 호출 60턴 예산(원 과제의 8시간 제한을 대체), 로컬 Docker,
도구 `bash / read_file / write_file / edit_file`. 조건당 8 trial, 압축 없는 FULL 은 4 trial. 채점은 과제의
공식 `tests/test.sh`. 자체 하네스이므로 공식 리더보드 수치와 직접 비교할 수 없다.

재파악 단계 = 압축 뒤, 쓰기 없는 도구 호출만 하고 출력이 3K 토큰 미만인 턴이 이어지는 구간(첫 쓰기 턴 전까지).
`reread.py` 의 "이전 창에서 본 파일을 다시 읽는 호출"과는 정의가 다르다(테스트 실행·예산 확인도 포함).

| 조건 | 압축 직후 첫 턴 ≥2 호출 | 압축당 재파악 턴 | 압축당 재파악 호출 | 재파악 구간 입력 | 재파악 턴 / 60턴 | 첫 <2,000 도달 턴 | 최종 cycles 중앙값 | 통과 | 유효 커널 |
|---|---|---|---|---|---|---|---|---|---|
| FULL | – | – | – | – | – | 10 | 1,184 | 3/4 | 4/4 |
| BASE | 0% | 2.88 | 2.88 | 20.5K | 34% | 38 | 1,378 | 1/8 | 6/8 |
| RE_SEQ | 7% | 3.66 | 3.73 | 28.1K | 45% | 38 | 1,433 | 1/8 | 8/8 |
| RE_PAR | 95% | 1.63 | 2.82 | 11.0K | 20% | 16 | 1,346 | 3/8 | 8/8 |

- **왕복만 줄었다.** 압축당 호출 수는 2.88 → 2.82 로 같고, 턴 수가 2.88 → 1.63 으로 줄었다. 가져오는 양은
  비슷하고 그것을 읽는 데 드는 prompt 재전송이 줄어든 것이다(구간 입력 20.5K → 11.0K).
- **효과는 "재파악하라"가 아니라 "병렬로 하라"에서 나온다.** RE_SEQ 는 재파악 턴이 오히려 늘었다(3.66).
- **진행 속도.** 2,000 cycles 아래 첫 도달이 38턴 → 16턴(순위 순열검정, BASE 대비 p = 0.003, RE_SEQ 대비
  p = 0.0015). 이 값은 에이전트가 직접 돌린 테스트 출력에서 읽었고, RE_PAR 은 테스트를 trial 당 10.0회
  (BASE 4.8회) 돌려서 더 일찍 관측된 부분이 섞였을 수 있다.
- **최종 성능은 방향만 일치한다.** log2 속도향상 차이 +1.79 [+0.06, +3.61], p = 0.061 이고 대부분 BASE 의 무효
  커널 2건에서 나온다. 최종 cycles 순위검정 p = 0.10.
- **압축 자체의 비용.** FULL 이 가장 좋지만 trial 당 $3.68, 압축 조건은 $0.24–0.29.

앞선 쉬운 벤치(Aider polyglot Python 12문제를 한 대화에서 연속 풀이, 임계 16K / 8K)에서는 같은 문구가 첫 턴 호출
수를 2.25 → 4.60 으로 늘렸지만 세션 호출 수 차이는 구간이 0 을 포함했다. 그 과제에서는 Haiku 5.5 가 문구 없이도
압축 직후 첫 턴의 69–86% 를 여러 호출로 처리해서 여지가 작았다.

한계: 과제 1개, 조건당 8 trial, 짝지은 표본이 아님. 무효 커널 4건과 미최적화 1건은 모두 예산 소진으로 끝난
trial 이라 최종 점수가 마지막 몇 턴에 좌우된다. 모델 1종(Haiku 5.5).

## 3. 재파악을 압축 비용에 넣는 정책 (설계안)

지금의 재읽기 처리(`optlimit/controller.py`)는 재읽기가 여러 턴에 하나씩 일어난다는 전제다.

| 옵션 | 지금 하는 일 | 병렬 재파악에서 어긋나는 점 |
|---|---|---|
| Re-read Charge | C += R, 사전값 R = `reread_turns`(3.5) × (턴당 비용 + β·`reread_turn_out`) | 턴 수만 센다. r 이 1–2 로 줄어도 재조회 토큰 W 는 그대로라 과소평가 |
| Productive-turn Charge | 창 K 턴 중 r 턴을 비생산으로 보고 생산적 턴당 비용 최소화 | r 의 사전값이 순차 기준 |
| Net rent | 재읽기 호출마다 그 prompt 증가를 집세에서 뺌 | 증가가 첫 턴 한 번에 W 만큼 온다. 호출 단위가 아니라 단계 단위가 맞음 |
| File Carry-over | 자주 재읽는 파일을 요약 뒤에 붙임 | 병렬이면 "필요할 때 가져오는 비용"이 싸져서 붙일 이유가 줄어듦 |

### 3.1 재파악을 압축의 2단계로 회계 처리

요약 호출과 재파악 턴을 묶어 압축 1회로 본다. 기호는 `docs/formulas.md` 를 따르고, 추가로
r = 재파악 턴 수, W = 재파악 단계에서 문맥에 들어온 토큰(도구 출력 합), O_r = 재파악 턴들의 출력 토큰 합.

**(a) 고정비.**

    C' = C + r·(L0 + Lp) + (α − 1)·W + β·O_r

`r·(L0+Lp)` 는 재파악 턴이 기본 prompt 를 읽는 비용, `(α−1)·W` 는 재조회 토큰을 새로 쓰는 프리미엄, `β·O_r` 은
무엇이 필요한지 추론하고 호출 인자를 쓰는 출력이다. β 가 크면(Luna β = 50) 셋 중 `β·O_r` 이 가장 크다.
사전값의 곱 대신 **관측한 단계 비용의 최근 `learn_recent` 회 평균**으로 학습한다(`RereadTracker.reread_cost` 와
같은 방식, 첫 압축 전까지만 사전값).

**(b) 창의 시작 크기.**

    Lp_eff = Lp + W

재조회한 토큰은 창이 끝날 때까지 매 턴 다시 읽힌다. 집세가 아니라 창의 바닥이다. balance 모드에서는 집세 기준선
P_s 를 **재파악 단계가 끝난 시점의 prompt** 로 잡는다(Net rent 의 단계 단위 판).

**(c) 창의 길이.** K 는 생산적 턴만 센다(Productive-turn Charge). r 의 사전값은 조건별로 둔다: 순차 3.5,
병렬 1.6(2절 측정; 모델·과제 1종이라 잠정값).

### 3.2 재파악 단계의 판정

`reread.py` 는 파일 경로가 있는 읽기 호출만 잡는다. 병렬 재파악 턴에는 테스트 실행, 남은 예산 확인, `ls` 처럼
경로가 없는 호출이 섞인다. 창 시작부터 **첫 편집 호출 전까지**를 재파악 단계로 보는 구간 기준을 추가하고,
상한(예: 3턴)을 둔다. 파일별 재읽기 점수(carry 용)는 지금 방식 그대로 둔다.

### 3.3 따라오는 변화

- **L\* 가 내려간다.** r 이 줄어 C' 가 작아지면 더 자주 압축해도 된다. `formulas.md` 2절의 Luna 사전값
  (재읽기 제외 C ≈ 243K, 재읽기 턴당 ≈ 40K 로 가정)으로 어림하면 r 3.5 → 1.6 에서 √(2gC) 항이 약 10% 줄어든다.
  W 와 O_r 을 재지 않은 어림이다.
- **창 하한이 내려간다.** `K_min = ρ/ε` 에서 ρ 가 줄어든다.
- **carry 의 손익분기가 올라간다.** 파일 f(크기 w_f, 다음 창에서 다시 읽힐 확률 p)를 붙이는 조건은

      (1 − p)·(α − 1 + K)·w_f  <  p·(필요할 때 가져오는 한계 비용)

  순차에서는 한계 비용이 턴 하나 전체(prompt 읽기 + β·출력), 병렬에서는 호출 인자 출력 β·o_call 뿐이다.
  α = 10, K = 20, w_f = 3K, o_call = 50–100 토큰으로 가정하면 순차는 p ≳ 0.8, 병렬은 p ≳ 0.95 에서 붙이는 것이
  이득이다. 병렬 재파악을 켜면 carry 는 `carry_order: reread_edited` 의 최상위 한두 개로 줄이는 쪽이 맞다.
- **짧은 과제 규칙.** `formulas.md` 3절 부등식의 우변에 재파악 비용을 더한다. 남은 턴이 적으면 재파악 턴 하나도
  손해다.

## 4. 다음에 할 것

1. `analysis/replay.py` 로 기존 궤적에 r, W 를 바꿔 넣어 L\* 와 턴당 비용이 얼마나 움직이는지 본다(API 비용 없음).
2. 손 넘김 문구에 병렬 재파악 문구(일반형)를 붙이는 옵션을 추가하고, in-process terminus-2 와 프록시 경로에서
   r, W, O_r 을 측정한다. 다른 모델(Luna, Sonnet)에서 문구를 따르는 비율부터 확인한다.
3. 3.1 의 (a)–(c) 를 컨트롤러 옵션으로 넣고, 순차 사전값 / 병렬 사전값 / 관측 학습을 리플레이와 실제 실행으로 비교한다.
4. 2절의 측정 편향 제거: 하네스가 매 턴 뒤 직접 채점해 진행 곡선을 기록(`analysis/reward_curve.py` 와 같은 축).
