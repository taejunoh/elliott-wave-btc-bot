# ewbot Plan 5a: A-ETH — 고정된 전략 A를 ETH에 적용하는 별도 연구·모의매매 트랙 설계 명세

- 작성일: 2026-10-07
- 상태: 사용자 승인(결정 사항 2026-10-07, 1.3절 원문). **사전 등록: 이 명세와 구현 계획은 ETH 1시간봉을 한 번이라도 받기 전에 커밋한다**(2절). **2026-10-08 개정(단계 1 전, ETH 1시간봉을 받기 전; 최종 검토 I1의 (b)안, 컨트롤러 결정): 모의매매 구간은 A 지문과 A-ETH 판단 지문(`aeth/config.py`·`aeth/data.py`)에 묶고, 단계 1 → 잠금 → 홀드아웃은 그대로 A-ETH 지문 전체에 묶는다.** 개정 내용과 이유는 15절에 있다.
- 관련 명세
  - Plan 1: `docs/superpowers/specs/2026-10-01-elliott-wave-btc-bot-design.md` (전략 A, 연구 프로토콜, 9.6절 실거래 전환 기준)
  - Plan 2: `docs/superpowers/specs/2026-10-02-ewbot-plan2-paper-trading-design.md` (A의 모의매매 런타임)
  - Plan 3: `docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md` (A의 실거래 런타임, SHADOW → CANARY → FULL)
  - Plan 4: `docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md` (전략 B: 종료됨, 격리 방식의 선례)

## 1. 목표와 범위

### 1.1 목표
- 전략 A를 한 글자도 바꾸지 않고(코드, 파라미터, 지문, 손실 한도) Coinbase `ETH-USD` 1시간봉에 그대로 적용해, A의 우위가 BTC 밖에서도 나타나는지 **사전 등록된 절차로 한 번만** 확인한다.
- 통과하면 ETH 전용 표본 외 카운트(50건)와 모의매매 재현(4주 이상)을 따로 쌓아, 나중의 ETH 실거래(Plan 5c) 판단 근거로 쓴다.

### 1.2 비목표
- 전략 A의 변경. A의 지문(`e0435223…`), `holdout.lock`, BTC 50건 카운트, `paper.yml`·`paper` 브랜치, 실거래 런타임(SHADOW 진행 중)은 건드리지 않는다.
- 어떤 종류의 튜닝(파라미터, 셋업, 비용, 필터, 기간)도 하지 않는다. ETH가 실패해도 다른 자산을 시도하지 않는다.
- ETH 실거래. Plan 5c에서 다루며, A-BTC의 CANARY 통과와 A-ETH 단계 3 충족 뒤에만 시작한다(9절).

### 1.3 결정 사항 원문 (2026-10-07 사용자 승인, 그대로 옮김)

