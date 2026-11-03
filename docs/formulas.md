# 공식 정리 — 압축 비용 모델, 최적 창, 하한, 적응, balance 규칙

단위: 토큰. 가격은 **캐시 읽기 단가를 1** 로 둔 비율. 기호:

| 기호 | 뜻 | 측정 |
|---|---|---|
| L0 | 압축 뒤에도 캐시 적중인 접두(시스템 프롬프트) | 창 시작 턴의 cache_read |
| Lp | 압축 직후 새로 쓰는 부분(과제 + 요약/손 넘김 + 화면) | 창 시작 턴의 cache_write (= prompt − cache_read) |
| g | 턴당 prompt 증가(에이전트 출력 + 관측) | 창 안 연속 턴의 prompt 차이 |
| L | 압축 임계: 보낼 prompt 가 L 이상이면 압축 | 설정/정책 |
| K | 창당 턴 수 | (L − L0 − Lp)/g + 1 |
| α | write(또는 uncached)/read 단가비 | 가격표 |
| β | output/read 단가비 | 가격표 |
| m | 압축 한 번의 요약 호출들이 전체 문맥을 읽는 횟수 | 요약 호출 prompt 합 / L |
| γ | 그 읽기의 평균 단가(캐시 적중 1, miss α): γ = 1 + (α−1)·u, u = miss 비율 | 요약 호출 usage |
| O_s | 요약 호출들의 출력 토큰 합 | 요약 호출 completion |
| φ | Lp 의 L 기울기(요약이 문맥에 비례해 길어짐): Lp(L) = c + φL | 적합 |

## 1. 턴당 입력 비용 (docs/cost_model_derivation2.pdf 식 3 + 요약 호출)

창 안의 prompt 는 L0+Lp 에서 g 씩 L 까지 자란다. 평균 prompt 읽기 + 새 토큰 write 프리미엄:

    f_A(L) = (L0 + Lp + L)/2 + (α − 1)·( g + (Lp − g)/K )

요약 호출(모델 B)을 더하면

    f_B(L) = f_A(L) + ( m·γ·L + β·O_s + S_fixed ) / K

K 로 나뉘는 항이 "압축 한 번의 고정비를 창의 턴 수로 나눈 것" 이다.

## 2. 최적 창 (φ = 0, PDF 식 5 의 B 확장)

    C  = (α − 1)(Lp − g) + m·γ·(L0 + Lp − g) + β·O_s + S_fixed      (압축 1회 고정비)
    L* = L0 + Lp − g + √(2 g C)
    K* = √(2 C / g)  (근사)

φ > 0 이면 수치 최소화(`optimal_L`), 특수 경우 닫힌 꼴(`optimal_L_closed_phi`). 최적 근처는 평평해서(PDF 식 6) L 을 ±20% 틀려도 턴당 비용은 1–2% 차이.

Luna 실측(Lp 3.2K, g 1,021, m 2.18, γ 1.51, O_s 4.2K, α 10, β 50): C = 19.6K + 11.3K + 212K = 243K → L\* 25.9K, K 21. **β·O_s 가 C 의 87%** 라 L\* 는 β 와 O_s 에 가장 민감하다(탄력성 β 0.38, g 0.37, α 0.05).

## 3. 짧은 과제 규칙 (PDF 6a)

남은 턴 R 이 적으면 압축이 손해일 수 있다. 현재 prompt P 에서

    압축 ⇔ R·(P − L0 − Lp) > (α − 1)(Lp − g) + m·γ·P + β·O_s + S_fixed

`horizon` 을 주면 formula 결정에 거부권으로 붙는다.

## 4. 창 하한 — 재획득(정보 손실) 바닥

압축 1회가 ρ 턴의 재획득(이미 한 명령 반복)을 낳는다고 측정되면(`analysis/reacquisition.py`), 재획득 비율을 ε 이하로:

    K_min = ρ / ε,   L_min = L0 + Lp + g·(K_min − 1),   L ← max(L*, L_min)

그리고 그 ρ 턴의 비용을 고정비에 더한다: C ← C + ρ·f(L\*). Luna 측정 ρ ≈ 1.5 → ε 0.1 이면 K_min 15, L_min ≈ 18.8K (L\* 25.9K 아래라 비활성). β 가 작은 가격에서 L\* 가 10K 대로 내려갈 때 작동한다.

## 5. 적응 — 무엇을 언제 갱신하나