> - Code in a new package src/ewbot/aeth/ (outside A's fingerprinted folders); A's engine/strategy/backtest/holdout/paper modules imported only, never modified. ETH-USD 1h candles from the same Coinbase endpoint via data/coinbase_history.fetch_1h (import), cache data/cache/ETH-USD_1h.parquet, with a guard: no bar ≥ 2024-01-01 is requested or read until holdout-aeth.lock exists (do NOT use update_cache, which fetches to now). Costs: A's CostParams with contract_size 0.1 (ETP-20DEC30-CDE), other cost fields A's defaults (min fee 0.15 not applied in backtests as in A; funding 10%/yr) — note ETP liquidity/slippage and min fee as Plan-5 live confirmation items. Params: A's exact params read from holdout.lock (K4=2.0, Q_MIN=0.7, S_MIN=0.5), conservative profile, A's breakers — no tuning of any kind.
> - Two-stage validation, one attempt:
>   Stage 1 (ETH 2017-01-01..2023-12-31, frozen params, no search): run A (and its comparators C, BH, random as A's compare() does). PASS requires all of: net total return > 0; MDD ≤ 15%; A's Sharpe AND Calmar each > C's and > the random-entry control's mean (A's random control as in compare()). Also report the 2019–2023 sub-period and a 2× cost run as information only. Fail → A-ETH ends; A stays BTC-only; no other asset is tried.
>   Stage 2 (only if stage 1 passes): one-shot ETH holdout 2024-01-01..lock time with holdout-aeth.lock (pins A's fingerprint e0435223…, A params, ETH pre-2024 data hash, code state like A's lock) using A's evaluate_criteria UNCHANGED (A's §9.6 items 1–2 judged on the holdout; trade count recorded). Fail → A-ETH ends.
>   Stage 3 (only if stage 2 passes): paper trading of A on ETH (separate runtime aeth/paper.py adapting A's paper runner parameters: product, costs, lock; separate ledger branch paper-aeth; new workflow paper-aeth.yml dormant behind a repo variable PAPER_AETH_ENABLED like paper-b; second trigger documented). Separate counter: ETH needs its own 50 out-of-sample trades (holdout + paper) and ≥ 4 weeks of paper with decisions matching (A's criterion 4 style). ETH trades are NOT pooled into A-BTC's 50.
> - Live ETH is out of scope (Plan 5c, only after A-BTC CANARY passes and A-ETH passes stage 3): note the live runtime constraints (single product, account-level breakers/margin, any live code change restarts SHADOW).
> - Pre-registration statement and research-integrity caveats (A designed on BTC 2017–2023; BTC/ETH correlated; ETH 2024+ never downloaded; ETH 2017–2023 read only by ended strategy B at 15m).

### 1.4 결정 사항 요약

| 항목 | 결정 |
|---|---|
| 코드 위치 | 새 패키지 `src/ewbot/aeth/`(A의 지문 대상 폴더 밖). A의 엔진·전략·백테스트·홀드아웃·모의매매 모듈은 import만 한다 |
| 데이터 | Coinbase `ETH-USD` 현물 1시간봉, A와 같은 공개 엔드포인트(`data/coinbase_history.fetch_1h` import), 캐시 `data/cache/ETH-USD_1h.parquet`. `holdout-aeth.lock` 전에는 2024-01-01 이후 봉을 요청하지도 읽지도 않는다. A의 `update_cache`는 쓰지 않는다 |
| 비용 | A의 `CostParams`에 계약 크기만 0.1(`ETP-20DEC30-CDE`). 수수료 0.05%, 슬리피지 0.05%, 펀딩 연 10%(롱 부담), 계약당 최소 수수료 0.15는 A처럼 백테스트에 적용하지 않음 |
| 파라미터 | `holdout.lock`에서 읽은 A의 값 그대로(K4 2.0, Q_MIN 0.7, S_MIN 0.5), conservative 프로파일(1%, 3배), A의 손실 한도. 튜닝 없음 |
| 단계 1 | ETH 2017-01-01 ~ 2023-12-31, A의 `compare()`(A, C, HYBRID, BH, EMA, 무작위 100회). 합격: 순수익 > 0, 최대 낙폭 ≤ 15%, A의 샤프와 Calmar가 각각 C와 무작위 대조군 평균보다 큼. 2019~2023과 비용 2배는 정보로만 |
| 단계 2 | 단계 1 합격 시에만. `holdout-aeth.lock` 뒤 2024-01-01 ~ 잠금 시점의 ETH 홀드아웃 1회, A의 `evaluate_criteria` 그대로(A의 9.6절 1·2번을 홀드아웃에서 판정, 거래 수 기록) |
| 단계 3 | 단계 2 합격 시에만. ETH 모의매매(`aeth/paper.py`, 브랜치 `paper-aeth`, 휴면 워크플로 `paper-aeth.yml`과 저장소 변수 `PAPER_AETH_ENABLED`, 두 번째 트리거 문서화). ETH 전용 카운트 50건(홀드아웃 + 모의)과 4주 이상의 판단 재현(A의 4번 기준 방식). A-BTC의 50건과 합치지 않음 |
| 실패 | 어느 단계든 탈락하면 A-ETH를 끝낸다. A는 BTC 전용으로 남고, 다른 자산은 시도하지 않는다 |
| 실거래 | 범위 밖(Plan 5c). A-BTC CANARY 통과와 A-ETH 단계 3 충족 뒤에만 |

## 2. 사전 등록 선언

- **커밋 순서.** 이 명세와 구현 계획(`docs/superpowers/plans/2026-10-07-ewbot-plan5a-aeth.md`)은 브랜치 `feat/aeth`의 첫 커밋이며, ETH 1시간봉을 처음 받는 것(계획의 단계 1 수동 작업)보다 먼저다. 이 커밋 시점에 이 프로젝트의 어느 캐시에도 ETH 1시간봉은 없다(로컬 `data/cache/`에는 BTC 1시간봉과, 종료된 전략 B가 받은 BTC·ETH 15분봉 2017~2023만 있다).
- **고정되는 것.** 합격·탈락을 정하는 모든 것을 여기서 정한다: 파라미터(A의 잠금 파일), 실행 설정, 비용, 데이터 출처와 기간, 비교 대상, 무작위 대조군 크기, 합격 기준, 1회 규칙. 구현 계획은 이 판정 코드를 데이터 없이 합성 데이터 테스트로만 만든다.
- **결과를 본 뒤.** 단계 1이나 단계 2의 결과를 본 뒤에는 아무것도 고치지 않는다. 탈락이면 A-ETH를 끝내고 결과를 이 명세의 끝(14절)에 기록한다. 같은 데이터로 다시 시험하거나 기준을 바꾸지 않는다.
- **예외.** 데이터를 받았지만 단계 1이 계산되기 전에 기술적 이유로 막힌 경우(예: 수집을 끝까지 했는데도 2017~2023을 덮지 못함)는 결과를 본 것이 아니므로, 사용자 승인과 이 명세의 개정 뒤에 고칠 수 있다.

## 3. 격리 원칙

전략 A의 지문은 `types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/`, `strategy/`, `engine/`, `execution/`, `risk/`의 **폴더 전체**를 해시한다. 이 폴더에 파일 하나만 더해도 A의 지문이 바뀌고 BTC 50건 카운트가 영구히 리셋된다. 그래서:

- A-ETH 코드는 모두 `src/ewbot/aeth/`에 둔다. 수정하지 않는 것: A의 지문 대상 폴더(파일 추가 포함), `holdout.lock`, `src/ewbot/backtest/`, `src/ewbot/data/coinbase_history.py`, `src/ewbot/paper/`, `src/ewbot/live/`, `src/ewbot/stratb/`, `src/ewbot/paperb/`, `.github/workflows/paper.yml`·`ci.yml`·`paper-b.yml`, `scripts/`, `ops/`, `deploy/`, `uv.lock`, `pyproject.toml`. 새 의존성은 없다(`uv.lock`이 바뀌면 A의 모의매매가 `ENVIRONMENT_CHANGE`를 한 번 겪는다).
- 바꿀 수 있는 기존 파일은 셋뿐이다: `src/ewbot/cli.py`(`ewbot aeth …` 하위 명령), `README.md`(문서), `tests/paper/test_static.py`(테스트만, 최소 수정: A-ETH가 A 지문 밖에 있다는 CI 검사 1개, `allow_holdout=True`를 쓸 수 있는 모듈 목록에 `aeth/holdout.py`·`aeth/paper.py` 추가, 워크플로 목록에 `paper-aeth.yml` 추가와 그 워크플로가 A를 건드리지 않는다는 CI 검사 1개. `ci.yml`이 이 파일과 `test_fingerprint.py`만 실행하므로 A-ETH의 CI 검사는 여기에 둔다).
- `cli.py`는 `ewbot.aeth`를 `aeth` 명령의 처리 함수 안에서만 import한다. 그래서 `ewbot live …`와 `ewbot paper …`가 불러오는 모듈은 A-ETH 이전과 같다(테스트로 확인).
- **A-ETH의 지문은 둘이다(2026-10-08 개정, 15절).** A-ETH 지문은 `src/ewbot/aeth` 아래 모든 `*.py`이고 단계 1 기록, 잠금, 홀드아웃 실행이 묶는다: 단계 1부터 홀드아웃까지 A-ETH 파일은 하나도 바뀔 수 없다. A-ETH 판단 지문은 모의매매가 무엇을 거래하는지 정하는 `aeth/config.py`와 `aeth/data.py`만 해시하고, 모의매매 구간이 묶는다(8절). 나머지 A-ETH 파일(`paper.py`의 메시지·입출력, `sync.py`, 잠금 검사)과 `cli.py`는 홀드아웃 실행 뒤(모의매매 중)에 고쳐도 ETH 카운트를 리셋하지 않는다 — A의 지문이 런타임 `paper/runner.py`를 빼는 것과 같다.
- A-ETH의 산출물은 모두 따로다: 캐시 `data/cache/ETH-USD_1h.parquet`, 단계 1 기록 `aeth-stage1.json`, 잠금 `holdout-aeth.lock`, 장부 브랜치 `paper-aeth`, 워크플로 `.github/workflows/paper-aeth.yml`, Actions 캐시 키 `eth-usd-1h-`, 텔레그램 접두어 `[ewbot paper-A-ETH]`. A-ETH 코드는 A나 B의 산출물 이름을 쓰지 않고 `stratb`·`paperb`를 import하지 않는다.
- A 보호 테스트: `tests/aeth/test_isolation.py`(A 지문 고정값, `aeth/`가 지문 대상 밖, `master` 이후 A-ETH를 건드린 브랜치가 A·B 경로를 하나도 바꾸지 않음, 2024 봉을 여는 호출자 제한, 산출물 이름)와 위 `test_static.py`의 CI 부분.

## 4. 구조

```
src/ewbot/aeth/
  config.py       상품·계약(ETH-USD, ETP 0.1 ETH), ETH 비용, A의 잠금에서 읽는 고정 파라미터, 실행 설정, 경로·기간 상수
  fingerprint.py  A-ETH 지문(aeth/ 파일), A-ETH 판단 지문(config.py, data.py), 구간 지문(A 지문 + A-ETH 판단 지문)
  lockfile.py     holdout-aeth.lock의 다이제스트·존재 판정·읽기, 커밋 상태, 작업 트리 검사
  data.py         ETH 1시간봉 수집(fetch_1h import, 조각마다 저장·이어 받기)과 2024 가드
  stage1.py       단계 1: compare, 합격 판정, 정보 실행(2019~2023, 비용 2배), 1회 기록(aeth-stage1.json)
  holdout.py      단계 2: 잠금 작성(단계 1 기록에서), 1회 홀드아웃(A의 evaluate_criteria 그대로), 판정
  paper.py        단계 3: 매시 재계산(A의 paper/ 부품), 장부, ETH 카운트, 알림
  sync.py         paper-aeth 브랜치 git 동기화
.github/workflows/paper-aeth.yml     휴면 워크플로(PAPER_AETH_ENABLED)
aeth-stage1.json, holdout-aeth.lock  단계 1·2의 영구 기록(수동 작업만 만든다)
브랜치 paper-aeth                     ETH 모의매매 장부(orphan)
CLI                                   ewbot aeth fetch | stage1 | holdout lock | holdout run | paper run | paper status
```

## 5. 고정된 입력 (튜닝 없음)

### 5.1 파라미터와 실행 설정
- 파라미터는 실행할 때마다 커밋된 `holdout.lock`에서 읽는다(읽기만 한다). 그 파일이 A의 완료된 잠금이어야 한다: 키가 A의 그리드 키, 값이 그리드 조합, 홀드아웃 시작이 2024-01-01, `sha256`이 `lock_digest(BotConfig(), params, random_runs, code_sha256)`(A의 모의매매가 하는 검사와 같다). 현재 값은 K4 2.0, Q_MIN 0.7, S_MIN 0.5다.
- 나머지 설정은 A의 홀드아웃 설정 그대로다: `holdout_config(BotConfig(), params)`(셋업 4개, 파동·전략 기본값, conservative 1%·3배, A의 `BreakerParams()` — 하루 −3%, 전체 −15%, 5연패 24시간 휴지, 켜짐 —, 초기 자금 10,000 USD, 시드 0)에서 비용만 5.2로 바꾼다.
- CLI에는 파라미터, 상품, 비용, 기간, 무작위 대조군 크기를 바꾸는 옵션이 없다.

### 5.2 비용
- A의 `CostParams()`에 `contract_size = 0.1`(ETP 1계약 = 0.1 ETH)만 바꾼다: 수수료율 0.05%, 슬리피지 0.05%, 펀딩 연 10%(롱 부담·숏 수취), 계약당 최소 수수료 0.15 USD는 A처럼 적용하지 않는다(`apply_min_fee = False`).
- 실거래(Plan 5c) 전에 확인할 항목: ETP의 유동성과 실제 슬리피지(BIP보다 얇을 수 있다), ETP 계약당 최소 수수료, ETH 펀딩, 장중 증거금(약 10%).

### 5.3 데이터와 2024 가드
- Coinbase Exchange 공개 캔들 API(A와 같은 엔드포인트)의 `ETH-USD` 1시간봉을 2017-01-01부터 받는다. A의 `fetch_1h`를 import해 3,000시간 조각으로 부르고 조각마다 캐시에 저장한다(중단되면 이어 받는다). 진행 중인 시간은 저장하지 않고, 누락 봉은 캐시에서 채우지 않는다(계산할 때 A의 `fill_gaps`로 채운다).
- **가드.** 유효한 `holdout-aeth.lock`(종류와 다이제스트가 맞는 파일)이 없으면: 수집은 2024-01-01에 닿는 구간을 요청하지 않고, 로더는 parquet을 읽을 때 2024 이후 봉을 걸러내며, 2024 이후 봉을 여는 호출(`unlock_2024=True`)은 거부된다. 잠금 없이 캐시에 2024 이후 봉이 있으면 A-ETH가 받은 것이 아니므로 수집과 로더가 거부한다(예: A의 `ewbot fetch --product ETH-USD`는 같은 파일을 지금까지 채운다 — 이 명령은 쓰지 않는다).
- 신호는 A처럼 현물로 내고 비용은 선물(ETP) 기준으로 모델링한다.

## 6. 단계 1: 2017~2023 고정 파라미터 확인 (1회, 탐색 없음)

- **기간**: [2017-01-01, 2024-01-01). 2024 이후 봉은 없다(가드와 실행 함수가 모두 거부).
- **실행**: A의 `compare()`를 ETH 설정으로 1회: A, C(피보나치 스윙), HYBRID, BH(매수 후 보유), EMA 50/200, 무작위 진입 대조군 100회(A의 실제 거래 수와 거래 모양, 시드 0~99). 손실 한도는 켠다(A가 운용하는 설정 그대로; A의 워크포워드 격자 실행만 손실 한도를 껐고 단계 1은 격자가 아니다).
- **합격(모두 충족)**: A의 순수익률 > 0; A의 최대 낙폭 ≤ 15%; A의 샤프 > C의 샤프이고 > 무작위 대조군 샤프의 평균; A의 Calmar > C의 Calmar이고 > 무작위 대조군 Calmar의 평균. 값이 없거나 유한하지 않으면 그 항목은 불합격이다. BH, EMA, HYBRID는 보고만 한다(결정 사항이 C와 무작위 대조군만 기준으로 정했다).
- **정보로만**: 2019-01-01 ~ 2023-12-31 비교(2017부터의 봉으로 준비하고 2019년부터 거래, 같은 대조군 100회; 같은 항목을 계산해 보여 주지만 판정에 쓰지 않는다)와 수수료·최소 수수료·슬리피지를 2배로 한 A 1회(펀딩은 그대로).
- **기록**: `aeth-stage1.json`(저장소 루트)에 한 번만 쓴다. 기록 파일이 있으면, 추적 파일에 커밋 안 된 변경이 있으면, A의 지문이 `e0435223…`가 아니면, 봉이 2017-01-01과 2023-12-31 23:00을 각각 하루 안으로 덮지 못하거나 2024에 닿으면 실행 전에 거부한다. 기록에는 파라미터와 `holdout.lock`의 sha256, 전체 설정, 결과(전략별 지표, 대조군 분포, 합격 항목, A의 연도별 지표, 정보 실행), 출처(2024 이전 원본 봉의 수·첫·끝·누락 수·해시, A 지문, A-ETH 지문, `src/ewbot` 전체 코드 해시, git HEAD와 변경 여부, 시각)가 들어간다. 결과와 상관없이 **곧바로 커밋**한다.
- **탈락이면** A-ETH를 끝낸다: 잠금을 만들지 않고, 2024 이후 ETH 봉을 받지 않으며, A는 BTC 전용으로 남고, 다른 자산을 시도하지 않는다.

## 7. 단계 2: ETH 홀드아웃 1회 (단계 1 합격 시에만)

### 7.1 잠금
- `holdout-aeth.lock`은 2024 이후 ETH 봉을 받거나 읽기 **전에**, 커밋된 단계 1 기록에서만 만든다. 기록은 커밋된 그대로여야 하고, 합격이어야 하며(기록의 합격 항목이 기록의 결과로 다시 계산한 값과 같아야 한다), 지금의 A 파라미터와 `holdout.lock` sha256, 실행 설정, A 지문(`e0435223…`), A-ETH 지문, 2024 이전 봉으로 만든 것이어야 한다.
- 잠금이 고정하는 것: 상품과 계약, 파라미터와 `holdout.lock` sha256, 전체 설정, 홀드아웃 시작(2024-01-01), 홀드아웃의 마지막 봉(잠금 시각의 마지막 완성 시간), 무작위 대조군 100회, A 지문, A-ETH 지문, A-ETH 판단 지문(모의매매 구간의 기준값; 2026-10-08 개정), **A의 잠금과 같은 코드 상태**(`code_sha256`: A의 `code_fingerprint`, 곧 `src/ewbot` 아래 모든 `*.py`의 해시), 2024 이전 봉의 기록, 단계 1 기록 파일의 sha256, 그리고 이 필드들의 다이제스트 `sha256`. 잠금 시각과 git HEAD·변경 여부도 적는다. 추적 파일에 커밋 안 된 변경이 있으면 거부한다. 만든 뒤 곧바로 커밋한다.

### 7.2 1회 실행
- **사용자 승인 뒤 1회만** 실행한다. 실행 직전에 수집이 2024 이후 봉을 처음으로 받는다(잠금이 있으므로).
- 거부 조건: 확인 없음(`--confirm`), 잠금이 유효하지 않거나 커밋된 것과 다름, `first_run`이 이미 있음(파일이나 커밋된 버전), A 지문·A-ETH 지문·코드 상태가 잠금과 다름, 잠금 필드가 지금 A의 파라미터에 대한 이 코드의 값과 다름, 잠금 말고 추적 파일에 커밋 안 된 변경이 있음, 2024 이전 봉이 잠금과 다름, 데이터가 잠금의 마지막 봉에 닿지 않음, 끝나지 않은 이전 시도가 다른 홀드아웃 봉을 봄. 시도는 실행 전에 잠금에 기록한다.
- 실행: 2017년부터의 봉(누락 봉을 채운 것)을 마지막 봉에서 자르고 A의 `compare()`를 [2024-01-01, 마지막 봉 + 1시간)에서 `allow_holdout=True`로 1회, 무작위 대조군 100회. 결과(전략별 지표, 대조군 분포, A의 `evaluate_criteria` 출력 전체, 판정, 홀드아웃 구간 봉 해시)를 잠금의 `first_run`에 쓰고 **곧바로 커밋**한다.

### 7.3 합격 기준
- A의 `evaluate_criteria`를 바꾸지 않고 쓴다. 합격 = `winners`에 **A**가 있음(A의 샤프와 Calmar가 C, BH, 무작위 대조군 평균보다 모두 큼; 대조군이 계산됨) **그리고** A의 최대 낙폭 ≤ 15% — A의 9.6절 1·2번을 ETH 홀드아웃에서 판정한 것이다. HYBRID의 결과는 보여 주기만 한다(검증 대상은 A다). 거래 수는 기록하고 단계 3의 50건 카운트에 들어간다(9.6절 3번은 단계 3에서 따로 센다).
- 탈락이면 A-ETH를 끝낸다(모의매매 없음).

## 8. 단계 3: ETH 모의매매 (단계 2 합격 시에만)

| 항목 | 내용 |
|---|---|
| 런타임 | `aeth/paper.py`. A의 `paper/runner.py`를 상품(ETH-USD), 비용(ETP), 잠금(`holdout-aeth.lock`)만 바꿔 옮긴 것이다. A의 `paper/` 부품(추적 엔진, 판단 기록과 대조, 장부 스키마, 구간, 카운트, 4번 기준, 실행 환경, 텔레그램 전송과 형식)을 import만 한다 |
| 방식 | 매 실행 2017년부터 결정적으로 다시 계산하고 2024-01-01부터 거래한다(홀드아웃을 재현하고 이어 간다). 판단 불일치는 A의 규칙대로 `DATA_REVISION`, `FINGERPRINT_CHANGE`, `ENVIRONMENT_CHANGE`, `UNEXPLAINED`로 분류한다 |
| 사전 점검 | 잠금이 유효하고 커밋된 그대로이며, 홀드아웃이 실행되어 합격했고, 잠금 필드가 지금 A의 파라미터에 대한 이 코드의 값과 같아야 한다. 아니면 거부하고 알린다 |
| 장부 | orphan 브랜치 `paper-aeth`의 `ledger.json`, `runs.log`(A의 스키마). `report.html`은 없다 |
| 구간 (2026-10-08 개정) | 구간 지문 = (A 지문, A-ETH 판단 지문: `aeth/config.py`·`aeth/data.py`). 어느 쪽이든 바뀌면 새 구간을 열고 ETH 카운트를 0부터 센다. 기준값은 잠금의 두 값이다. 모의매매 중 런타임만 고친 것(`aeth/paper.py`의 메시지·재시도·장부 입출력·알림, `aeth/sync.py`, `cli.py`)은 구간을 바꾸지 않는다. `aeth/paper.py`가 계산하는 것(데이터 경로, 재계산, 카운트)은 모의매매 중에도 고치지 않는다(15절) |
| 카운트 | ETH 표본 외 거래 = 홀드아웃의 A 거래(첫 구간일 때) + 잠금의 마지막 봉 이후 진입한 모의 거래. 50건 필요. A-BTC의 50건과 합치지 않는다 |
| 재현 | A의 4번 기준 방식: 모의매매 28일 이상, 그 구간의 `UNEXPLAINED` 0건, 매시간 실행 커버리지 95% 이상 |
| 워크플로 | `.github/workflows/paper-aeth.yml`. 저장소 변수 `PAPER_AETH_ENABLED`가 `true`일 때만 돈다(합격 전에는 휴면). 1차 트리거는 두 번째 Cloudflare Worker의 크론 `42 * * * *`(A의 Worker는 `paper.yml`만 부르므로 따로 만든다; 코드는 `docs/aeth.md`), 예비 `schedule`은 `42 */3 * * *` |
| 알림 | 같은 텔레그램 봇, 접두어 `[ewbot paper-A-ETH]`. 진입·손절 이동·청산(ETH 누적 n/50)·뉴욕 9시대 일일 요약(포지션, 자산, n/50, 8.1의 조건)·경고·실패 |
| 하지 않는 것 | 매일의 9.6절 1·2번 재계산(대조군 100회, 하루 10~20분): Actions 사용 시간 때문에 하지 않는다. 1·2번은 단계 2가 홀드아웃에서 판정했고, Plan 5c에서 전체 표본 외 구간으로 한 번 다시 판정한다 |
| 사용량 | 실행마다 1~2분(매시 24회 + 예비 8회, 월 약 1,000~2,000분)이 A의 모의매매 위에 더해진다. A만으로도 무료 2,000분 근처라 사용자의 소액 Actions 예산으로 넘는 분을 덮는다. 매시 트리거는 줄이지 않는다(4번 기준이 매시 커버리지를 센다) |

### 8.1 A-ETH 실거래 검토 조건 (Plan 5c 진입 조건)
1. 단계 2 합격(7.3)
2. ETH 표본 외 거래 50건 이상(홀드아웃 + 모의, 기준 구간)
3. 모의매매 재현(28일, `UNEXPLAINED` 0건, 커버리지 95%)
4. A-BTC의 CANARY 통과
5. 전체 ETH 표본 외 구간에서 A의 9.6절 1·2번을 한 번 다시 판정(Plan 5c에서 수동)
6. 사용자 명시 승인

1~3은 모의매매가 자동으로 표시하고, 4~6은 수동이다. 자동 전환은 없다.

## 9. 실거래 (범위 밖, Plan 5c) — 지금 알려진 제약

- **단일 상품.** Plan 3의 실거래 런타임은 `BIP-20DEC30-CDE` 한 상품과 그 장부만 다룬다. ETH에는 상품을 매개변수로 받는 런타임이나 두 번째 런타임이 필요하다.
- **계좌 단위의 손실 한도와 증거금.** A의 손실 한도(하루 −3%, 전체 −15%, 연패 휴지)와 CFM 증거금은 계좌 단위다. 같은 계좌에서 A-BTC와 A-ETH를 함께 돌리면 둘이 한도와 증거금을 공유한다(별도 계좌나 포트폴리오를 쓸지는 Plan 5c에서 정한다).
- **코드 변경은 SHADOW를 다시 시작한다.** 실거래 운영 규칙상 SHADOW가 시작된 뒤의 코드 변경은 14일 카운트를 리셋한다. A-ETH는 `master`에 병합되어도 VPS에 배포되지 않고(배포는 태그로만), `ewbot live …`는 `ewbot.aeth`를 불러오지 않는다. 그래도 `cli.py`가 바뀌므로, A-ETH만을 위해 새 태그를 배포하지 않는다. 다른 이유로 배포하면 그 규칙을 그대로 따른다.
- ETP의 유동성·슬리피지, 최소 수수료, 펀딩, 증거금은 5.2의 확인 항목이다.

## 10. 오류 처리
- 수집 실패: A의 `fetch_1h`는 429·5xx를 3회 재시도한다. 모의매매는 수집 전체를 3회(10초, 20초 간격) 시도한 뒤 실패를 알리고 실행을 멈춘다(부분 데이터로 판단하지 않는다). 연구 명령은 다시 실행하면 저장된 조각부터 이어 받는다.
- 2024 가드 위반, 잠금·기록 불일치, 더러운 작업 트리: 거부하고 이유를 출력한다(모의매매는 `precheck` 실패 알림).
- 홀드아웃 구간 봉의 해시가 `first_run`과 다름(캔들 수정, 캐시 재수집): 거부하지 않는다. A처럼 해시마다 한 번 경고하고, 그 때문에 바뀐 판단은 `DATA_REVISION`이다. 재계산한 홀드아웃 거래 수가 잠금과 다르면 같은 방식으로 한 번 경고한다.
- 홀드아웃의 마지막 봉이 원본에 없으면 캐시 전체를 `fill_gaps`로 채운 뒤 자른 값, 곧 직전 종가로 평평하게 채운 봉을 쓴다(뒤의 봉이 있어야 누락으로 드러난다; 모의매매와 같은 방식이라 해시가 일치한다; 2026-10-08 문구 정정).
- A·B와의 충돌: A-ETH가 A·B의 파일, 브랜치, 워크플로, 캐시 키를 건드리지 않음을 테스트로 보장한다.

## 11. 테스트

| 종류 | 확인 내용 |
|---|---|
| 설정 | ETH 비용(계약 크기만 다름), 실행 설정 = A의 홀드아웃 설정 + ETH 비용, 실제 `holdout.lock`에서 읽은 값이 (2.0, 0.7, 0.5)(읽기만), A의 완료된 잠금이 아니면 거부 |
| 지문 | A-ETH 지문과 판단 지문의 파일 집합, A-ETH 편집은 A 지문을 바꾸지 않음, A 편집은 A-ETH 지문을 바꾸지 않음, 판단 파일 편집과 A 편집은 구간 지문을 바꾸고 다른 A-ETH 파일 편집은 바꾸지 않음(A-ETH 지문은 바꿈), 판단 파일은 A-ETH에서 판단 파일과 `lockfile`만 import, 둘 밖의 파일은 아무것도 바꾸지 않음 |
| 데이터 가드 | 잠금 전 수집이 2024에 닿는 구간을 요청하지 않음, 조각 저장과 이어 받기, 잠금 뒤 지금까지 수집, 로더의 필터와 열기 조건, 잠금 없는 2024 봉 거부, 다이제스트가 맞는 잠금만 유효 |
| 단계 1 | 판정 항목마다의 탈락, 값이 없으면 탈락, 대조군이 없으면 탈락, 합성 데이터의 실제 `compare` 실행(ETH 비용, 정보 실행), 2024 거부, 기록의 출처, 1회·더러운 트리·지문·기간 거부 |
| 단계 2 | 판정(A가 winners에 있고 낙폭 ≤ 15%, HYBRID만으로는 탈락), 잠금이 묶는 필드, 잠금 거부(미커밋·수정·탈락·손으로 바꾼 합격·출처 불일치·더러운 트리·2024 전 시각·2024 봉), 합성 데이터의 실제 1회 실행(A의 `evaluate_criteria` 그대로), 재실행 거부, 실행 거부 조건 전부, 마지막 봉 누락 처리 |
| 모의매매 | 홀드아웃 재현과 모의 거래·카운트(홀드아웃 + 모의 / 50), 접두어, 같은 시간 재실행, 새 봉, 일일 요약, 구간 변경과 카운트 리셋, 모의매매 중 런타임 수정은 구간 유지·`config.py` 수정은 새 구간, 사전 점검 거부, 데이터 해시·거래 수 경고, 수집 재시도, 다른 잠금의 장부 거부, 상태 줄 |
| CLI | 하위 명령과 조정 옵션 없음, `ewbot.cli`가 `ewbot.aeth`를 불러오지 않음, 전체 기간으로만 단계 1, 1회, 잠금 전 단계 1 기록 필요, 거부는 `refusing:`과 종료 코드 2(캐시의 2024 봉 포함), 홀드아웃 실행 전 다운로드 없음, 모의매매 실행·상태·실패 알림 |
| 동기화·워크플로 | `paper-aeth` 브랜치(orphan 시작, `paper`·`paper-b` 불변, 재시도, 원격 이동 시 재계산), 워크플로 모양(휴면, 고정 Python, 시크릿 범위, 접두어, A·B 이름 없음) |
| A 보호 | A 지문 `e0435223…` 유지, `aeth/`가 지문 밖, A·B 경로 불변, 2024 봉을 여는 호출자 제한, A·B 산출물 이름 없음, CI 부분(`test_static.py`) |

테스트는 합성 데이터만 쓴다(네트워크, 실제 캐시 없음; 실제 `holdout.lock`은 설정 테스트가 읽기만 한다).

## 12. 진행 절차
1. 이 명세와 구현 계획 커밋(사전 등록, ETH 데이터 없음)
2. 작업별 구현과 검토, 마지막 전체 검토, PR. A의 CI 지문 검사 통과가 병합 조건이다(사용자 병합)
3. 단계 1(컨트롤러): 2024 이전 ETH 1시간봉 수집, 단계 1 1회, `aeth-stage1.json` 즉시 커밋, 보고. 탈락이면 14절에 기록하고 끝낸다
4. 단계 2: 잠금 작성·즉시 커밋 → **사용자 승인** → 홀드아웃 1회 → 즉시 커밋 → PR(사용자 병합). 탈락이면 14절에 기록하고 끝낸다
5. 단계 3(사용자): `PAPER_AETH_ENABLED` 켜기, 두 번째 Cloudflare Worker, 첫 실행 확인, 첫 주 사용량 확인

## 13. 연구 정직성 주의
- **A는 BTC로 설계되었다.** 규칙, 고정 가중치, 1시간 트리거(N = 3; BTC의 빈도 자료로 정함), 파라미터(BTC 2017~2023 워크포워드)가 모두 BTC에서 나왔다. ETH 2017~2023은 시간으로는 A의 설계 기간과 같고 자산으로만 표본 밖이다.
- **BTC와 ETH는 강하게 상관되어 있고, A의 BTC 홀드아웃 결과는 이미 알려져 있다**(2024-01-01 ~ 2026-10-02: 샤프 0.97, Calmar 0.90, 최대 낙폭 2.9%, 36건, C·BH·무작위 대조군보다 위험 대비 수익이 높음). ETH 홀드아웃은 거의 같은 기간이다. 그래서 ETH 합격은 독립된 증거로 약하다: 같은 시장 국면을 다시 본 것일 수 있다. 합격은 "버릴 이유가 없다"로만 해석하고, 확인은 모의매매로 한다.
- **선택 효과.** ETH 시험은 A의 BTC 홀드아웃이 좋아 보였고 전략 B가 실패한 뒤에 정했다. 이를 1회 규칙, 사전 등록, "다른 자산은 시도하지 않는다"로 제한한다.
- **ETH 2024 이후 데이터는 이 프로젝트에서 받은 적이 없다.** 전략 B의 가드가 ETH 15분봉을 2023-12-31에서 멈췄고, A-ETH의 가드는 잠금 전까지 같은 일을 한다.
- **ETH 2017~2023은 종료된 전략 B만 15분봉으로 읽었다.** B 자신의 규칙(15분봉 다이버전스 진입)을 위한 것이었고 A를 ETH에 돌린 사람은 없다. B의 워크포워드 결과(BTC+ETH 포트폴리오, 모든 조합 음수)는 A-ETH의 어떤 선택에도 쓰이지 않았다(A-ETH에는 고를 자유 파라미터가 없다).
- **비용 가정.** ETP는 BIP보다 유동성이 얇을 수 있어 0.05% 슬리피지가 낙관적일 수 있다. 비용 2배 정보 실행이 그 민감도를 보여 준다. 최소 수수료와 펀딩도 실거래 전에 확인한다.
- **변동성.** ETH는 BTC보다 변동성이 커서 같은 1% 위험에서도 연속 손실과 낙폭이 클 수 있다. 낙폭 15% 기준은 A와 같게 둔다.

## 14. 결과 기록
단계 1·2의 결과(합격이든 탈락이든)는 이 절에 덧붙인다. 탈락이면 여기에 종료를 기록한다.

## 15. 개정 기록

### 2026-10-08 — 모의매매 구간 지문을 A-ETH 판단 지문으로 좁힘 (단계 1 전, ETH 1시간봉을 받기 전)

최종 검토(I1)의 (b)안, 컨트롤러 결정. ETH 1시간봉은 아직 받지 않았고 `aeth-stage1.json`·`holdout-aeth.lock`도 없다(결과를 본 적이 없다). 합격·탈락을 정하는 것(파라미터, 실행 설정, 비용, 데이터 출처와 기간, 비교 대상, 무작위 대조군 크기, 단계 1·2의 기준, 1회 규칙)은 하나도 바꾸지 않는다.

- **무엇.** 모의매매 구간 지문이 (A 지문, A-ETH 지문 전체)에서 (A 지문, A-ETH 판단 지문)으로 바뀐다(3, 4, 7.1, 8, 11절). 잠금은 판단 지문도 묶는다(`aeth_decision_sha256`, 구간의 기준값). 단계 1 기록 → 잠금 → 홀드아웃 실행은 그대로 A-ETH 지문 전체와 `code_sha256`을 묶는다.
- **이유.** 전에는 모의매매 중 `sync.py`의 git 재시도나 `paper.py`의 메시지 하나만 고쳐도 새 구간이 열려 홀드아웃의 ETH 거래가 카운트에서 영구히 빠지고, 장부가 "판정 대상 아님"이 되었다. 판단을 바꾸지 않는 실행 코드 수정으로 ETH 카운트를 리셋하지 않기 위해서다. A의 지문도 같은 이유로 런타임(`paper/runner.py`, `scripts/paper_sync.sh`)을 뺀다.
- **판단 파일 = `aeth/config.py`, `aeth/data.py`.** 둘은 모의매매가 무엇을 거래하는지 정한다: `config.py`는 파라미터(A의 잠금에서 읽음), 실행 설정과 ETH 비용, 상품, 첫 봉을, `data.py`는 어떤 ETH 봉을 받고 어떻게 읽는지를 정한다. 둘의 변경은 재계산의 봉이나 설정을 바꾸는데, 봉이 바뀌면 판단 대조가 그 변화를 `DATA_REVISION`(설명된 불일치)으로 분류하므로 지문으로만 드러난다. 나머지 A-ETH 파일은 판단을 바꾸지 않는다: `lockfile.py`와 `holdout.py`의 `lock_mismatch`는 실행을 허용하거나 거부하는 검사이고, `stage1.py`와 홀드아웃 실행은 모의매매가 쓰지 않으며, `sync.py`는 git이고, `fingerprint.py`는 지문 계산이다(판단 파일 집합을 바꾸면 값이 바뀌어 새 구간이 열린다). 판단 파일은 A-ETH에서 판단 파일과 `lockfile`(잠금 확인)만 import한다(테스트).
- **남는 한계.** `aeth/paper.py`는 A의 `paper/runner.py`처럼 구간 지문 밖인데, 그 안의 데이터 경로(`load_eth` → `fill_gaps`)와 재계산(`run_backtest`의 구간), 카운트는 판단에 쓰인다. 모의매매 중에는 이 부분을 고치지 않고 메시지·재시도·장부 입출력·알림만 고친다(홀드아웃 실행 전에는 단계 1부터 A-ETH 파일을, 잠금부터 `src/ewbot`의 모든 `*.py`를 바꿀 수 없다). 고치면 새 구간이 열리지 않고 바뀐 판단이 불일치로만 보인다(`UNEXPLAINED`, 봉이 달라졌으면 `DATA_REVISION`과 홀드아웃 데이터 해시 경고).
- **문구 정정(10절).** 마지막 봉 누락 처리의 설명을 실제 동작에 맞게 고쳤다(직전 종가로 평평하게 채운 봉; 동작은 그대로).