| 변수 | 갱신 신호 | 규칙 |
|---|---|---|
| α, β | 가격표 변경(모델·TTL·개정) | `update_prices`; `prices_reload` 가 파일 변경을 감지 |
| g | 매 턴 prompt 증분 | 수축 평균(사전 가중 n0; n0 = 0 이면 관측만), 클리핑 |
| Lp | 창 시작마다 prompt − cached | 수축 평균 (`learn_lp`, 가중 `learn_weight` 회) |
| O_s | 압축마다 요약 호출 completion 합 | 수축 평균 (`learn_summary_out`) |
| γ | 압축마다 요약 호출의 miss 비율 u | γ = 1 + (α−1)·u (`learn_gamma`; α 가 바뀌면 자동 반영) |
| ρ | 압축 뒤 k 턴의 명령 반복률 − 기저 | 지금은 오프라인 측정값 입력(`reacq_turns`) |

갱신된 값으로 매 턴 L\*(또는 C)를 다시 계산하므로, 식 자체는 고정이고 입력이 움직인다.

## 6. Balance 규칙 (Dynamic TCP Acknowledgment 의 대응)

DTAP: 패킷이 도착할 때마다 ACK 를 미루면 패킷마다 지연 비용이 쌓이고, ACK 를 보내면 고정비 1. 온라인 최적은 "쌓인 지연 비용이 ACK 비용과 같아질 때 보낸다"(**balance/greedy**)로, 임의의 도착열에 대해 오프라인 최적의 **2배 이내**(결정론 최적, [Dooly–Goldman–Scott, JACM 2001](https://data.zhenzhi.com/details/1/875866061/1/2)); 무작위화는 e/(e−1) ≈ 1.58 ([Karlin–Kenyon–Randall, Algorithmica 2003](https://link.springer.com/doi/10.1007/s00453-003-1013-x)); 최대 지연 벌점 변형은 π²/6 ([Albers–Bals](https://portal.fis.tum.de/en/publications/dynamic-tcp-acknowledgment-penalizing-long-delays/)); 일반 지연 함수에서도 greedy 2-competitive ([Bhore–Pawłowski–Umboh, APPROX/RANDOM 2026](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.APPROX/RANDOM.2026.26)).

대응: 턴 = 패킷, 압축 = ACK(고정비 C), 지연 비용 = 창이 시작된 뒤 쌓인 토큰을 매 턴 다시 읽는 비용. 창 시작 prompt 를 P_s 라 하면 턴 t 의 "집세"는 P_t − P_s (읽기 단가 단위). 규칙:

    rent_t = Σ_{i ≤ t, 창 안} (P_i − P_s)
    압축 ⇔ rent_t + (P_t + ĝ − P_s) ≥ C   (다음 턴 집세까지 내기 전에),  단 K ≥ K_min

일정 성장(P_i − P_s = g(i−1))이면 rent = gK(K−1)/2 = C 에서 K ≈ √(2C/g), 즉 **2절의 L\* 와 같은 시점**이다. 다른 점은 성장이 울퉁불퉁할 때다. 큰 관측(15K)이 창 초반에 들어오면 수준 규칙(formula)은 prompt 가 L\* 를 넘는 즉시 압축하고(미래에 그 토큰을 계속 다시 읽을 것이라는 내기), balance 는 실제로 낸 집세가 C 에 이를 때 압축한다(미래에 내기하지 않음; 과제가 곧 끝나면 압축이 낭비였을 수 있으므로). 리플레이에서는 두 규칙의 턴당 비용이 모든 궤적에서 ±1% 로 같고(`analysis/replay.py` 의 `balance` 열), 가격이 바뀌는 경우에도 같다. balance 의 장점은 (1) ĝ 추정이 필요 없고(lookahead 0 이면 전혀), (2) C 만 현재 가격·학습 항으로 유지하면 되며, (3) 어떤 성장열에도 2배 보장이 있다는 것. 단점은 하한(K_min)을 따로 걸어야 한다는 것(DTAP 에는 "너무 자주 ACK" 의 품질 비용이 없다). 구현: `mode: balance` (`optlimit/controller.py`), `make_config.py --balance PRIOR`.

같은 계열의 다른 규칙과의 관계: ski-rental 의 손익분기(지금까지 낸 임대료 = 구매비면 산다)가 바로 이 규칙이고, "balance" 라는 이름으로 불리는 k-server/metrical task system 의 BALANCE 알고리즘(서버별 누적 비용을 맞추는)은 여러 세션·여러 모델에 예산을 나눌 때라면 몰라도 단일 세션의 압축 시점에는 해당하지 않는다.
