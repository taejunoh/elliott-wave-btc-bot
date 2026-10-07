# ewbot Plan 4 — 전략 B(매억남식 15분봉 전략) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 A를 전혀 건드리지 않고, 1시간봉 스윙 구조에서 피보나치 되돌림과 매물대가 겹치는 자리를 찾아 15분봉 RSI 다이버전스와 돌파를 확인한 뒤 진입하는 전략 B를 만들고, A와 같은 절차(2017~2023 워크포워드 → 2024-01-01 이후 홀드아웃 1회 → 모의매매)로 검증할 연구 코드와 모의매매 런타임(`paper-b`)을 만든다.

**Architecture:** 새 코드는 `src/ewbot/stratb/`(전략·연구, B 지문 대상)와 `src/ewbot/paperb/`(모의매매 런타임)에만 둔다. A의 모듈(비용, 사이징, 손실 한도, 지그재그, 지표, 성과 지표, 워크포워드 구간, 장부, 판단 대조, 알림)은 import만 한다. 15분봉 엔진은 마감된 봉으로만 판단하고(자른 데이터와 전체 데이터의 판단이 같음을 테스트), 시간 단위 자산 곡선을 내므로 A의 성과 지표 계산을 그대로 쓴다. 2024+ 데이터는 `holdout-b.lock`이 생기기 전에는 데이터 로더 단계에서 읽히지 않는다. 모의매매는 매 실행 2023-10-01부터 전체를 결정적으로 다시 계산하고, A의 장부 스키마와 불일치 분류로 `paper-b` 브랜치에 기록한다.

**Tech Stack:** Python 3.12 (uv), 기존 의존성만(numpy, pandas 3, pyarrow, requests, PyYAML); pytest; GitHub Actions(`actions/checkout@v7`, `astral-sh/setup-uv` v10.2.0 커밋 `c18668ad3cf93ea998bef934396af7bb5c839dc7`, `actions/cache@v6`); Coinbase Exchange 공개 캔들 API(granularity 900); Telegram Bot API(A와 같은 봇·시크릿).

**Spec:** `docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md`(사용자 승인 2026-10-07). 상위 명세: Plan 1 `2026-10-01-elliott-wave-btc-bot-design.md`, Plan 2 `2026-10-02-ewbot-plan2-paper-trading-design.md`.

## Global Constraints

- 작업 브랜치 `feat/plan4-strategy-b`, 저장소 루트에서 실행한다. Python 3.12(uv). 테스트 명령은 `uv run --group live pytest …`. 기준선(`master`, 이 계획 이전): `1246 passed`(`data/cache/BTC-USD_1h.parquet`가 없으면 `1245 passed, 1 skipped`).
- **A의 지문 대상은 절대 수정하지 않는다**: `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/**`, `strategy/**`, `engine/**`, `execution/**`, `risk/**`. 지문은 폴더 전체를 해시하므로 이 폴더들 안에 **파일을 추가하지도 않는다**.
- 다음도 수정하지 않는다: `holdout.lock`, `src/ewbot/backtest/**`, `src/ewbot/data/coinbase_history.py`, `src/ewbot/paper/**`, `src/ewbot/live/**`, `.github/workflows/paper.yml`, `.github/workflows/ci.yml`, `scripts/**`, `ops/**`, `pyproject.toml`, `uv.lock`. 새 의존성을 추가하지 않는다(`uv.lock`이 바뀌면 A의 모의매매가 `ENVIRONMENT_CHANGE`를 한 번 겪는다).
- 만들 수 있는 것: `src/ewbot/stratb/**`, `src/ewbot/paperb/**`, `tests/stratb/**`, `tests/paperb/**`, `.github/workflows/paper-b.yml`, `docs/strategy-b.md`. 수정할 수 있는 것: `src/ewbot/cli.py`(`stratb` 하위 명령), `README.md`, `tests/paper/test_static.py`(Task 12에서 워크플로 목록 테스트 1개를 고치고 B 보호 테스트 2개를 추가: `ci.yml`은 `tests/paper/test_fingerprint.py`와 이 파일만 실행하므로 B의 CI 검사는 여기에 둔다).
- A 지문은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`로 유지된다(`tests/paper/test_fingerprint.py`, `tests/stratb/test_isolation.py`, Task 13).
- B 지문 = `src/ewbot/stratb/` 아래 모든 `*.py`의 sha256(A와 같은 형식: `ewbot.paper.fingerprint.fingerprint_of`). `paperb/`와 `cli.py`는 포함하지 않는다. 바뀌면 B의 모의매매 구간(epoch)만 새로 열린다.
- **2024-01-01 00:00 UTC(`HOLDOUT_START = 1704067200`) 이후 데이터는 `holdout-b.lock`이 생기기 전에 읽을 수 없다**: `stratb.data15.load_15m`은 parquet을 읽을 때 걸러내고, `update_cache_15m`은 내려받지 않으며, `include_holdout=True`는 잠금 파일이 있어야 한다. 리터럴 `include_holdout=True`는 `stratb/holdout.py`와 `paperb/runner.py`에서만 쓴다. B 코드는 A의 `allow_holdout` 키워드와 `_init_worker`·`_map_tasks`·`check_before_holdout` 이름을 쓰지 않는다(A의 `tests/paper/test_static.py`가 `src/ewbot` 전체를 검사한다).
- 결정성: 판단은 마감된 15분봉으로만 하고, 데이터의 마지막 봉에서는 진입을 평가하지 않는다. 시각 t에서 자른 데이터의 판단은 전체 데이터의 t까지 판단과 같아야 한다.
- 고정값(명세 5절): RSI 14, 피벗 좌우 3봉, 매물대 허용 ±0.25×ATR1h, 손절 여유 0.2×ATR15m, 최대 손절 2.5×ATR1h, 확인 기한 72시간, 시간 손절 48시간, ATR 14. 그리드 32개: `zz_k` (2.0, 3.0) × 되돌림 구간 ((0.382, 0.618), (0.5, 0.786)) × `tp1_r` (1.0, 1.5) × `e` (1.272, 1.618) × `remainder` (breakeven, original).
- 위험·비용(명세 4.4, 6.2): 거래당 0.5%, 레버리지 상한 3배(`RiskProfile("stratb", 0.005, 3.0)`), A의 `BreakerParams()`(하루 −3%, 전체 −15%, 5연패 후 24시간 휴지), A의 `CostParams()`(수수료 0.05%, 슬리피지 0.05%, 펀딩 연 10%: 15분봉마다 시간당 값의 1/4), 초기 자금 10,000 USD, 0.01 BTC 정수 계약으로 내림.
- `REPLAY_ANCHOR` = 2023-10-01 00:00 UTC(`1696118400`): 홀드아웃 실행과 모의매매 재계산의 시작점(명세 6.4).
- 테스트는 네트워크, 실제 `data/cache`, 실제 `holdout-b.lock`·`holdout.lock`을 쓰지 않는다(합성 15분봉, 임시 git 저장소, 가짜 세션·발송기·수집기). 실제 데이터 수집, 워크포워드, 잠금, 홀드아웃은 Task 14(수동, 사용자 승인)에서만 한다.
- 모든 커밋 메시지는 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 줄로 끝난다(`git commit -m "<제목>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`).

## 명세 해석 (이 계획이 정함, 모든 작업에 구속력)

1. **A의 워크포워드 규칙(실제 값, `src/ewbot/backtest/walkforward.py`를 2026-10-07에 확인).** 구간은 `folds()`: 학습 24개월, 확인 6개월, 6개월씩 이동, 첫 확인 2019-01-01, 마지막 확인 끝 2024-01-01로 10개다. 각 조합을 [2017-01-01, 2024-01-01)에서 **한 번** 돌리고(손실 한도 끔: `breakers.enabled=False`) 구간은 그 결과를 잘라 쓴다. 최소 거래 수는 학습 창에서 `MIN_TRAIN_TRADES = 12`건이고 Calmar가 유한해야 한다. 선택 점수는 그리드 이웃(모든 축에서 인덱스 차 ≤ 1, 자신 포함) 가운데 적격 조합의 Calmar 평균(plateau)이며, 자신을 뺀 이웃의 50%(`MIN_ELIGIBLE_NEIGHBOUR_FRAC`) 이상이 적격일 때만 고를 수 있고, 동점이면 자신의 Calmar, 그다음 작은 그리드 인덱스를 고른다. 표본 외 곡선은 각 구간 확인 창의 시간별 수익률을 이어 붙인 복리다. 최종 조합은 무언가를 고른 **마지막 구간**의 조합(`chosen_final`)이고, 그것이 마지막 구간이 아니면 `chosen_final_fallback`이다(A의 CLI는 이 경우 홀드아웃을 거부한다). **재사용:** `folds`, `oos_returns`, `compute_metrics`, `MIN_TRAIN_TRADES`, `MIN_ELIGIBLE_NEIGHBOUR_FRAC`, `HOLDOUT_START`, `WF_START`은 import한다. A의 `pick_plateau`·`evaluate_folds`·`run_grid`는 A의 전역 `GRID`(K4, Q_MIN, S_MIN)와 A의 엔진에 묶여 있어 B 그리드에 쓸 수 없으므로, 같은 규칙을 `stratb/research.py`에 다시 구현한다.
2. **2단계 그리드에서는 plateau 규칙이 퇴화한다.** 축마다 값이 2개라 모든 조합이 서로 이웃이다. 그래서 점수가 모든 조합에서 같고(적격 조합 전체의 평균), 결과는 "32개 중 17개 이상이 적격일 때 자신의 Calmar가 가장 높은 적격 조합"이 된다. 명세가 "A와 같은 규칙"을 요구하므로 그대로 구현하고 이 동작을 테스트로 고정한다(사용자에게 보고할 위험 요소: 과최적화 방어가 약해진다).
3. **워크포워드 종료(명세 6.3).** `proceed = (chosen_final 있음) and (fallback 아님) and (이어 붙인 표본 외 총수익 ≥ 0)`. 거짓이면 잠금을 만들지 않고 B를 끝낸다(`ewbot stratb holdout lock`이 거부한다).
4. **기준 구간은 방향마다 따로(명세 4.1 "가장 최근에 확정된 상승 구간").** 롱은 가장 최근에 확정된 상승 구간(L → H), 숏은 가장 최근의 하락 구간(H → L)을 기준으로 하며, 같은 방향의 새 구간이 확정되거나 72시간이 지나거나 무효가 될 때까지 유효하다. 되돌림 저점이 1시간 피벗으로 확정되어도 롱의 기준 구간은 남는다(돌파 랠리가 먼저 그 저점을 확정해 롱이 사라지는 것을 막는다). 기준 구간 하나에서 신호는 하나다. 같은 봉에서 롱과 숏이 함께 확인되면 더 새로운 구간의 신호만 쓴다.
5. **매물대(명세 4.2)** = 기준 구간의 시작 피벗(L) **이전**의 1시간 지그재그 피벗 가운데 L보다 최대 30일(`confluence_lookback_hours = 720`) 앞선 것. 명세에 기간이 없어 정했다: 전 기간을 보면 2017년부터 데이터를 가진 워크포워드와 2023-10-01부터 다시 계산하는 홀드아웃·모의매매가 서로 다른 매물대를 보게 된다(30일은 앵커와 홀드아웃 시작 사이의 3개월 안에 들어간다). 허용 폭의 ATR1h는 구간이 확정된 시점의 값이다.
6. **다이버전스와 돌파(명세 4.3).** 15분 피벗 저점은 왼쪽 3봉보다 엄격히 낮고 오른쪽 3봉보다 같거나 낮은 봉이며(고점은 대칭), 피벗 j는 봉 j+3 마감에 알려진다. 기준 구간 극값이 나온 15분봉 이후의 피벗만 본다. 연속한 두 피벗이 가격은 더 낮고 RSI는 더 높으며 두 번째가 되돌림 구간 안이면 다이버전스 쌍이 된다. 이 쌍은 더 새로운 쌍이 생기거나 이후 봉이 두 번째 저점을 깨면 사라진다. 확인은 그 뒤 15분 종가가 두 피벗 사이 봉들의 최고가(숏은 최저가)를 처음 넘는 봉이다.
7. **진입가 기준(명세 4.4).** 손절·목표·걸러내기·수량은 확인 봉 종가(`entry_ref`)로 정하고(A의 `Signal.entry_ref`와 같은 방식) 실제 체결은 다음 15분봉 시가 ± 슬리피지다. 다음 시가가 이미 손절가에 닿았거나 그 봉이 누락 봉이면 진입하지 않는다. 2차 목표가 1차 목표보다 가깝거나 **같으면** 진입하지 않는다.
8. **누락 봉(명세 6.1, 8).** 캐시에는 채우지 않는다. 엔진은 A의 `fill_gaps(df, tf=900)`로 평평한 합성 봉을 넣어 시간 격자를 잇고, p1−3봉부터 확인 봉까지 합성 봉이 하나라도 있으면 신호를 내지 않는다.
9. **체결(명세 6.2).** A의 `PaperBroker` 규칙을 15분봉에 그대로 쓴다: 시가가 손절가를 넘으면 시가에 손절, 같은 봉에서 손절가에 닿으면 손절가에 손절(목표보다 우선), 아니면 TP1(절반, 지정가, 엄격한 돌파)과 TP2. 1계약이면 TP1에서 전량. 본절 이동은 TP1이 체결된 봉의 다음 봉부터. 손실 한도 관측은 매시 마지막 15분봉 마감(A와 같은 시각). 자산 곡선은 시간 단위(시각 = 그 시간의 시작)라서 A의 `compute_metrics`(연 8760시간)를 그대로 쓴다. Coinbase 선물의 금요일 정기 휴장은 명세에 없어 모델링하지 않는다(실거래는 Plan 5).
10. **무작위 대조군(명세 6.5-1).** 같은 기간, 같은 진입 횟수(B의 거래 수), 같은 엔진(청산, 시간 손절, 잔량 규칙, 0.5% 수량, 3배 상한, 손실 한도, 비용). B 거래의 모양(방향, 기준가 대비 손절 거리, TP1·TP2의 R 배수)을 시드 고정 무작위 순서로 무작위 15분봉(마지막 봉·누락 봉·누락 체결 봉 제외)에 놓는다. 포지션 중에 온 진입은 B와 똑같이 건너뛰므로 실현 횟수는 같거나 적다. 1,000회(시드 0~999), B의 샤프를 대조군 샤프의 95번째 백분위수와 비교한다.
11. **잠금과 1회 실행(명세 6.4).** `holdout-b.lock`은 2024+ 데이터를 보기 **전에** 만든다. 조합, 전체 실행 설정, 그리드, B 지문, 2024 이전 15분봉 해시(`wf_data`), 홀드아웃 시작, 앵커, 무작위 대조군 1,000회와 시드, `last_bar_ts`(잠금 시각의 마지막 완성 시간의 마지막 15분봉 = "잠금 시점의 마지막 확정 봉"), 그리고 이 필드들의 다이제스트 `sha256`을 담는다. 홀드아웃 데이터의 해시는 잠금 시점에 읽을 수 없으므로 `first_run.data`에 기록한다. 홀드아웃은 잠금이 커밋되어 있고, `first_run`이 비어 있고, B 지문과 2024 이전 데이터가 잠금과 같을 때만 1회 돈다. 시도는 실행 전에 기록되고, 끝나지 않은 시도 뒤의 재시도는 같은 홀드아웃 데이터일 때만 허용된다.
12. **모의매매의 잠금 불일치(명세 7, 8).** 잠금 파일 자체(다이제스트, 커밋된 그대로인지, 설정이 이 코드의 `config_for(params)`와 같은지, `first_run`이 합격인지)가 맞지 않거나 홀드아웃 구간 15분봉의 해시가 `first_run.data.sha256`과 다르면 실행을 거부하고 알린다. 현재 B 지문이 잠금의 `code_sha256`과 다르면 거부하지 않고 새 구간을 연다(명세 7절).
13. **판단 기록.** A의 판단 형식과 대조 함수(`verify_decisions`, `decisions_to_record`, `classify`, `same_decision`)를 그대로 쓴다. B의 종류는 SIGNAL, ENTRY, TP1, TP2, STOP, EXIT(TIME, BREAKER), BREAKEVEN이다. A의 `known_at`은 SIGNAL을 ts + 1시간으로 셈하는데 B의 신호는 ts + 15분부터 알 수 있다. 기록 시점은 같고 APPEARED 검사만 최대 45분 늦어진다(오탐 없음).
14. **모의 카운트(명세 7, 7.1).** 잠금의 마지막 봉 이후에 진입한 현재 구간의 청산 거래만 센다(홀드아웃 거래는 세지 않는다). 필요 30건. B 지문이 바뀌면 새 구간에서 0부터 세고, 그 구간은 실거래 전환 판정 대상이 아니다(A와 같은 규칙).
15. **워크플로 휴면.** 명세 10절 순서상 `paper-b.yml`은 홀드아웃보다 먼저 `master`에 들어간다. 그동안 예비 `schedule`이 실패 알림을 보내지 않도록 작업은 저장소 변수 `PAPER_B_ENABLED`가 `true`일 때만 돈다(합격 후 사용자가 켠다. 건너뛴 작업은 실행 시간을 쓰지 않는다).
16. **1차 트리거.** 기존 Cloudflare Worker 코드는 크론과 관계없이 `paper.yml`만 부르므로 크론 1줄만 더하면 A가 두 번 돈다. 그래서 `docs/strategy-b.md`에 `paper-b.yml`을 부르는 두 번째 Worker 코드를 싣는다(사용자 작업, `ops/`는 바꾸지 않는다).
17. **git 동기화.** A의 `scripts/paper_sync.sh`는 `paper` 브랜치에 묶여 있고 수정할 수 없으므로 같은 규칙을 `paper-b`용 파이썬 모듈 `ewbot.paperb.sync`로 구현한다(`python -m ewbot.paperb.sync checkout | push …`).
18. **파일 배치.** 명세 3절의 `research.py`(워크포워드와 홀드아웃)는 책임별로 `research.py`(워크포워드)와 `holdout.py`(잠금·1회 실행·합격 기준)로 나누고, 신호 생성은 `signals.py`에 둔다(`rules.py`는 순수 함수만). 모두 `stratb/` 안이라 B 지문 대상이다.

## File Structure

| 파일 | 작업 | 책임 |
|---|---|---|
| `src/ewbot/stratb/__init__.py` | 1 | 패키지 표시 |
| `src/ewbot/stratb/config.py` | 1 | 그리드 32개, 고정값, 실행 설정(`BConfig`), 조합 정규화, 비용 배수 |
| `src/ewbot/stratb/fingerprint.py` | 1 | B 지문 |
| `src/ewbot/stratb/data15.py` | 2 | 15분봉 수집·캐시, 2024 가드 로더, 1시간봉 합성, 누락 보고 |
| `src/ewbot/stratb/rules.py` | 3 | 순수 함수: RSI, 피벗, 되돌림 구간, 확장 목표, 매물대 겹침, 다이버전스, 돌파 수준, 손절, 1차 목표, 걸러내기 |
| `src/ewbot/stratb/signals.py` | 4 | 15분 프레임과 1시간봉, 기준 구간(A의 ZigZag), 신호 생성 |
| `src/ewbot/stratb/engine.py` | 5 | 15분 백테스트 루프: 체결, 수량, 손실 한도, 비용, 판단 추적 |
| `src/ewbot/stratb/research.py` | 6 | 2024 가드, 단일 백테스트, 그리드, plateau, 구간, 워크포워드 |
| `src/ewbot/stratb/baselines.py` | 7 | 매수 후 보유, 무작위 진입 대조군 |
| `src/ewbot/stratb/holdout.py` | 8 | `holdout-b.lock`, 1회 홀드아웃, 합격 기준 |
| `src/ewbot/paperb/__init__.py` | 9 | 패키지 표시 |
| `src/ewbot/paperb/ledger.py` | 9 | A의 장부 스키마를 B 잠금에 묶기, 모의 카운트 n/30 |
| `src/ewbot/paperb/decisions.py` | 9 | B 실행 결과 → A의 판단 형식 |
| `src/ewbot/paperb/notify.py` | 9 | `[ewbot paper-B]` 메시지 |
| `src/ewbot/paperb/runner.py` | 10 | 모의매매 1회 실행: 사전 점검, 데이터, 구간, 재계산, 대조, 장부, 알림 |
| `src/ewbot/cli.py` (수정) | 11 | `ewbot stratb fetch | backtest | walkforward | holdout lock | holdout run | paper run | paper status` |
| `src/ewbot/paperb/sync.py` | 12 | `paper-b` 브랜치 git 동기화 |
| `.github/workflows/paper-b.yml` | 12 | 모의매매 워크플로 |
| `tests/paper/test_static.py` (수정) | 12 | 워크플로 목록 + B 보호(CI) |
| `docs/strategy-b.md`, `README.md` (수정) | 12 | 운영 문서 |
| `tests/stratb/*`, `tests/paperb/*` | 1–12 | 테스트와 합성 데이터 도우미 |

## 작업 개요

| 작업 | 구성 | 새 테스트 |
|---|---|---|
| 1 | B 그리드·실행 설정, B 지문, A 보호 검사 | 16 |
| 2 | 15분봉 수집·캐시·2024 가드 | 8 |
| 3 | 매매 규칙 순수 함수 | 8 |
| 4 | 15분 프레임, 기준 구간, 신호(+ 합성 데이터 도우미) | 11 |
| 5 | 15분 엔진 | 13 |
| 6 | 백테스트 가드, 그리드, 워크포워드 | 7 |
| 7 | 매수 후 보유, 무작위 대조군 | 5 |
| 8 | 잠금, 1회 홀드아웃, 합격 기준 | 9 |
| 9 | 모의매매 어댑터(장부, 판단, 알림) | 6 |
| 10 | 모의매매 실행기 | 8 |
| 11 | CLI `ewbot stratb …` | 11 |
| 12 | `paper-b` 동기화, 워크플로, CI 검사, 문서 | 12 |
| 13 | 최종 검증(전체 테스트, A 지문) | — |
| 14 | 수동 절차: 수집, 워크포워드, 잠금, 승인, 홀드아웃, 모의매매 시작(컨트롤러와 사용자만) | — |

Task 12 이후 전체: `1360 passed`(캐시가 없으면 `1359 passed, 1 skipped`) — 기준선보다 114개 많다. 각 작업의 Step 4는 그 작업의 테스트만 돌린다. 실행 시간: Task 1~12의 새 테스트는 합쳐서 약 25초, 전체는 약 2분 20초.

---

### Task 1: B 그리드와 실행 설정, B 지문, A 보호 검사

**Files:**
- Create: `src/ewbot/stratb/__init__.py`
- Create: `src/ewbot/stratb/config.py`
- Create: `src/ewbot/stratb/fingerprint.py`
- Test: `tests/stratb/__init__.py`
- Test: `tests/stratb/test_config.py`
- Test: `tests/stratb/test_fingerprint_b.py`
- Test: `tests/stratb/test_isolation.py`

**Interfaces:**
- Consumes: `ewbot.config`: `BreakerParams`, `CostParams`, `RiskProfile`(import만). `ewbot.paper.fingerprint`: `PROJECT_ROOT`, `fingerprint_of(files: Mapping[str, bytes]) -> str`, `current_fingerprint(repo_root=None) -> str`, `is_fingerprinted(rel: str) -> bool`, `tree_files(repo_root=None) -> dict[str, bytes]`.
- Produces: `ewbot.stratb.config`: `M15 = 900`; `GRID_B: dict[str, tuple]`(키 순서 `zz_k`, `zone`, `tp1_r`, `ext`, `remainder`); `REMAINDER_MODES = ("breakeven", "original")`; `Combo = tuple[Any, ...]` — `(zz_k: float, (z_lo, z_hi): tuple[float, float], tp1_r: float, ext: float, remainder: str)`; `@dataclass(frozen=True) StratBParams(zz_k=2.0, zone=(0.382, 0.618), tp1_r=1.0, ext=1.272, remainder="breakeven", rsi_len=14, pivot_w=3, confluence_atr=0.25, stop_buf_atr=0.2, max_stop_atr1h=2.5, confirm_hours=72, time_stop_hours=48, atr_len=14, confluence_lookback_hours=720)`(잘못된 `remainder`/`zone`은 `ValueError`); `RISK_B = RiskProfile("stratb", 0.005, 3.0)`; `@dataclass(frozen=True) BConfig(params=StratBParams(), risk=RISK_B, breakers=BreakerParams(), costs=CostParams(), initial_equity=10_000.0, tp1_frac=0.5)`; `grid_combos() -> list[Combo]`(32개); `normalize_combo(values: Sequence) -> Combo`(JSON 리스트도 받음, 그리드 밖이면 `ValueError`); `combo_to_json(combo) -> list`(`[zz_k, [z_lo, z_hi], tp1_r, ext, remainder]`); `params_for(combo) -> StratBParams`; `combo_of(p) -> Combo`; `config_for(combo, *, breakers_enabled=True) -> BConfig`; `scale_costs(c: CostParams, mult: float) -> CostParams`(수수료율·최소 수수료·슬리피지만 배수).
  `ewbot.stratb.fingerprint`: `STRATB_PREFIX = "src/ewbot/stratb/"`; `stratb_files(repo_root=None) -> dict[str, bytes]`; `stratb_fingerprint(repo_root=None) -> str`(파일이 없으면 `FileNotFoundError`).
  `tests/stratb/test_isolation.py`: 이후 모든 작업에서 A 보호를 지키는 검사(아래 설명).

B의 모든 매개변수를 A의 `config.py`와 별개인 `stratb/config.py`에 둔다. 그리드 조합은 튜플로 다루며, JSON에서 온 리스트는 `normalize_combo`가 정확한 그리드 튜플로 바꾸고 그리드 밖의 값은 거부한다. B 지문은 A와 같은 형식으로 `stratb/` 아래 파일만 해시한다. `tests/stratb/test_isolation.py`는 이 계획 전체의 안전장치다: A 지문 고정값, `stratb/`·`paperb/`가 A 지문 대상 밖인지, `master`와의 merge-base 이후 B의 경로를 바꾼 브랜치가 A의 경로(지문 대상, `backtest/`, `paper/`, `live/`, `holdout.lock`, `paper.yml`, `ci.yml`, `scripts/`, `pyproject.toml`, `uv.lock`)를 하나도 바꾸지 않았는지(추적되지 않은 새 파일 포함; B를 건드리지 않는 A 유지보수 브랜치는 해당 없음; `master`가 없는 얕은 클론에서는 건너뜀), 리터럴 `include_holdout=True`를 쓰는 모듈이 `stratb/holdout.py`·`paperb/runner.py`뿐인지, B 코드가 A의 실행 산출물 이름(`paper.yml`, `holdout.lock`, `refs/heads/paper`, `origin/paper`, `BTC-USD_1h.parquet`)을 문자열로 쓰지 않는지 확인한다. 집합 검사는 부분집합(`<=`)이라 아직 없는 모듈이 있어도 통과한다.

- [ ] **Step 1: 실패하는 테스트 작성**

테스트 패키지와 테스트 파일 4개를 만든다.

```bash
mkdir -p src/ewbot/stratb tests/stratb
```

**파일 `tests/stratb/__init__.py`** (새 파일, 전체 내용)

```python
"""Tests for ewbot.stratb (Plan 4)."""
```

**파일 `tests/stratb/test_config.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import pytest

from ewbot.config import CostParams
from ewbot.stratb.config import (
    GRID_B,
    M15,
    RISK_B,
    BConfig,
    StratBParams,
    combo_of,
    combo_to_json,
    config_for,
    grid_combos,
    normalize_combo,
    params_for,
    scale_costs,
)


def test_the_grid_has_32_combos_in_key_order():
    combos = grid_combos()
    assert list(GRID_B) == ["zz_k", "zone", "tp1_r", "ext", "remainder"]
    assert len(combos) == 32 and len(set(combos)) == 32
    assert combos[0] == (2.0, (0.382, 0.618), 1.0, 1.272, "breakeven")
    assert combos[-1] == (3.0, (0.5, 0.786), 1.5, 1.618, "original")


def test_defaults_are_the_first_combo_and_the_spec_fixed_values():
    p = StratBParams()
    assert combo_of(p) == grid_combos()[0]
    assert (p.rsi_len, p.pivot_w, p.confluence_atr, p.stop_buf_atr, p.max_stop_atr1h) == (14, 3, 0.25, 0.2, 2.5)
    assert (p.confirm_hours, p.time_stop_hours, p.atr_len, p.confluence_lookback_hours) == (72, 48, 14, 720)
    assert M15 == 900


def test_normalize_accepts_json_lists_and_refuses_off_grid_values():
    assert normalize_combo([3, [0.5, 0.786], 1.5, 1.618, "original"]) == (3.0, (0.5, 0.786), 1.5, 1.618, "original")
    assert combo_to_json(grid_combos()[5]) == [2.0, [0.382, 0.618], 1.5, 1.272, "original"]
    for bad in ([2.5, [0.382, 0.618], 1.0, 1.272, "breakeven"], [2.0, [0.382, 0.618], 1.0, 1.272, "trail"],
                [2.0, [0.382, 0.618], 1.0, 1.272], [2.0, 0.5, 1.0, 1.272, "breakeven"]):
        with pytest.raises(ValueError):
            normalize_combo(bad)


def test_params_round_trip_every_combo():
    for c in grid_combos():
        assert combo_of(params_for(c)) == c


def test_run_config_uses_the_spec_risk_and_strategy_a_breakers_costs_and_equity():
    cfg = config_for(grid_combos()[0])
    assert cfg.risk == RISK_B and (RISK_B.risk_pct, RISK_B.max_leverage) == (0.005, 3.0)
    assert (cfg.breakers.daily_loss, cfg.breakers.total_dd, cfg.breakers.enabled) == (0.03, 0.15, True)
    assert cfg.costs == CostParams() and cfg.initial_equity == 10_000.0 and cfg.tp1_frac == 0.5
    assert config_for(grid_combos()[0], breakers_enabled=False).breakers.enabled is False
    assert BConfig().params == StratBParams()


def test_scale_costs_scales_fees_and_slippage_only():
    c = scale_costs(CostParams(), 1.5)
    base = CostParams()
    assert c.fee_rate == pytest.approx(base.fee_rate * 1.5) and c.slippage == pytest.approx(base.slippage * 1.5)
    assert c.fee_min_per_contract == pytest.approx(base.fee_min_per_contract * 1.5)
    assert (c.funding_apr_long, c.contract_size, c.apply_min_fee) == (base.funding_apr_long, base.contract_size, base.apply_min_fee)


def test_invalid_params_are_refused():
    with pytest.raises(ValueError):
        StratBParams(remainder="trail")
    with pytest.raises(ValueError):
        StratBParams(zone=(0.7, 0.5))
```

**파일 `tests/stratb/test_fingerprint_b.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import shutil

import pytest

from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint
from ewbot.stratb.fingerprint import STRATB_PREFIX, stratb_files, stratb_fingerprint


def _copy(tmp_path):
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", tmp_path / "src" / "ewbot",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_the_file_set_is_every_python_file_below_stratb():
    files = stratb_files()
    pkg = PROJECT_ROOT / "src" / "ewbot" / "stratb"
    assert set(files) == {STRATB_PREFIX + f.relative_to(pkg).as_posix() for f in pkg.rglob("*.py")}
    assert "src/ewbot/stratb/config.py" in files and all(k.startswith(STRATB_PREFIX) for k in files)


def test_an_edit_an_addition_and_a_removal_change_it(tmp_path):
    root = _copy(tmp_path)
    base = stratb_fingerprint(root)
    assert base == stratb_fingerprint(PROJECT_ROOT)
    cfg = root / "src/ewbot/stratb/config.py"
    original = cfg.read_bytes()
    cfg.write_bytes(original + b"# comment\n")
    assert stratb_fingerprint(root) != base
    cfg.write_bytes(original)
    assert stratb_fingerprint(root) == base
    (root / "src/ewbot/stratb/extra.py").write_text("x = 1\n", encoding="utf-8")
    assert stratb_fingerprint(root) != base
    (root / "src/ewbot/stratb/extra.py").unlink()
    (root / "src/ewbot/stratb/fingerprint.py").unlink()
    assert stratb_fingerprint(root) != base


def test_strategy_a_and_b_fingerprints_are_independent(tmp_path):
    root = _copy(tmp_path)
    a0, b0 = current_fingerprint(root), stratb_fingerprint(root)
    (root / "src/ewbot/stratb/config.py").write_text("# changed\n", encoding="utf-8")
    b1 = stratb_fingerprint(root)
    assert current_fingerprint(root) == a0 and b1 != b0
    (root / "src/ewbot/paper/runner.py").write_text("# changed\n", encoding="utf-8")
    loop = root / "src/ewbot/engine/loop.py"
    loop.write_bytes(loop.read_bytes() + b"# one line\n")
    assert current_fingerprint(root) != a0 and stratb_fingerprint(root) == b1


def test_an_empty_tree_has_no_fingerprint(tmp_path):
    with pytest.raises(FileNotFoundError):
        stratb_fingerprint(tmp_path)
```

**파일 `tests/stratb/test_isolation.py`** (새 파일, 전체 내용)

```python
"""Strategy-A protection (Plan 4 spec 2, 9): B must not change anything A's paper, live or go-live
count depends on. These run in the full suite; tests/paper/test_static.py (run by ci.yml) carries the
CI part (Task 13)."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import ewbot
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, is_fingerprinted, tree_files

SRC = Path(ewbot.__file__).resolve().parent
FROZEN_A = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
B_FOLDERS = ("stratb", "paperb")
# Strategy A's files: fingerprinted code, research code, runtimes, lock, workflows, sync script, lockfiles.
PROTECTED = (
    "src/ewbot/types.py", "src/ewbot/config.py", "src/ewbot/indicators.py", "src/ewbot/data/",
    "src/ewbot/waves/", "src/ewbot/strategy/", "src/ewbot/engine/", "src/ewbot/execution/", "src/ewbot/risk/",
    "src/ewbot/backtest/", "src/ewbot/paper/", "src/ewbot/live/", "holdout.lock",
    ".github/workflows/paper.yml", ".github/workflows/ci.yml", "scripts/", "uv.lock", "pyproject.toml",
)
B_PATHS = (
    "src/ewbot/stratb/", "src/ewbot/paperb/", "tests/stratb/", "tests/paperb/", ".github/workflows/paper-b.yml",
    "docs/strategy-b.md", "holdout-b.lock",
)
INCLUDE_TRUE = {"stratb/holdout.py", "paperb/runner.py"}  # the only callers that may read 2024+ candles
INCLUDE_PASS_THROUGH = {"stratb/research.py"}  # backtest_b forwards its own include_holdout argument
A_ARTIFACTS = {"paper.yml", "holdout.lock", "refs/heads/paper", "origin/paper", "BTC-USD_1h.parquet"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _modules(folders: tuple[str, ...] | None = None):
    roots = [SRC] if folders is None else [SRC / f for f in folders if (SRC / f).is_dir()]
    for root in roots:
        for f in sorted(root.rglob("*.py")):
            yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def test_strategy_a_fingerprint_is_unchanged():
    assert current_fingerprint() == FROZEN_A


def test_strategy_b_code_is_outside_strategy_a_fingerprinted_folders():
    for folder in B_FOLDERS:
        assert not is_fingerprinted(f"{folder}/x.py") and not is_fingerprinted(f"{folder}/sub/y.py")
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratb/", "src/ewbot/paperb/"))]


def test_strategy_b_work_changes_no_strategy_a_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches
    strategy B touches no strategy-A path. A branch of strategy-A maintenance alone is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(B_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


def test_only_the_b_holdout_and_the_paper_b_runner_unlock_2024_candles():
    literal_true, other = set(), set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "include_holdout":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            literal_true.add(rel)
                        elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                            other.add(rel)
    assert literal_true <= INCLUDE_TRUE
    assert other <= INCLUDE_PASS_THROUGH


def test_strategy_b_code_never_names_strategy_a_runtime_artifacts():
    for rel, tree in _modules(B_FOLDERS):
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not names & A_ARTIFACTS, rel
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb -q
```

예상: `No module named 'ewbot.stratb`
예상: `2 errors`

`ewbot.stratb`가 아직 없어 두 파일이 수집 단계에서 실패한다(`mkdir`로 만든 빈 폴더는 네임스페이스 패키지로 읽혀 메시지가 `No module named 'ewbot.stratb.config'`처럼 나온다). `test_isolation.py`는 수집이 중단되어 실행되지 않는다.

- [ ] **Step 3: 구현**

패키지, 설정, 지문 모듈을 만든다.

**파일 `src/ewbot/stratb/__init__.py`** (새 파일, 전체 내용)

```python
"""Strategy B (Plan 4): 15-minute confirmation entries on 1h swing structure.

Every *.py file below this folder is hashed by ewbot.stratb.fingerprint (the B fingerprint)."""
```

**파일 `src/ewbot/stratb/config.py`** (새 파일, 전체 내용)

```python
"""Strategy-B parameters (Plan 4 spec 4-5): the 32-combo grid, the fixed values and the run config.

Separate from ewbot.config (strategy A): A's RiskProfile / BreakerParams / CostParams types are only
imported, never changed. Combos are tuples in GRID_B key order: (zz_k, (z_lo, z_hi), tp1_r, ext, remainder)."""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass
from typing import Any, Sequence

from ewbot.config import BreakerParams, CostParams, RiskProfile

M15 = 900  # one 15-minute bar, seconds

GRID_B: dict[str, tuple[Any, ...]] = {
    "zz_k": (2.0, 3.0),
    "zone": ((0.382, 0.618), (0.5, 0.786)),
    "tp1_r": (1.0, 1.5),
    "ext": (1.272, 1.618),
    "remainder": ("breakeven", "original"),
}
REMAINDER_MODES = ("breakeven", "original")

Combo = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class StratBParams:
    """Grid values (defaults = the first grid combo) and the fixed values of spec 5, which are not
    in the grid. confluence_lookback_hours is the plan's resolution of spec 4.2 ("swing highs or lows
    before L"): only the 1h pivots of the 30 days before L count."""

    zz_k: float = 2.0
    zone: tuple[float, float] = (0.382, 0.618)
    tp1_r: float = 1.0
    ext: float = 1.272
    remainder: str = "breakeven"
    rsi_len: int = 14
    pivot_w: int = 3
    confluence_atr: float = 0.25
    stop_buf_atr: float = 0.2
    max_stop_atr1h: float = 2.5
    confirm_hours: int = 72
    time_stop_hours: int = 48
    atr_len: int = 14
    confluence_lookback_hours: int = 720

    def __post_init__(self) -> None:
        if self.remainder not in REMAINDER_MODES:
            raise ValueError(f"remainder must be one of {REMAINDER_MODES}, got {self.remainder!r}")
        if not 0.0 < self.zone[0] < self.zone[1] < 1.0:
            raise ValueError(f"zone must satisfy 0 < z_lo < z_hi < 1, got {self.zone}")


RISK_B = RiskProfile("stratb", 0.005, 3.0)  # spec 4.4: 0.5% of equity per trade, leverage cap 3x


@dataclass(frozen=True, slots=True)
class BConfig:
    """Everything a strategy-B run depends on. Breakers, costs and the initial equity are A's values
    (spec 4.4, 6.2): daily -3%, total -15%, 5 losses -> 24h pause; fee 0.05%, slippage 0.05%, funding
    10% APR; 10,000 USD; contracts of 0.01 BTC."""

    params: StratBParams = StratBParams()
    risk: RiskProfile = RISK_B
    breakers: BreakerParams = BreakerParams()
    costs: CostParams = CostParams()
    initial_equity: float = 10_000.0
    tp1_frac: float = 0.5


def grid_combos() -> list[Combo]:
    """The 2 x 2 x 2 x 2 x 2 = 32 combos in GRID_B key order."""
    return list(itertools.product(*GRID_B.values()))


def normalize_combo(values: Sequence[Any]) -> Combo:
    """A combo from JSON or the CLI (the zone may be a list) -> the exact grid tuple. ValueError when the
    values are not one of the 32 combos."""
    if len(values) != len(GRID_B):
        raise ValueError(f"a combo has {len(GRID_B)} values {list(GRID_B)}, got {list(values)}")
    zz, zone, tp1, ext, rem = values
    try:
        combo = (float(zz), (float(zone[0]), float(zone[1])), float(tp1), float(ext), str(rem))
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(f"malformed combo {list(values)}: {exc}") from exc
    if combo not in grid_combos():
        raise ValueError(f"{list(values)} is not a combo of the strategy-B grid {GRID_B}")
    return combo


def combo_to_json(combo: Sequence[Any]) -> list[Any]:
    c = normalize_combo(combo)
    return [c[0], [c[1][0], c[1][1]], c[2], c[3], c[4]]


def params_for(combo: Sequence[Any]) -> StratBParams:
    return StratBParams(**dict(zip(GRID_B, normalize_combo(combo))))


def combo_of(p: StratBParams) -> Combo:
    return tuple(getattr(p, k) for k in GRID_B)


def config_for(combo: Sequence[Any], *, breakers_enabled: bool = True) -> BConfig:
    """The run config of a grid combo. Walk-forward grid runs pass breakers_enabled=False (as A's)."""
    return BConfig(params=params_for(combo), breakers=dataclasses.replace(BreakerParams(), enabled=breakers_enabled))


def scale_costs(c: CostParams, mult: float) -> CostParams:
    """Fees (rate and per-contract minimum) and slippage times `mult`; funding unchanged (spec 6.5 item 4)."""
    return dataclasses.replace(
        c, fee_rate=c.fee_rate * mult, fee_min_per_contract=c.fee_min_per_contract * mult, slippage=c.slippage * mult
    )
```

**파일 `src/ewbot/stratb/fingerprint.py`** (새 파일, 전체 내용)

```python
"""Strategy-B fingerprint (Plan 4 spec 3, 7): sha256 over every *.py file below src/ewbot/stratb.

Same format as strategy A's (ewbot.paper.fingerprint.fingerprint_of: sorted repo-relative paths, each
path NUL bytes NUL), over a disjoint file set: stratb/ is outside A's fingerprinted folders and no A file
is in this one, so either fingerprint can change without the other. Any byte change, addition, removal or
rename below stratb/ changes it; paperb/ and cli.py are not part of it."""

from __future__ import annotations

from pathlib import Path

from ewbot.paper.fingerprint import PROJECT_ROOT, fingerprint_of

STRATB_PREFIX = "src/ewbot/stratb/"


def stratb_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/stratb/<rel>": bytes} for every *.py file below src/ewbot/stratb."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot" / "stratb"
    return {STRATB_PREFIX + f.relative_to(pkg).as_posix(): f.read_bytes() for f in pkg.rglob("*.py") if f.is_file()}


def stratb_fingerprint(repo_root: Path | None = None) -> str:
    files = stratb_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no strategy-B source files below {repo_root or PROJECT_ROOT}/src/ewbot/stratb")
    return fingerprint_of(files)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb -q
```

예상: `16 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/__init__.py src/ewbot/stratb/config.py src/ewbot/stratb/fingerprint.py tests/stratb/__init__.py tests/stratb/test_config.py tests/stratb/test_fingerprint_b.py tests/stratb/test_isolation.py
git commit -m "feat(stratb): strategy-B grid, run config, B fingerprint and the strategy-A protection guard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 15분봉 수집·캐시와 2024 가드

**Files:**
- Create: `src/ewbot/stratb/data15.py`
- Test: `tests/stratb/test_data15.py`

**Interfaces:**
- Consumes: `ewbot.data.coinbase_history`: `API`, `COLUMNS`, `MAX_PER_REQUEST`, `_get_json(session, url, params) -> list`(429·5xx 재시도; 파일은 수정하지 않고 import만). `ewbot.data.candles`: `IntegrityReport`, `check_integrity(df, tf)`. `ewbot.backtest.walkforward`: `HOLDOUT_START = 1704067200`, `WF_START = "2017-01-01"`. `ewbot.backtest.runner.utc_ts(date) -> int`. `ewbot.paper.fingerprint.PROJECT_ROOT`. Task 1: `M15`.
- Produces: `ewbot.stratb.data15`: `PRODUCT = "BTC-USD"`; `START_TS = 1483228800`(2017-01-01); `REPLAY_ANCHOR = 1696118400`(2023-10-01); `DEFAULT_LOCK_PATH = <저장소 루트>/holdout-b.lock`; `LOCK_KIND = "ewbot-stratb-holdout"`; `class HoldoutGuardError(RuntimeError)`; `cache_path(cache_dir, product="BTC-USD") -> Path`(`<cache_dir>/BTC-USD_15m.parquet`); `lock_exists(lock_path) -> bool`(`kind == LOCK_KIND`이고 `sha256`이 문자열인 JSON 객체일 때만 참); `fetch_15m(product, start_ts, end_ts, session=None, sleep_s=0.15) -> DataFrame`(`[start_ts, end_ts)`의 마감된 봉, 오름차순, 표준 스키마 `ts:int64, open, high, low, close, volume:float64`); `update_cache_15m(cache_dir, *, product="BTC-USD", lock_path=DEFAULT_LOCK_PATH, start_ts=START_TS, now_ts=None, session=None, sleep_s=0.15) -> Path`; `load_15m(path, *, include_holdout=False, lock_path=DEFAULT_LOCK_PATH) -> DataFrame`; `gap_report(df15) -> IntegrityReport`; `resample_1h(df15) -> DataFrame`(완성된 UTC 시간만, `synthetic` 열 포함).

A의 `coinbase_history.py`와 같은 공개 엔드포인트와 같은 요청 방식(최신순 최대 300행, start·end 포함)을 granularity 900으로 쓴다. 캐시는 A의 1시간봉 파일과 다른 `BTC-USD_15m.parquet`이다. 진행 중인 봉은 저장하지 않고, 누락 봉은 채우지 않는다. **가드:** 잠금 파일이 없으면 `update_cache_15m`은 2024-01-01 이후를 요청조차 하지 않고, `load_15m`은 pyarrow 필터로 읽는 순간 걸러내며, `include_holdout=True`는 `HoldoutGuardError`를 낸다. 네트워크는 `update_cache_15m`(→ `fetch_15m`)에서만 쓰고, 그것을 부르는 곳은 CLI의 `stratb fetch`·`stratb holdout run`과 모의매매 실행기뿐이다(모두 테스트에서는 가짜로 바꾼다). 모의매매는 빈 캐시를 `REPLAY_ANCHOR`부터 받는다(`start_ts`).

- [ ] **Step 1: 실패하는 테스트 작성**

가짜 세션(요청한 구간만 최신순으로 돌려주고 요청을 기록)으로 페이지 나누기, 2024 차단, 잠금 후 확장, 로더 가드, 1시간봉 합성, 누락 보고를 확인한다.

**파일 `tests/stratb/test_data15.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from ewbot.data import coinbase_history as ch
from ewbot.stratb import data15 as d15
from ewbot.stratb.data15 import (
    LOCK_KIND,
    REPLAY_ANCHOR,
    START_TS,
    HoldoutGuardError,
    cache_path,
    fetch_15m,
    gap_report,
    load_15m,
    lock_exists,
    resample_1h,
    update_cache_15m,
)

HS = 1_704_067_200  # 2024-01-01 00:00 UTC
M15 = 900


def _row(ts: int) -> list[float]:
    base = 40_000.0 + (ts - HS) / M15
    return [ts, base - 5.0, base + 10.0, base, base + 1.0, 1.5]  # [time, low, high, open, close, volume]


class FakeSession:
    """Honours start/end (inclusive), at most 300 rows, newest first; records every request."""

    def __init__(self, first_ts: int, last_ts: int):
        self.first, self.last = first_ts, last_ts
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, **params})
        assert params["granularity"] == 900
        s = int(datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        e = int(datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
        tss = [t for t in range(s, e + 1, M15) if self.first <= t <= self.last]
        assert len(tss) <= 300

        class R:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return [_row(t) for t in reversed(tss)]

        return R()


def _lock(path, kind=LOCK_KIND):
    path.write_text(json.dumps({"kind": kind, "sha256": "ab" * 32}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(ch.time, "sleep", lambda s: None)


def test_constants():
    assert (START_TS, REPLAY_ANCHOR) == (1_483_228_800, 1_696_118_400)
    assert cache_path("data/cache").name == "BTC-USD_15m.parquet"
    assert d15.DEFAULT_LOCK_PATH.name == "holdout-b.lock"


def test_fetch_pages_300_bars_at_granularity_900():
    t0 = HS - 1000 * M15
    sess = FakeSession(t0, HS)
    df = fetch_15m("BTC-USD", t0, t0 + 700 * M15, session=sess, sleep_s=0)
    assert len(sess.calls) == 3 and sess.calls[0]["url"].endswith("/products/BTC-USD/candles")
    assert df["ts"].tolist() == [t0 + k * M15 for k in range(700)]
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"] and str(df["ts"].dtype) == "int64"
    assert df.loc[0, "open"] == _row(t0)[3] and df.loc[0, "low"] == _row(t0)[1]


def test_without_a_lock_the_cache_stops_before_2024(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    path = update_cache_15m(tmp_path, lock_path=tmp_path / "none.lock", start_ts=HS - 50 * M15,
                            now_ts=HS + 400 * M15, session=sess, sleep_s=0)
    df = pd.read_parquet(path)
    assert int(df["ts"].max()) == HS - M15 and len(df) == 50 and len(sess.calls) == 1
    assert all(c["end"] < "2024-01-01" for c in sess.calls)


def test_with_a_lock_the_cache_extends_to_the_last_closed_bar(tmp_path):
    sess = FakeSession(HS - 50 * M15, HS + 500 * M15)
    lock = _lock(tmp_path / "holdout-b.lock")
    update_cache_15m(tmp_path, lock_path=tmp_path / "none.lock", start_ts=HS - 50 * M15, now_ts=HS + 400 * M15,
                     session=sess, sleep_s=0)
    path = update_cache_15m(tmp_path, lock_path=lock, now_ts=HS + 10 * M15 + 899, session=sess, sleep_s=0)
    df = pd.read_parquet(path)
    assert int(df["ts"].max()) == HS + 9 * M15 and df["ts"].is_unique and len(df) == 60
    assert sess.calls[-1]["start"] == "2024-01-01T00:00:00Z"


def test_load_never_returns_2024_bars_without_include_holdout(tmp_path):
    p = tmp_path / "c.parquet"
    pd.DataFrame([_row(t) for t in range(HS - 4 * M15, HS + 4 * M15, M15)],
                 columns=["ts", "low", "high", "open", "close", "volume"]).to_parquet(p, index=False)
    df = load_15m(p, lock_path=_lock(tmp_path / "l.lock"))
    assert int(df["ts"].max()) == HS - M15 and len(df) == 4
    with pytest.raises(HoldoutGuardError):
        load_15m(p, include_holdout=True, lock_path=tmp_path / "missing.lock")
    (tmp_path / "bogus.lock").write_text("{}", encoding="utf-8")
    with pytest.raises(HoldoutGuardError):
        load_15m(p, include_holdout=True, lock_path=tmp_path / "bogus.lock")
    assert len(load_15m(p, include_holdout=True, lock_path=tmp_path / "l.lock")) == 8


def test_lock_exists_needs_a_strategy_b_lock(tmp_path):
    assert not lock_exists(tmp_path / "x")
    (tmp_path / "x").write_text("not json", encoding="utf-8")
    assert not lock_exists(tmp_path / "x")
    assert not lock_exists(_lock(tmp_path / "a", kind="other"))
    assert lock_exists(_lock(tmp_path / "b"))


def test_resample_keeps_complete_hours_only_and_flags_synthetic():
    rows = [[HS + k * M15, 10.0 + k, 20.0 + k, 5.0 + k, 11.0 + k, 1.0] for k in range(10)]
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["synthetic"] = [False] * 5 + [True] + [False] * 4
    h = resample_1h(df)
    assert h["ts"].tolist() == [HS, HS + 3600]
    assert h.loc[0, ["open", "high", "low", "close", "volume"]].tolist() == [10.0, 23.0, 5.0, 14.0, 4.0]
    assert h["synthetic"].tolist() == [False, True]


def test_gap_report_lists_missing_15m_bars():
    df = pd.DataFrame({"ts": [HS, HS + M15, HS + 3 * M15], "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                       "volume": 1.0})
    assert gap_report(df).missing_ts == (HS + 2 * M15,)
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_data15.py -q
```

예상: `cannot import name 'data15' from 'ewbot.stratb'`
예상: `1 error`

`ewbot.stratb.data15`가 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

15분봉 모듈을 만든다.

**파일 `src/ewbot/stratb/data15.py`** (새 파일, 전체 내용)

```python
"""15-minute BTC-USD candles for strategy B (Plan 4 spec 6.1): fetch, cache, guarded load, 1h bars.

Same public endpoint and request pattern as ewbot.data.coinbase_history (Coinbase Exchange, no auth;
GET .../candles returns at most 300 rows [time, low, high, open, close, volume], newest first, start and
end inclusive bar-open times), with granularity 900. The cache is <cache_dir>/BTC-USD_15m.parquet; the
bar in progress is never stored and missing bars are not filled in it (gap_report lists them).

Holdout guard (spec 6.3): until holdout-b.lock exists, update_cache_15m never downloads a bar at or after
HOLDOUT_START (2024-01-01) and load_15m never returns one - the parquet read itself filters them out.
load_15m(include_holdout=True) raises HoldoutGuardError until the lock exists."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import HOLDOUT_START, WF_START
from ewbot.data.candles import IntegrityReport, check_integrity
from ewbot.data.coinbase_history import API, COLUMNS, MAX_PER_REQUEST, _get_json
from ewbot.paper.fingerprint import PROJECT_ROOT
from ewbot.stratb.config import M15
from ewbot.types import H1

PRODUCT = "BTC-USD"
START_TS = utc_ts(WF_START)  # 2017-01-01: the first bar fetched
REPLAY_ANCHOR = utc_ts("2023-10-01")  # spec 6.4: holdout and paper runs both recompute from here
DEFAULT_LOCK_PATH = PROJECT_ROOT / "holdout-b.lock"
LOCK_KIND = "ewbot-stratb-holdout"


class HoldoutGuardError(RuntimeError):
    """Candles at/after 2024-01-01 were requested before holdout-b.lock exists."""


def cache_path(cache_dir: Path, product: str = PRODUCT) -> Path:
    return Path(cache_dir) / f"{product}_15m.parquet"


def lock_exists(lock_path: Path) -> bool:
    """True only for a strategy-B holdout lock (a JSON object of kind LOCK_KIND with a sha256), not for
    any file that happens to have the name."""
    p = Path(lock_path)
    if not p.is_file():
        return False
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(lock, dict) and lock.get("kind") == LOCK_KIND and isinstance(lock.get("sha256"), str)


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]}})


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype({"ts": "int64", **{c: "float64" for c in COLUMNS[1:]}})
    return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_15m(
    product: str, start_ts: int, end_ts: int, session: requests.Session | None = None, sleep_s: float = 0.15
) -> pd.DataFrame:
    """Closed 15m bars with open time in [start_ts, end_ts), ascending, standard schema (maybe empty).
    Walks windows of 300 bars; retries 429 / 5xx like coinbase_history (its _get_json)."""
    first = -(-start_ts // M15) * M15
    last_open = (end_ts - 1) // M15 * M15
    if first > last_open:
        return _empty()
    sess = session if session is not None else requests.Session()
    url = API.format(product=product)
    rows: list[list[float]] = []
    w_start = first
    while w_start <= last_open:
        w_end = min(w_start + (MAX_PER_REQUEST - 1) * M15, last_open)
        rows.extend(_get_json(sess, url, {"granularity": M15, "start": _iso(w_start), "end": _iso(w_end)}))
        w_start = w_end + M15
        if w_start <= last_open and sleep_s > 0:
            time.sleep(sleep_s)
    if not rows:
        return _empty()
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = df["ts"].astype("int64")
    return _standard(df[(df["ts"] >= first) & (df["ts"] <= last_open)])


def update_cache_15m(
    cache_dir: Path,
    *,
    product: str = PRODUCT,
    lock_path: Path = DEFAULT_LOCK_PATH,
    start_ts: int = START_TS,
    now_ts: int | None = None,
    session: requests.Session | None = None,
    sleep_s: float = 0.15,
) -> Path:
    """Create (from start_ts) or extend the 15m cache up to the last CLOSED bar - and, while holdout-b.lock
    does not exist, never past HOLDOUT_START. Network: only here (`stratb fetch`, `stratb holdout run`,
    the paper-B runner, which starts an empty cache at REPLAY_ANCHOR)."""
    path = cache_path(cache_dir, product)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = _standard(pd.read_parquet(path)) if path.exists() else _empty()
    now = int(time.time()) if now_ts is None else int(now_ts)
    end = now // M15 * M15  # exclusive: the bar in progress is never stored
    if not lock_exists(lock_path):
        end = min(end, HOLDOUT_START)
    begin = int(old["ts"].max()) + M15 if len(old) else int(start_ts)
    if begin >= end:
        if not path.exists():
            old.to_parquet(path, index=False)
        return path
    new = fetch_15m(product, begin, end, session=session, sleep_s=sleep_s)
    _standard(pd.concat([old, new], ignore_index=True)).to_parquet(path, index=False)
    return path


def load_15m(path: Path, *, include_holdout: bool = False, lock_path: Path = DEFAULT_LOCK_PATH) -> pd.DataFrame:
    """The cached 15m bars (standard schema, sorted, unique ts). Without include_holdout the parquet read
    filters out every bar at/after HOLDOUT_START; include_holdout=True needs holdout-b.lock."""
    if include_holdout and not lock_exists(lock_path):
        raise HoldoutGuardError(
            f"{lock_path} does not exist: candles from 2024-01-01 on cannot be read before the strategy-B lock"
        )
    filters = None if include_holdout else [("ts", "<", HOLDOUT_START)]
    df = pd.read_parquet(Path(path), filters=filters)
    return _standard(df)


def gap_report(df15: pd.DataFrame) -> IntegrityReport:
    """Missing / duplicate / non-positive 15m bars (A's check_integrity on the 15m grid)."""
    return check_integrity(df15, tf=M15)


def resample_1h(df15: pd.DataFrame) -> pd.DataFrame:
    """1h bars from 15m bars: COMPLETE UTC hours only (exactly 4 distinct bars), synthetic when any
    constituent is (the column is False when the input has none)."""
    d = df15.drop_duplicates("ts").sort_values("ts")
    syn = d["synthetic"].astype(bool) if "synthetic" in d.columns else False
    d = d.assign(bucket=(d["ts"] // H1) * H1, synthetic=syn)
    agg = d.groupby("bucket", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), synthetic=("synthetic", "any"), n=("ts", "size"),
    )
    agg = agg[agg["n"] == H1 // M15].drop(columns="n")
    return agg.rename_axis("ts").reset_index().astype({"ts": "int64", "synthetic": "bool"})
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_data15.py -q
```

예상: `8 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/data15.py tests/stratb/test_data15.py
git commit -m "feat(stratb): 15m Coinbase candles with a cache that cannot reach 2024 before holdout-b.lock" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 매매 규칙 순수 함수

**Files:**
- Create: `src/ewbot/stratb/rules.py`
- Test: `tests/stratb/test_rules.py`

**Interfaces:**
- Consumes: numpy만.
- Produces: `ewbot.stratb.rules`(방향 `+1` = 상승 구간의 롱, `-1` = 하락 구간의 숏; 같은 식이 양쪽에 쓰인다): `rsi_series(close, n=14) -> np.ndarray`(Wilder, 인덱스 n 전은 NaN, 손실만 0이면 100, 변화가 없으면 50); `pivot_lows(low, w=3) -> np.ndarray[bool]`(왼쪽 w봉보다 엄격히 낮고 오른쪽 w봉보다 같거나 낮음); `pivot_highs(high, w=3)`(대칭); `retracement_zone(start, end, z_lo, z_hi) -> tuple[float, float]`; `extension_target(start, end, e) -> float`(`start + e (end − start)`); `has_confluence(zone, levels, tol) -> bool`; `is_divergence(direction, price1, price2, rsi1, rsi2) -> bool`; `breakout_level(direction, high, low, p1, p2) -> float`(봉 p1+1..p2−1의 최고가/최저가, 사이 봉이 없으면 `ValueError`); `stop_level(direction, pivot_price, atr15, buf_atr) -> float`; `first_target(entry_ref, stop, tp1_r) -> float`; `skip_reason(direction, entry_ref, stop, tp1, tp2, atr1h, max_stop_atr1h) -> str | None`(`"stop_wrong_side"`, `"stop_too_far"`, `"tp2_not_beyond_tp1"` 또는 `None`).

명세 4.2~4.4의 판단 하나하나를 상태 없는 함수로 만든다. 다음 작업의 신호 생성기는 이 함수들만 조합한다. `start`/`end`는 기준 구간의 시작과 끝(상승 구간 L→H, 하락 구간 H→L)이므로 되돌림 구간·확장 목표·1차 목표가 방향과 관계없이 한 식으로 쓰인다.

- [ ] **Step 1: 실패하는 테스트 작성**

손으로 계산한 Wilder RSI 값, 인과성(앞부분만 넣어도 같은 값), 피벗의 엄격/비엄격 경계, 두 방향의 구간·확장, 허용 폭, 다이버전스, 돌파 수준, 걸러내기 사유를 확인한다.

**파일 `tests/stratb/test_rules.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import math

import numpy as np
import pytest

from ewbot.stratb.rules import (
    breakout_level,
    extension_target,
    first_target,
    has_confluence,
    is_divergence,
    pivot_highs,
    pivot_lows,
    retracement_zone,
    rsi_series,
    skip_reason,
    stop_level,
)


def test_rsi_matches_a_hand_computed_wilder_value_and_is_causal():
    close = np.array([10, 11, 10, 12, 11, 13], dtype=float)
    r = rsi_series(close, n=3)
    assert np.isnan(r[:3]).all()
    ag, al = 3 / 3, 1 / 3  # first 3 changes: +1 -1 +2
    assert r[3] == pytest.approx(100 - 100 / (1 + ag / al))
    ag, al = (ag * 2 + 0) / 3, (al * 2 + 1) / 3  # change -1
    assert r[4] == pytest.approx(100 - 100 / (1 + ag / al))
    rng = np.random.default_rng(1)
    long = 100 + rng.normal(0, 1, 300).cumsum()
    full = rsi_series(long)
    for t in (20, 99, 250):
        assert np.array_equal(rsi_series(long[: t + 1]), full[: t + 1], equal_nan=True)


def test_rsi_extremes():
    assert rsi_series(np.arange(20.0), 14)[-1] == 100.0
    assert rsi_series(np.full(20, 5.0), 14)[-1] == 50.0
    assert rsi_series(np.arange(20.0)[::-1], 14)[-1] == 0.0


def test_pivots_are_strict_on_the_left_and_inclusive_on_the_right():
    low = np.array([5, 4, 3, 1, 2, 1, 3, 4, 5, 6], dtype=float)
    assert np.flatnonzero(pivot_lows(low, 3)).tolist() == [3]  # bar 5 ties bar 3 on its left: not a pivot
    assert pivot_lows(low[:6], 3).sum() == 0  # bar 3 needs 3 bars after it
    high = -low
    assert np.flatnonzero(pivot_highs(high, 3)).tolist() == [3]
    assert pivot_lows(np.ones(5), 3).sum() == 0


def test_zone_extension_and_confluence_for_both_legs():
    assert retracement_zone(1000.0, 1200.0, 0.382, 0.618) == pytest.approx((1076.4, 1123.6))
    assert retracement_zone(1200.0, 1000.0, 0.382, 0.618) == pytest.approx((1076.4, 1123.6))
    assert retracement_zone(1000.0, 1200.0, 0.5, 0.786) == pytest.approx((1042.8, 1100.0))
    assert extension_target(1000.0, 1200.0, 1.272) == pytest.approx(1254.4)
    assert extension_target(1200.0, 1000.0, 1.618) == pytest.approx(876.4)
    zone = (1076.4, 1123.6)
    assert has_confluence(zone, [900.0, 1100.0], 0.0)
    assert not has_confluence(zone, [1070.0, 1130.0], 5.0)
    assert has_confluence(zone, [1070.0], 6.5) and has_confluence(zone, [1130.0], 6.5)


def test_divergence_both_sides_and_nan():
    assert is_divergence(1, 100.0, 99.0, 20.0, 25.0)
    assert not is_divergence(1, 100.0, 99.0, 25.0, 20.0)
    assert not is_divergence(1, 99.0, 100.0, 20.0, 25.0)
    assert is_divergence(-1, 100.0, 101.0, 80.0, 75.0)
    assert not is_divergence(-1, 100.0, 101.0, 75.0, 80.0)
    assert not is_divergence(1, 100.0, 99.0, math.nan, 25.0)


def test_breakout_level_is_the_swing_between_the_pivots():
    high = np.array([5, 9, 7, 8, 4], dtype=float)
    low = np.array([1, 3, 2, 4, 0], dtype=float)
    assert breakout_level(1, high, low, 0, 4) == 9.0
    assert breakout_level(-1, high, low, 0, 4) == 2.0
    with pytest.raises(ValueError):
        breakout_level(1, high, low, 2, 3)


def test_stop_and_first_target():
    assert stop_level(1, 100.0, 10.0, 0.2) == 98.0 and stop_level(-1, 100.0, 10.0, 0.2) == 102.0
    assert first_target(110.0, 100.0, 1.5) == 125.0 and first_target(90.0, 100.0, 1.0) == 80.0


def test_skip_reasons():
    assert skip_reason(1, 110.0, 100.0, 120.0, 130.0, atr1h=5.0, max_stop_atr1h=2.5) is None
    assert skip_reason(1, 110.0, 100.0, 120.0, 130.0, atr1h=3.9, max_stop_atr1h=2.5) == "stop_too_far"
    assert skip_reason(1, 110.0, 100.0, 120.0, 120.0, atr1h=5.0, max_stop_atr1h=2.5) == "tp2_not_beyond_tp1"
    assert skip_reason(1, 100.0, 100.0, 100.0, 130.0, atr1h=5.0, max_stop_atr1h=2.5) == "stop_wrong_side"
    assert skip_reason(-1, 90.0, 100.0, 80.0, 70.0, atr1h=5.0, max_stop_atr1h=2.5) is None
    assert skip_reason(-1, 90.0, 100.0, 80.0, 85.0, atr1h=5.0, max_stop_atr1h=2.5) == "tp2_not_beyond_tp1"
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_rules.py -q
```

예상: `No module named 'ewbot.stratb.rules'`
예상: `1 error`

`ewbot.stratb.rules`가 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

규칙 모듈을 만든다.

**파일 `src/ewbot/stratb/rules.py`** (새 파일, 전체 내용)

```python
"""Strategy-B trading rules as pure functions (Plan 4 spec 4.2-4.4). Direction +1 = long setup on an
up leg (start L, end H), -1 = short setup on a down leg (start H, end L); every formula below is written
once for both. signals.py applies them bar by bar."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def _rsi(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder RSI(n): NaN until n price changes exist (index n), then RMA of gains and losses seeded with
    their simple mean. 100 when there are only gains, 50 when nothing moved. out[i] uses close[:i+1] only."""
    c = np.asarray(close, dtype=np.float64)
    out = np.full(c.size, np.nan)
    if c.size <= n:
        return out
    diff = np.diff(c)
    gains = np.maximum(diff, 0.0).tolist()
    losses = np.maximum(-diff, 0.0).tolist()
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    out[n] = _rsi(ag, al)
    for i in range(n + 1, c.size):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi(ag, al)
    return out


def pivot_lows(low: np.ndarray, w: int = 3) -> np.ndarray:
    """Bar j is a pivot low when low[j] is strictly below each of the w lows before it and at or below
    each of the w lows after it (spec 4.3: left/right 3 bars). It becomes known at the CLOSE of bar j + w;
    the first and last w bars are never pivots."""
    x = np.asarray(low, dtype=np.float64)
    n = x.size
    out = np.zeros(n, dtype=bool)
    if n < 2 * w + 1:
        return out
    core = x[w : n - w]
    ok = np.ones(core.size, dtype=bool)
    for k in range(1, w + 1):
        ok &= core < x[w - k : n - w - k]
        ok &= core <= x[w + k : n - w + k]
    out[w : n - w] = ok
    return out


def pivot_highs(high: np.ndarray, w: int = 3) -> np.ndarray:
    """Mirror of pivot_lows: strictly above the w highs before, at or above the w highs after."""
    return pivot_lows(-np.asarray(high, dtype=np.float64), w)


def retracement_zone(start: float, end: float, z_lo: float, z_hi: float) -> tuple[float, float]:
    """(lo, hi) of the retracement of the leg start -> end between z_lo and z_hi of its length (spec 4.2).
    Up leg L -> H: [H - z_hi (H - L), H - z_lo (H - L)]; down leg H -> L: [L + z_lo (H - L), L + z_hi (H - L)]."""
    a = end - z_lo * (end - start)
    b = end - z_hi * (end - start)
    return (min(a, b), max(a, b))


def extension_target(start: float, end: float, e: float) -> float:
    """Second target (spec 4.4): L + e (H - L) for an up leg, H - e (H - L) for a down leg."""
    return start + e * (end - start)


def has_confluence(zone: tuple[float, float], levels: Sequence[float], tol: float) -> bool:
    """Supply/demand overlap (spec 4.2): some earlier swing level inside the zone widened by +/- tol."""
    lo, hi = zone
    return any(lo - tol <= x <= hi + tol for x in levels)


def is_divergence(direction: int, price1: float, price2: float, rsi1: float, rsi2: float) -> bool:
    """Long: lower low in price and higher low in RSI; short: higher high in price, lower high in RSI."""
    if not (math.isfinite(rsi1) and math.isfinite(rsi2)):
        return False
    return direction * (price1 - price2) > 0.0 and direction * (rsi2 - rsi1) > 0.0


def breakout_level(direction: int, high: np.ndarray, low: np.ndarray, p1: int, p2: int) -> float:
    """The 15m swing between the two pivots (bars p1+1 .. p2-1): highest high for a long, lowest low for a short."""
    if p2 - p1 < 2:
        raise ValueError(f"pivots {p1} and {p2} have no bar between them")
    if direction > 0:
        return float(np.max(high[p1 + 1 : p2]))
    return float(np.min(low[p1 + 1 : p2]))


def stop_level(direction: int, pivot_price: float, atr15: float, buf_atr: float) -> float:
    """Invalidation (spec 4.4): second pivot -/+ buf_atr x ATR15m."""
    return pivot_price - direction * buf_atr * atr15


def first_target(entry_ref: float, stop: float, tp1_r: float) -> float:
    """TP1 = entry + tp1_r x R with R = entry - stop (signed, so the same formula serves both sides)."""
    return entry_ref + tp1_r * (entry_ref - stop)


def skip_reason(
    direction: int, entry_ref: float, stop: float, tp1: float, tp2: float, atr1h: float, max_stop_atr1h: float
) -> str | None:
    """None when the setup may be traded, else why it is skipped (spec 4.4 filters)."""
    r = direction * (entry_ref - stop)
    if r <= 0.0:
        return "stop_wrong_side"
    if r > max_stop_atr1h * atr1h:
        return "stop_too_far"
    if direction * (tp2 - tp1) <= 0.0:
        return "tp2_not_beyond_tp1"
    return None
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_rules.py -q
```

예상: `8 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/rules.py tests/stratb/test_rules.py
git commit -m "feat(stratb): strategy-B rules as pure functions (RSI, pivots, zone, confluence, divergence, levels)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 15분 프레임, 기준 구간, 신호 생성

**Files:**
- Create: `src/ewbot/stratb/signals.py`
- Test: `tests/stratb/helpers.py`
- Test: `tests/stratb/test_signals.py`

**Interfaces:**
- Consumes: Task 1: `M15`, `StratBParams`. Task 3: 모든 규칙 함수. A: `ewbot.data.candles.fill_gaps(df, tf)`, `ewbot.indicators.atr_series(high, low, close, n)`, `ewbot.waves.zigzag.ZigZag(k, atr_len)`(`.update(i, bar) -> Pivot | None`, `.pivots`, `.atr`; `Pivot(i, sub_ts, price, kind, confirmed_at)`), `ewbot.types.Bar`, `H1`.
- Produces: `ewbot.stratb.signals`: `@dataclass(frozen=True) Frame15(ts, open, high, low, close, synthetic, rsi, atr15, piv_lo, piv_hi, h_ts, h_open, h_high, h_low, h_close, h_atr, h_end, last_hour)`(numpy 배열; `len(f)` = 15분봉 수; `h_end[j]` = 1시간봉 j의 마지막 15분봉 인덱스; `last_hour[i]` = 봉 i 마감까지 완성된 마지막 1시간봉, 없으면 −1); `@dataclass(frozen=True) RefLeg(known_i, direction, start, end, start_ts, end_ts, end_i, atr1h, prior)`; `@dataclass(frozen=True) SignalB(i, ts, direction, entry_ref, stop, tp1, tp2, leg_start=0.0, leg_end=0.0, p1_ts=0, p2_ts=0, level=0.0, reason="")`(`i` = 확인 봉의 프레임 인덱스); `build_frame(df15, p=StratBParams()) -> Frame15`; `reference_legs(f, zz_k, p=StratBParams()) -> list[RefLeg]`; `generate_signals(f, p, legs=None) -> list[SignalB]`(봉 순서, 봉마다 최대 1개).
  `tests/stratb/helpers.py`: `T0 = 1700006400`, `RANDOM_T0 = 1690848000`, `M15`, `LONG_SETUP`, `LONG_START = 1060.0`, `SIGNAL_BAR = 177`, `path(start, segments) -> list[float]`, `frame_df(closes, t0=T0, wick=0.5) -> DataFrame`, `long_setup_df(t0=T0)`, `mirror(df, ref=1200.0)`, `random_df(n, seed, t0=RANDOM_T0, start=20000.0)`.

명세 4.1~4.4를 15분봉 위에서 봉 마감 순서대로 적용한다(파일 머리의 docstring이 순서를 정의한다). 15분봉은 A의 `fill_gaps`로 시간 격자를 잇고(합성 봉 표시), 완성된 1시간봉(15분봉 4개)을 A의 `ZigZag`에 넣는다. 기준 구간은 방향마다 가장 최근 것이며(명세 해석 4), 매물대는 L 이전 30일의 1시간 피벗이다(해석 5). 다이버전스 쌍은 해석 6, 진입가 기준과 걸러내기는 해석 7을 따른다. 모든 계산은 인과적이다: 15분 피벗 j는 봉 j+3에서야 쓰이고, 1시간 피벗은 그 시간의 마지막 15분봉 마감에 알려진다.

`tests/stratb/helpers.py`의 `LONG_SETUP`은 손으로 만든 경로다: 1시간 지그재그(k = 2와 3 모두)가 상승 구간 L = 999.5 → H = 1200.5를 확정하고, 그 전의 스윙 고점 1100.5가 38.2~61.8% 구간 [1076.3, 1123.7] 안에 있다. 되돌림이 15분 저점 1109.5(RSI 13.9)와 1104.5(RSI 25.4, 구간 안)를 만들고, 봉 177의 종가 1130이 두 저점 사이의 고점 1125.5를 넘어 롱 신호 하나가 나온다. 가격이 1/64 격자라 `mirror()`(p → 2400 − p)가 정확한 숏 경로를 만든다. `random_df`는 무작위 조각선 경로로, 잘라내기 불변성 검사에 쓴다(시드 0, 3, 4는 신호를 만든다).

- [ ] **Step 1: 실패하는 테스트 작성**

합성 데이터 도우미와 신호 테스트를 만든다. 잘라내기 테스트는 신호가 나온 봉과 그 직전 봉, 그리고 몇몇 고정 시점에서 데이터를 잘라 다시 계산하고, 그 시점까지의 신호가 전체 계산과 같은지 확인한다.

**파일 `tests/stratb/helpers.py`** (새 파일, 전체 내용)

```python
"""Synthetic 15m candles for the strategy-B tests (no network, no real cache).

LONG_SETUP is a hand-made path whose 1h ZigZag (k = 2 or 3) confirms an up leg L = 999.5 -> H = 1200.5
with an earlier swing high (1100.5) inside the 38.2-61.8% zone; the retracement makes two 15m lows
1109.5 (RSI 13.9) and 1104.5 (RSI 25.4, inside the zone) and the close 1130 at bar 177 breaks the 1125.5
swing between them: one long signal at bar 177. Prices sit on a 1/64 grid, so mirror() is exact."""

from __future__ import annotations

import numpy as np
import pandas as pd

T0 = 1_700_006_400  # 2023-11-15 00:00 UTC (hour aligned)
RANDOM_T0 = 1_690_848_000  # 2023-08-01 00:00 UTC: 6,000 random bars end before 2024
M15 = 900
LONG_SETUP = [(40, 1040), (30, 1100), (25, 1000), (25, 1200), (40, 1110), (5, 1125), (7, 1105), (20, 1205), (40, 1180)]
LONG_START = 1060.0
SIGNAL_BAR = 177


def path(start: float, segments: list[tuple[int, float]]) -> list[float]:
    """Closes along straight segments: (number of bars, target close) each, from `start`."""
    out = [float(start)]
    for n, target in segments:
        a = out[-1]
        out += [a + (target - a) * (k + 1) / n for k in range(n)]
    return out


def frame_df(closes, t0: int = T0, wick: float = 0.5) -> pd.DataFrame:
    """15m bars: open = previous close, high/low = body -/+ wick (prices rounded to 1/64)."""
    c = np.round(np.asarray(closes, dtype=float) * 64) / 64
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame(
        {"ts": t0 + M15 * np.arange(c.size), "open": o, "high": np.maximum(o, c) + wick,
         "low": np.minimum(o, c) - wick, "close": c, "volume": 1.0}
    ).astype({"ts": "int64"})


def long_setup_df(t0: int = T0) -> pd.DataFrame:
    return frame_df(path(LONG_START, LONG_SETUP), t0)


def mirror(df: pd.DataFrame, ref: float = 1200.0) -> pd.DataFrame:
    """p -> 2 ref - p (high and low swap): every long structure becomes the mirrored short one."""
    out = df.copy()
    out["open"], out["close"] = 2 * ref - df["open"], 2 * ref - df["close"]
    out["high"], out["low"] = 2 * ref - df["low"], 2 * ref - df["high"]
    return out


def random_df(n: int, seed: int, t0: int = RANDOM_T0, start: float = 20_000.0) -> pd.DataFrame:
    """Random piecewise-linear 15m path (segments of 4-39 bars) with noise and random wicks."""
    rng = np.random.default_rng(seed)
    c = [start]
    while len(c) < n:
        n_seg = int(rng.integers(4, 40))
        target = c[-1] + rng.normal(0, 1) * n_seg * 12
        step = (target - c[-1]) / n_seg
        c += [c[-1] + step * (k + 1) + rng.normal(0, 4) for k in range(n_seg)]
    c = np.round(np.array(c[:n]) * 64) / 64
    o = np.concatenate([[c[0]], c[:-1]])
    up = np.round(np.abs(rng.normal(0, 6, n)) * 64) / 64
    dn = np.round(np.abs(rng.normal(0, 6, n)) * 64) / 64
    return pd.DataFrame(
        {"ts": t0 + M15 * np.arange(n), "open": o, "high": np.maximum(o, c) + up, "low": np.minimum(o, c) - dn,
         "close": c, "volume": 1.0}
    ).astype({"ts": "int64"})
```

**파일 `tests/stratb/test_signals.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from ewbot.stratb.config import StratBParams
from ewbot.stratb.signals import build_frame, generate_signals, reference_legs
from tests.stratb.helpers import SIGNAL_BAR, T0, long_setup_df, mirror, random_df

M15 = 900


def _key(s):
    return (s.i, s.ts, s.direction, s.entry_ref, s.stop, s.tp1, s.tp2, s.p1_ts, s.p2_ts, s.level)


@pytest.fixture(scope="module")
def setup():
    p = StratBParams(zz_k=2.0)
    f = build_frame(long_setup_df(), p)
    return p, f, reference_legs(f, p.zz_k, p)


def test_frame_has_complete_hours_and_flags_gaps():
    df = long_setup_df(T0 + 2 * M15).drop(index=[10])  # starts mid-hour, one bar missing
    f = build_frame(df)
    assert f.ts[0] == T0 + 2 * M15 and f.synthetic.sum() == 1 and f.synthetic[10]
    assert f.h_ts[0] == T0 + 3600 and (f.h_ts % 3600 == 0).all()
    assert (f.ts[f.h_end] == f.h_ts + 2700).all()
    assert f.last_hour[int(f.h_end[0]) - 1] == -1 and f.last_hour[int(f.h_end[0])] == 0


def test_legs_alternate_and_carry_the_earlier_swings(setup):
    _, f, legs = setup
    assert [g.direction for g in legs] == [1, -1, 1, -1, 1]
    up = legs[2]
    assert (up.start, up.end, up.known_i) == (999.5, 1200.5, 135)
    assert up.prior == (1039.5, 1100.5) and f.high[up.end_i] == 1200.5
    assert up.known_i % 4 == 3 and f.ts[up.known_i] % 3600 == 2700


def test_the_long_setup_gives_one_signal_with_the_spec_levels(setup):
    p, f, legs = setup
    (s,) = generate_signals(f, p, legs)
    assert (s.i, s.direction, s.entry_ref, s.level) == (SIGNAL_BAR, 1, 1130.0, 1125.5)
    assert (s.p1_ts, s.p2_ts) == (int(f.ts[160]), int(f.ts[172])) and f.low[172] == 1104.5
    assert s.stop == pytest.approx(1104.5 - 0.2 * f.atr15[SIGNAL_BAR])
    assert s.tp1 == pytest.approx(1130.0 + (1130.0 - s.stop)) and s.tp2 == pytest.approx(999.5 + 1.272 * 201.0)
    assert f.rsi[172] > f.rsi[160]
    assert [x.i for x in generate_signals(build_frame(long_setup_df()), StratBParams(zz_k=3.0))] == [SIGNAL_BAR]


def test_the_mirrored_path_gives_the_mirrored_short(setup):
    p, _, _ = setup
    f = build_frame(mirror(long_setup_df()), p)
    (s,) = generate_signals(f, p)
    assert (s.i, s.direction, s.entry_ref, s.level) == (SIGNAL_BAR, -1, 2400 - 1130.0, 2400 - 1125.5)
    assert s.stop == pytest.approx(2400 - (1104.5 - 0.2 * f.atr15[SIGNAL_BAR]))
    assert s.tp2 == pytest.approx(2400 - (999.5 + 1.272 * 201.0))


def test_no_confluence_no_signal(setup):
    p, f, legs = setup
    legs = [dataclasses.replace(g, prior=(1300.0,)) for g in legs]
    assert generate_signals(f, p, legs) == []


def test_a_close_below_l_invalidates_and_an_undercut_kills_the_pair(setup):
    p, f, legs = setup
    close = f.close.copy()
    close[170] = 999.0  # below L = 999.5, between the leg's confirmation (135) and the signal
    assert generate_signals(dataclasses.replace(f, close=close), p, legs) == []
    close[170] = 1000.0
    assert len(generate_signals(dataclasses.replace(f, close=close), p, legs)) == 1
    low = f.low.copy()
    low[175] = 1104.0  # below the second pivot low 1104.5 after it formed
    assert generate_signals(dataclasses.replace(f, low=low), p, legs) == []


def test_filters_and_expiry(setup):
    p, f, legs = setup
    assert generate_signals(f, dataclasses.replace(p, max_stop_atr1h=0.5), legs) == []  # stop too far
    assert generate_signals(f, dataclasses.replace(p, tp1_r=5.0), legs) == []  # TP2 not beyond TP1
    assert generate_signals(f, dataclasses.replace(p, confirm_hours=10), legs) == []  # 72 h -> 10 h: expired


def test_a_gap_filled_bar_blocks_the_confirmation():
    df = long_setup_df().drop(index=[174]).reset_index(drop=True)
    p = StratBParams(zz_k=2.0)
    f = build_frame(df, p)
    assert f.synthetic[174] and generate_signals(f, p) == []


@pytest.mark.parametrize("seed", [0, 3, 4])
def test_signals_up_to_t_never_change_when_later_bars_are_cut(seed):
    df = random_df(6000, seed)
    for p in (StratBParams(zz_k=2.0), StratBParams(zz_k=3.0, zone=(0.5, 0.786))):
        full = generate_signals(build_frame(df, p), p)
        assert full, "the seed must produce signals"
        cuts = sorted({s.i for s in full} | {s.i - 1 for s in full} | {1000, 2999, 4500, 5998})
        for t in cuts:
            got = generate_signals(build_frame(df.iloc[: t + 1], p), p)
            assert [_key(s) for s in got] == [_key(s) for s in full if s.i <= t], (seed, t)
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_signals.py -q
```

예상: `No module named 'ewbot.stratb.signals'`
예상: `1 error`

`ewbot.stratb.signals`가 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

신호 모듈을 만든다.

**파일 `src/ewbot/stratb/signals.py`** (새 파일, 전체 내용)

```python
"""Strategy-B signals over a gap-filled 15m frame (Plan 4 spec 4.1-4.4). Decisions at a bar's CLOSE
from bars up to it only; the output for bars <= t never depends on later bars (tested by truncation).

1. Structure: complete 1h bars (4 x 15m) feed A's ATR ZigZag (waves/zigzag.py, k = zz_k, ATR 14). A
   pivot confirmed on the hour whose last 15m bar is i makes the leg (previous pivot -> new pivot) a
   reference from bar i on: a confirmed high ends an up leg L -> H, the reference of long setups; a
   confirmed low ends a down leg H -> L, the reference of short setups. Each side keeps its latest leg
   ("the most recently confirmed up leg") until the next leg of the same direction replaces it.
2. Candidate (spec 4.2): retracement zone [H - z_hi (H - L), H - z_lo (H - L)] (mirrored for shorts) and a
   1h pivot before the leg's start, at most confluence_lookback_hours earlier, inside the zone +/-
   confluence_atr x ATR1h. No overlap -> the leg is never traded.
3. Life: from the confirming bar for confirm_hours (72 h), until the next leg of the same direction, or
   until a 15m close beyond the start (below L for a long) invalidates it.
4. Confirmation (spec 4.3): two consecutive 15m pivots of the trade's kind (lows for a long, w = 3) after
   the leg's extreme 15m bar make a lower low with a higher RSI(14) low, the second inside the zone; the
   latest such pair stays the setup until a newer pair replaces it or a later bar undercuts its second
   pivot. Confirmed at the first close beyond the swing between the two pivots (highest high for a
   long) with no gap-filled bar from p1 - w to the confirming bar.
5. Levels (spec 4.4): entry_ref = the confirming close; stop = second pivot -/+ 0.2 x ATR15; TP1 = entry +
   tp1_r x R; TP2 = start + e (end - start). Skipped (the reference stays alive) when R > 2.5 x ATR1h or TP2
   is not beyond TP1. A signal consumes its reference: one signal per leg."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ewbot.data.candles import fill_gaps
from ewbot.indicators import atr_series
from ewbot.stratb.config import M15, StratBParams
from ewbot.stratb.rules import (
    breakout_level,
    extension_target,
    first_target,
    has_confluence,
    is_divergence,
    pivot_highs,
    pivot_lows,
    retracement_zone,
    rsi_series,
    skip_reason,
    stop_level,
)
from ewbot.types import H1, Bar
from ewbot.waves.zigzag import ZigZag


@dataclass(frozen=True)
class Frame15:
    """Gap-filled 15m bars with their indicators, and the complete 1h bars built from them."""

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    synthetic: np.ndarray
    rsi: np.ndarray
    atr15: np.ndarray
    piv_lo: np.ndarray
    piv_hi: np.ndarray
    h_ts: np.ndarray
    h_open: np.ndarray
    h_high: np.ndarray
    h_low: np.ndarray
    h_close: np.ndarray
    h_atr: np.ndarray
    h_end: np.ndarray  # 15m index of each 1h bar's last 15m bar (its close is the hour's close)
    last_hour: np.ndarray  # per 15m bar: index of the latest 1h bar closed at its close, -1 if none

    def __len__(self) -> int:
        return int(self.ts.size)


@dataclass(frozen=True, slots=True)
class RefLeg:
    known_i: int  # 15m bar at whose close the leg is known
    direction: int  # +1: up leg, long setups; -1: down leg, short setups
    start: float  # L of an up leg, H of a down leg
    end: float  # H of an up leg, L of a down leg
    start_ts: int  # 1h bar of the start pivot
    end_ts: int  # 1h bar of the end pivot
    end_i: int  # 15m bar of the end pivot's extreme (first one reaching it)
    atr1h: float  # ATR1h at confirmation
    prior: tuple[float, ...]  # 1h pivot prices before the start pivot, within the lookback


@dataclass(frozen=True, slots=True)
class SignalB:
    i: int  # confirming 15m bar (index into the frame)
    ts: int
    direction: int
    entry_ref: float
    stop: float
    tp1: float
    tp2: float
    leg_start: float = 0.0
    leg_end: float = 0.0
    p1_ts: int = 0
    p2_ts: int = 0
    level: float = 0.0
    reason: str = ""


def build_frame(df15: pd.DataFrame, p: StratBParams = StratBParams()) -> Frame15:
    """Gap-fill (A's fill_gaps on the 15m grid: flat synthetic bars) and compute every causal series."""
    df = fill_gaps(df15, tf=M15)
    ts = df["ts"].to_numpy(dtype=np.int64)
    o, h, lo, c = (df[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close"))
    syn = df["synthetic"].to_numpy(dtype=bool)
    ends = np.flatnonzero(ts % H1 == H1 - M15)
    ends = ends[(ends >= 3) & (ts[np.maximum(ends - 3, 0)] == ts[ends] - (H1 - M15))]
    starts = ends - 3
    hh = np.maximum.reduce([h[ends - k] for k in range(4)]) if ends.size else np.zeros(0)
    hl = np.minimum.reduce([lo[ends - k] for k in range(4)]) if ends.size else np.zeros(0)
    hc = c[ends]
    return Frame15(
        ts=ts, open=o, high=h, low=lo, close=c, synthetic=syn,
        rsi=rsi_series(c, p.rsi_len), atr15=atr_series(h, lo, c, p.atr_len),
        piv_lo=pivot_lows(lo, p.pivot_w), piv_hi=pivot_highs(h, p.pivot_w),
        h_ts=ts[starts], h_open=o[starts], h_high=hh, h_low=hl, h_close=hc,
        h_atr=atr_series(hh, hl, hc, p.atr_len), h_end=ends,
        last_hour=np.searchsorted(ends, np.arange(ts.size), side="right") - 1,
    )


def reference_legs(f: Frame15, zz_k: float, p: StratBParams = StratBParams()) -> list[RefLeg]:
    """Every leg the 1h ZigZag confirms, in order (spec 4.1)."""
    zz = ZigZag(zz_k, p.atr_len)
    legs: list[RefLeg] = []
    lookback = p.confluence_lookback_hours * H1
    for j in range(f.h_ts.size):
        bar = Bar(int(f.h_ts[j]), float(f.h_open[j]), float(f.h_high[j]), float(f.h_low[j]), float(f.h_close[j]), 0.0)
        piv = zz.update(j, bar)
        if piv is None or len(zz.pivots) < 2:
            continue
        prev = zz.pivots[-2]
        d = piv.kind
        first = int(f.h_end[piv.i]) - 3
        seg = f.high[first : first + 4] if d > 0 else f.low[first : first + 4]
        k = int(np.argmax(seg)) if d > 0 else int(np.argmin(seg))
        prior = tuple(q.price for q in zz.pivots[:-2] if q.sub_ts >= prev.sub_ts - lookback)
        legs.append(RefLeg(int(f.h_end[j]), d, prev.price, piv.price, prev.sub_ts, piv.sub_ts, first + k,
                           float(zz.atr), prior))
    return legs


def _scan(f: Frame15, p: StratBParams, leg: RefLeg, last_i: int) -> SignalB | None:
    d, w = leg.direction, p.pivot_w
    zone = retracement_zone(leg.start, leg.end, *p.zone)
    if not has_confluence(zone, leg.prior, p.confluence_atr * leg.atr1h):
        return None
    piv = f.piv_lo if d > 0 else f.piv_hi
    ext = f.low if d > 0 else f.high
    tp2 = extension_target(leg.start, leg.end, p.ext)
    prev: int | None = None  # latest pivot of the trade's kind after the leg's extreme
    pair: tuple[int, int] | None = None  # latest consecutive divergence pair, second pivot in the zone

    def add(j: int) -> None:
        nonlocal prev, pair
        in_zone = zone[0] <= ext[j] <= zone[1]
        if prev is not None and in_zone and is_divergence(d, ext[prev], ext[j], f.rsi[prev], f.rsi[j]):
            pair = (prev, j)
        prev = j

    for j in range(leg.end_i + 1, leg.known_i - w):
        if piv[j]:
            add(j)
    for i in range(leg.known_i, last_i + 1):
        if d * (f.close[i] - leg.start) < 0.0:
            return None  # invalidated: a close beyond the leg's start
        j = i - w
        if j > leg.end_i and piv[j]:
            add(j)
        if pair is None:
            continue
        p1, p2 = pair
        x2 = float(ext[p2])
        after = ext[p2 + 1 : i + 1]
        if after.size and d * (float(after.min() if d > 0 else after.max()) - x2) < 0.0:
            pair = None  # the second pivot was undercut: wait for a new pair
            continue
        level = breakout_level(d, f.high, f.low, p1, p2)
        if d * (f.close[i] - level) <= 0.0 or f.synthetic[max(p1 - w, 0) : i + 1].any():
            continue
        hour = int(f.last_hour[i])
        atr15, atr1h = float(f.atr15[i]), float(f.h_atr[hour]) if hour >= 0 else float("nan")
        if not (np.isfinite(atr15) and np.isfinite(atr1h)):
            continue
        entry = float(f.close[i])
        stop = stop_level(d, x2, atr15, p.stop_buf_atr)
        tp1 = first_target(entry, stop, p.tp1_r)
        if skip_reason(d, entry, stop, tp1, tp2, atr1h, p.max_stop_atr1h) is not None:
            continue
        p1_ts, p2_ts = int(f.ts[p1]), int(f.ts[p2])
        return SignalB(i=i, ts=int(f.ts[i]), direction=d, entry_ref=entry, stop=stop, tp1=tp1, tp2=tp2,
                       leg_start=leg.start, leg_end=leg.end, p1_ts=p1_ts, p2_ts=p2_ts, level=level,
                       reason=f"{'long' if d > 0 else 'short'} div {p1_ts}/{p2_ts} break {level:.2f}")
    return None


def generate_signals(f: Frame15, p: StratBParams, legs: list[RefLeg] | None = None) -> list[SignalB]:
    """Every strategy-B signal over the frame, in bar order. Longs use the latest up leg and shorts the
    latest down leg, each until the next leg of the same direction; at most one signal per leg, and on
    the (rare) bar where a long and a short both confirm, the newer leg's signal."""
    legs = reference_legs(f, p.zz_k, p) if legs is None else legs
    n = len(f)
    life = p.confirm_hours * H1 // M15
    found: list[tuple[int, int, SignalB]] = []
    for d in (1, -1):
        mine = [leg for leg in legs if leg.direction == d]
        for k, leg in enumerate(mine):
            nxt = mine[k + 1].known_i if k + 1 < len(mine) else n
            sig = _scan(f, p, leg, min(nxt - 1, leg.known_i + life, n - 1))
            if sig is not None:
                found.append((sig.i, -leg.known_i, sig))
    out: list[SignalB] = []
    for i, _, sig in sorted(found, key=lambda x: (x[0], x[1])):
        if not out or out[-1].i != i:
            out.append(sig)
    return out
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_signals.py -q
```

예상: `11 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/signals.py tests/stratb/helpers.py tests/stratb/test_signals.py
git commit -m "feat(stratb): 1h reference legs on A's ZigZag and 15m divergence-breakout signals (truncation-invariant)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 15분 엔진(체결, 수량, 손실 한도, 비용)

**Files:**
- Create: `src/ewbot/stratb/engine.py`
- Test: `tests/stratb/test_engine.py`

**Interfaces:**
- Consumes: Task 1: `M15`, `BConfig`. Task 4: `Frame15`, `SignalB`(테스트는 `build_frame`, `generate_signals`, `tests.stratb.helpers.T0`, `random_df`도 쓴다). A: `ewbot.engine.loop.EquityPoint(ts, equity)`, `Trade(entry_ts, exit_ts, setup, direction, contracts, entry_price, exit_price_avg, stop0, tp1, tp2, pnl, fees, funding, r_multiple, exit_reason, snapshot)`; `ewbot.execution.costs.fee(price, contracts, c)`, `slip(price, side, c)`, `funding_per_hour(mark, contracts, direction, c)`; `ewbot.risk.sizing.size_contracts(equity, entry, stop, direction, risk, risk_mult, c) -> int`; `ewbot.risk.breakers.CircuitBreakers(p, equity0)`(`on_equity(ts, equity)`, `on_trade_closed(ts, pnl)`, `can_enter(ts)`, `halted`).
- Produces: `ewbot.stratb.engine`: `SETUP = "B"`; `@dataclass(frozen=True) FillB(ts, kind, contracts, price, fee, trade_key)`(`kind` ∈ ENTRY, TP1, TP2, STOP, EXIT; `trade_key` = 진입 봉 ts); `@dataclass(frozen=True) StopMoveB(ts, trade_key, old, new, kind="BREAKEVEN")`; `@dataclass(frozen=True) OrderB(ts, direction, contracts, tp1_contracts)`; `@dataclass(frozen=True) BResult(trades, equity, signals, orders, fills, stops, end_fill, events)`(`signals`와 `orders`는 진입 주문을 낸 신호와 그 수량, 같은 순서; `end_fill`은 마지막 봉의 강제 청산 `FillB(kind="END")` 또는 `None`; `events`는 `(ts, "ENTRY_SKIPPED:gap" | "ENTRY_SKIPPED:open_beyond_stop" | "SIGNAL_REJECTED:size" | "SIGNAL_REJECTED:breaker" | "DAILY_HALT" | "TOTAL_HALT" | "CONSEC_PAUSE")`); `tp1_contracts(n, frac) -> int`; `run_engine(f, signals, cfg, start_ts=None) -> BResult`. 거래는 A의 `Trade`이고 `setup = "B"`, `snapshot = {"signal_ts", "entry_ref", "leg", "pivots", "level", "reason"}`, `r_multiple = pnl / (|entry_price − stop0| × contracts × 0.01)`, `exit_reason` ∈ STOP, TP1(1계약), TP2, TIME, BREAKER, END. 자산은 매시 마지막 15분봉 마감의 평가액이고 시각은 그 시간의 시작이다.

파일 머리의 docstring이 봉 안의 처리 순서(1~9)를 정의한다. 체결 규칙은 A의 `PaperBroker`와 같다(명세 해석 9). 비용·수량·손실 한도는 A의 함수를 그대로 부르므로 같은 값이 쓰인다. 엔진은 신호를 받기만 하므로(신호 생성과 분리) 무작위 대조군(Task 7)도 같은 엔진을 쓴다. 엔진이 판단 추적(주문 수량, 체결, 본절 이동, 마지막 봉 청산)을 직접 내므로 A처럼 클래스를 바꿔 끼우는 추적이 필요 없다.

- [ ] **Step 1: 실패하는 테스트 작성**

손으로 만든 15분봉과 손으로 만든 `SignalB`로 다음 체결을 확인한다: 다음 시가 체결과 0.5% 수량(41계약), 3배 상한(150계약), 같은 봉의 손절 우선, TP1 절반·본절 이동·TP2, 본절 잔량의 손절과 원래 손절 유지, 시가 갭 손절, 48시간 시간 손절과 15분 펀딩, 1계약 TP1 전량, 마지막 봉 청산과 시간 단위 자산, 누락 봉·손절 너머 시가의 진입 취소, 전체 낙폭 한도의 청산과 진입 차단, 그리고 잘라낸 데이터의 체결·주문이 전체와 같은지.

**파일 `tests/stratb/test_engine.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import dataclasses

import pandas as pd
import pytest

from ewbot.config import BreakerParams, CostParams
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.risk.sizing import size_contracts
from ewbot.stratb.config import RISK_B, BConfig, StratBParams
from ewbot.stratb.engine import run_engine, tp1_contracts
from ewbot.stratb.signals import SignalB, build_frame, generate_signals
from tests.stratb.helpers import T0, random_df

M15, H1 = 900, 3600
C = CostParams()


def bars(rows, t0=T0):
    """rows of (open, high, low, close) -> a 15m frame."""
    return build_frame(pd.DataFrame(
        [(t0 + k * M15, o, h, lo, c, 1.0) for k, (o, h, lo, c) in enumerate(rows)],
        columns=["ts", "open", "high", "low", "close", "volume"]))


def flat(n, px=20_000.0):
    return [(px, px + 5, px - 5, px)] * n


def sig(i, f, stop, tp1, tp2, d=1):
    return SignalB(i=i, ts=int(f.ts[i]), direction=d, entry_ref=float(f.close[i]), stop=stop, tp1=tp1, tp2=tp2,
                   reason="test")


def test_entry_fills_at_the_next_open_with_slippage_fee_and_half_percent_sizing():
    f = bars(flat(10))
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    n = size_contracts(10_000.0, 20_000.0, 19_900.0, 1, RISK_B, 1.0, C)
    assert n == 41 and res.orders[0].contracts == 41 and res.orders[0].tp1_contracts == 20
    entry = res.fills[0]
    px = slip(20_000.0, 1, C)
    assert (entry.kind, entry.ts, entry.price, entry.contracts) == ("ENTRY", int(f.ts[3]), px, 41)
    assert entry.fee == pytest.approx(fee(px, 41, C))
    assert res.signals[0].i == 2 and len(res.trades) == 1 and res.trades[0].exit_reason == "END"


def test_leverage_is_capped_at_3x():
    f = bars(flat(10))
    res = run_engine(f, [sig(2, f, 19_990.0, 20_100.0, 20_300.0)], BConfig())
    assert res.orders[0].contracts == 150  # 3 x 10,000 / 20,000 = 1.5 BTC = 150 contracts of 0.01


def test_the_stop_beats_a_target_touched_in_the_same_bar():
    rows = flat(4) + [(20_000.0, 20_500.0, 19_800.0, 20_000.0)] + flat(3)
    f = bars(rows)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    (t,) = res.trades
    assert [x.kind for x in res.fills] == ["ENTRY", "STOP"] and res.fills[1].price == slip(19_900.0, -1, C)
    assert t.exit_reason == "STOP" and t.pnl < 0 and t.r_multiple < -1.0


def test_tp1_takes_half_moves_the_rest_to_breakeven_and_tp2_closes_it():
    rows = flat(4) + [(20_000.0, 20_150.0, 19_950.0, 20_120.0), (20_120.0, 20_350.0, 20_100.0, 20_300.0)] + flat(2, 20_300.0)
    f = bars(rows)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    (t,) = res.trades
    assert [(x.kind, x.contracts, x.price) for x in res.fills[1:]] == [("TP1", 20, 20_100.0), ("TP2", 21, 20_300.0)]
    (mv,) = res.stops
    entry_px = res.fills[0].price
    assert (mv.ts, mv.old, mv.new, mv.kind) == (int(f.ts[4]), 19_900.0, entry_px, "BREAKEVEN")
    gross = (20_100.0 - entry_px) * 20 * 0.01 + (20_300.0 - entry_px) * 21 * 0.01
    assert t.exit_reason == "TP2" and t.pnl == pytest.approx(gross - t.fees - t.funding)
    assert t.fees == pytest.approx(sum(x.fee for x in res.fills))


def test_the_breakeven_rest_stops_at_the_entry_and_original_keeps_the_stop():
    rows = flat(4) + [(20_000.0, 20_150.0, 19_950.0, 20_120.0), (20_120.0, 20_120.0, 19_850.0, 19_860.0)] + flat(2, 19_860.0)
    f = bars(rows)
    s = [sig(2, f, 19_900.0, 20_100.0, 20_300.0)]
    be = run_engine(f, s, BConfig())
    assert [x.kind for x in be.fills] == ["ENTRY", "TP1", "STOP"]
    assert be.fills[2].price == slip(be.fills[0].price, -1, C)
    orig = run_engine(f, s, BConfig(params=StratBParams(remainder="original")))
    assert orig.stops == () and orig.fills[2].price == slip(19_900.0, -1, C)


def test_a_gap_through_the_stop_fills_at_the_open():
    rows = flat(4) + [(19_800.0, 19_850.0, 19_700.0, 19_750.0)] + flat(2, 19_750.0)
    f = bars(rows)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    assert res.fills[1].kind == "STOP" and res.fills[1].price == slip(19_800.0, -1, C)


def test_the_time_stop_closes_at_the_open_48_hours_after_the_entry_and_funding_is_quarter_hourly():
    f = bars(flat(250))
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    (t,) = res.trades
    assert t.exit_reason == "TIME" and t.exit_ts == t.entry_ts + 48 * H1 and res.fills[-1].kind == "EXIT"
    assert res.fills[-1].price == slip(20_000.0, -1, C)
    held = (t.exit_ts - t.entry_ts) // M15  # bars from the entry bar to the one before the exit
    assert t.funding == pytest.approx(held * funding_per_hour(20_000.0, 41, 1, C) / 4)


def test_a_single_contract_closes_at_tp1():
    f = bars(flat(4, 100.0) + [(100.0, 103.0, 99.5, 102.0)] + flat(2, 102.0))
    res = run_engine(f, [sig(2, f, 90.0, 101.0, 105.0)], BConfig(initial_equity=30.0))
    assert res.orders[0].contracts == 1 and res.orders[0].tp1_contracts == 1
    assert res.trades[0].exit_reason == "TP1" and tp1_contracts(1, 0.5) == 1 and tp1_contracts(7, 0.5) == 3


def test_end_of_data_entries_from_start_and_hourly_equity():
    f = bars(flat(16))
    s = [sig(2, f, 19_900.0, 20_100.0, 20_300.0), sig(15, f, 19_900.0, 20_100.0, 20_300.0)]
    res = run_engine(f, s, BConfig(), start_ts=T0 + H1)
    assert res.orders == ()  # bar 2 is before start_ts, bar 15 is the last bar
    assert [p.ts for p in res.equity] == [T0 + H1, T0 + 2 * H1, T0 + 3 * H1]
    res = run_engine(f, s[:1], BConfig())
    assert res.end_fill is not None and res.end_fill.kind == "END" and res.trades[-1].exit_reason == "END"
    assert [x.kind for x in res.fills] == ["ENTRY"] and res.equity[-1].equity == pytest.approx(res.trades[-1].pnl + 10_000.0)


def test_entries_are_dropped_on_a_gap_bar_or_an_open_beyond_the_stop():
    df = pd.DataFrame([(T0 + k * M15, 20_000.0, 20_005.0, 19_995.0, 20_000.0, 1.0) for k in range(10) if k != 3],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    f = build_frame(df)
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    assert res.fills == () and (int(f.ts[3]), "ENTRY_SKIPPED:gap") in res.events
    f = bars(flat(3) + [(19_890.0, 19_900.0, 19_880.0, 19_890.0)] + flat(3))
    res = run_engine(f, [sig(2, f, 19_900.0, 20_100.0, 20_300.0)], BConfig())
    assert res.fills == () and (int(f.ts[3]), "ENTRY_SKIPPED:open_beyond_stop") in res.events


def test_the_total_drawdown_breaker_flattens_and_blocks_entries():
    rows = flat(4) + [(20_000.0, 20_005.0, 19_950.0, 19_960.0)] * 4 + flat(8, 19_960.0)
    f = bars(rows)
    cfg = BConfig(breakers=dataclasses.replace(BreakerParams(), total_dd=0.0001))
    res = run_engine(f, [sig(2, f, 19_000.0, 21_000.0, 22_000.0), sig(10, f, 19_000.0, 21_000.0, 22_000.0)], cfg)
    assert ("TOTAL_HALT" in {e for _, e in res.events}) and res.trades[0].exit_reason == "BREAKER"
    assert (int(f.ts[10]), "SIGNAL_REJECTED:breaker") in res.events and len(res.trades) == 1


@pytest.mark.parametrize("seed", [3, 4])
def test_decisions_up_to_t_never_change_when_later_bars_are_cut(seed):
    df = random_df(6000, seed)
    p = StratBParams(zz_k=2.0, zone=(0.5, 0.786))
    full_f = build_frame(df, p)
    full = run_engine(full_f, generate_signals(full_f, p), BConfig(params=p))
    assert full.fills
    for t in sorted({x.ts // M15 * M15 for x in full.fills} | {int(df["ts"].iloc[2999])}):
        k = int((t - int(df["ts"].iloc[0])) // M15)
        f = build_frame(df.iloc[: k + 1], p)
        cut = run_engine(f, generate_signals(f, p), BConfig(params=p))
        assert [x for x in cut.fills] == [x for x in full.fills if x.ts <= t]
        assert list(cut.stops) == [x for x in full.stops if x.ts <= t]
        assert [o for o in cut.orders] == [o for o in full.orders if o.ts < t]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_engine.py -q
```

예상: `No module named 'ewbot.stratb.engine'`
예상: `1 error`

`ewbot.stratb.engine`이 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

엔진을 만든다.

**파일 `src/ewbot/stratb/engine.py`** (새 파일, 전체 내용)

```python
"""Strategy-B 15-minute backtest loop (Plan 4 spec 4.4, 6.2). Costs (execution/costs.py), sizing
(risk/sizing.py) and the circuit breakers (risk/breakers.py) are strategy A's, imported unchanged.

Per 15m bar i, in this order:
  1. market orders queued at the previous close fill at this bar's open +/- slippage: a queued exit (TIME,
     BREAKER) first, then a queued entry - dropped when this bar is gap-filled or opens at/beyond its stop;
  2. intrabar, as A's PaperBroker: open at/beyond the stop -> stop at the open; stop touched -> stop at the
     stop (it beats any target in the same bar); else TP1 (half, limit, strict cross) then TP2 (the rest);
  3. TP1 filled and remainder == "breakeven" -> the rest's stop moves to the entry price, from the next bar;
  4. funding for the quarter hour (A's hourly funding / 4) on the position open at the close;
  5. time stop: open 48 h at this close -> exit queued for the next open;
  6. at each hour's close (the bar at hh:45): breakers.on_equity(close instant, equity); TOTAL_HALT -> exit
     queued, no more entries;
  7. entry: not on the last bar, ts >= start_ts, flat with nothing queued, breakers.can_enter, a signal on
     this bar -> size_contracts(equity, entry_ref, stop, 0.5% risk, 3x cap); TP1 takes half (1 contract:
     all of it);
  8. last bar: an open position is closed at the close (END, not a decision);
  9. at each hour's close with hour >= start_ts: EquityPoint(hour open ts, equity at the close) - hourly
     equity, so A's metrics (annualised with 8760 hours) apply unchanged."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from ewbot.engine.loop import EquityPoint, Trade
from ewbot.execution.costs import fee, funding_per_hour, slip
from ewbot.risk.breakers import CircuitBreakers
from ewbot.risk.sizing import size_contracts
from ewbot.stratb.config import M15, BConfig
from ewbot.stratb.signals import Frame15, SignalB
from ewbot.types import H1

SETUP = "B"
_HOUR_LAST = H1 - M15  # ts % H1 of an hour's last 15m bar


@dataclass(frozen=True, slots=True)
class FillB:
    ts: int  # bar open time of the fill
    kind: str  # "ENTRY", "TP1", "TP2", "STOP", "EXIT" ("END" only as BResult.end_fill)
    contracts: int
    price: float
    fee: float
    trade_key: int  # entry ts of the position


@dataclass(frozen=True, slots=True)
class StopMoveB:
    ts: int  # bar whose close moved the stop (effective from the next bar)
    trade_key: int
    old: float
    new: float
    kind: str = "BREAKEVEN"


@dataclass(frozen=True, slots=True)
class OrderB:
    ts: int  # signal bar
    direction: int
    contracts: int
    tp1_contracts: int


@dataclass(frozen=True)
class BResult:
    trades: tuple[Trade, ...]
    equity: tuple[EquityPoint, ...]
    signals: tuple[SignalB, ...]  # the signals that queued an entry, with their orders below
    orders: tuple[OrderB, ...]
    fills: tuple[FillB, ...]
    stops: tuple[StopMoveB, ...]
    end_fill: FillB | None
    events: tuple[tuple[int, str], ...]


@dataclass
class _Pos:
    sig: SignalB
    direction: int
    total: int
    contracts: int
    entry_ts: int
    entry_price: float
    stop: float
    tp1: float | None
    tp2: float
    tp1_n: int
    fees: float
    gross: float = 0.0
    funding: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    snapshot: dict = field(default_factory=dict)


def tp1_contracts(n: int, frac: float) -> int:
    """Contracts closed at TP1: n if n <= 1, else max(1, floor(n x frac))."""
    return n if n <= 1 else min(n, max(1, math.floor(n * frac + 1e-9)))


def run_engine(f: Frame15, signals: Iterable[SignalB], cfg: BConfig, start_ts: int | None = None) -> BResult:
    """One run over the whole frame (cut it at the end before building it). Entries only from start_ts."""
    c, cs = cfg.costs, cfg.costs.contract_size
    by_i: dict[int, SignalB] = {}
    for s in signals:
        by_i.setdefault(s.i, s)
    ts, op, hi, lo, cl = (a.tolist() for a in (f.ts, f.open, f.high, f.low, f.close))
    syn = f.synthetic.tolist()
    time_stop = cfg.params.time_stop_hours * H1
    breakers = CircuitBreakers(cfg.breakers, cfg.initial_equity)
    cash = float(cfg.initial_equity)
    pos: _Pos | None = None
    pending: tuple[SignalB, int, int] | None = None
    exit_req: str | None = None
    trades: list[Trade] = []
    equity: list[EquityPoint] = []
    sigs: list[SignalB] = []
    orders: list[OrderB] = []
    fills: list[FillB] = []
    stops: list[StopMoveB] = []
    events: list[tuple[int, str]] = []
    end_fill: FillB | None = None

    def close(t: int, kind: str, price: float, qty: int, reason: str | None) -> FillB:
        nonlocal cash, pos
        assert pos is not None
        fe = fee(price, qty, c)
        realized = pos.direction * (price - pos.entry_price) * qty * cs
        cash += realized - fe
        pos.fees += fe
        pos.gross += realized
        pos.contracts -= qty
        pos.exit_qty += qty
        pos.exit_value += price * qty
        fill = FillB(t, kind, qty, price, fe, pos.entry_ts)
        if pos.contracts == 0:
            trade = _trade(pos, t, reason or kind, cs)
            trades.append(trade)
            for ev in breakers.on_trade_closed(t, trade.pnl):
                events.append((t, ev))
            pos = None
        return fill

    def mark(price: float) -> float:
        if pos is None:
            return cash
        return cash + pos.direction * (price - pos.entry_price) * pos.contracts * cs

    last = len(ts) - 1
    for i in range(len(ts)):
        t = ts[i]
        if exit_req is not None and pos is not None:  # 1. queued market orders
            fills.append(close(t, "EXIT", slip(op[i], -pos.direction, c), pos.contracts, exit_req))
        exit_req = None
        if pending is not None:
            sig, n, n1 = pending
            pending = None
            d = sig.direction
            if syn[i]:
                events.append((t, "ENTRY_SKIPPED:gap"))
            elif d * (op[i] - sig.stop) <= 0.0:
                events.append((t, "ENTRY_SKIPPED:open_beyond_stop"))
            else:
                px = slip(op[i], d, c)
                fe = fee(px, n, c)
                cash -= fe
                pos = _Pos(sig, d, n, n, t, px, sig.stop, sig.tp1, sig.tp2, n1, fe, snapshot=_snapshot(sig))
                fills.append(FillB(t, "ENTRY", n, px, fe, t))
        if pos is not None:  # 2. intrabar
            d = pos.direction
            tp1_hit = False
            if d * (op[i] - pos.stop) <= 0.0:
                fills.append(close(t, "STOP", slip(op[i], -d, c), pos.contracts, None))
            elif (lo[i] <= pos.stop) if d > 0 else (hi[i] >= pos.stop):
                fills.append(close(t, "STOP", slip(pos.stop, -d, c), pos.contracts, None))
            else:
                fav = hi[i] if d > 0 else lo[i]
                if pos.tp1 is not None and d * (fav - pos.tp1) > 0.0:
                    px, pos.tp1, tp1_hit = pos.tp1, None, True
                    fills.append(close(t, "TP1", px, min(pos.tp1_n, pos.contracts), None))
                if pos is not None and pos.tp1 is None and d * (fav - pos.tp2) > 0.0:
                    fills.append(close(t, "TP2", pos.tp2, pos.contracts, None))
            if pos is not None and tp1_hit and cfg.params.remainder == "breakeven" and pos.stop != pos.entry_price:
                stops.append(StopMoveB(t, pos.entry_ts, pos.stop, pos.entry_price))  # 3.
                pos.stop = pos.entry_price
        if pos is not None:  # 4. funding for the quarter hour
            cost = funding_per_hour(cl[i], pos.contracts, pos.direction, c) * (M15 / H1)
            cash -= cost
            pos.funding += cost
            if t + M15 - pos.entry_ts >= time_stop:  # 5.
                exit_req = "TIME"
        hour_close = t % H1 == _HOUR_LAST
        if hour_close:  # 6.
            for ev in breakers.on_equity(t + M15, mark(cl[i])):
                events.append((t, ev))
            if breakers.halted and pos is not None:
                exit_req = "BREAKER"
        if i < last and pos is None and pending is None and (start_ts is None or t >= start_ts):  # 7.
            sig = by_i.get(i)
            if sig is not None:
                if not breakers.can_enter(t + M15):
                    events.append((t, "SIGNAL_REJECTED:breaker"))
                else:
                    n = size_contracts(cash, sig.entry_ref, sig.stop, sig.direction, cfg.risk, 1.0, c)
                    if n == 0:
                        events.append((t, "SIGNAL_REJECTED:size"))
                    else:
                        n1 = tp1_contracts(n, cfg.tp1_frac)
                        pending = (sig, n, n1)
                        sigs.append(sig)
                        orders.append(OrderB(t, sig.direction, n, n1))
        if i == last:  # 8.
            pending = None
            if pos is not None:
                key = pos.entry_ts
                f_end = close(t, "EXIT", slip(cl[i], -pos.direction, c), pos.contracts, "END")
                end_fill = FillB(f_end.ts, "END", f_end.contracts, f_end.price, f_end.fee, key)
        if hour_close and (start_ts is None or t - _HOUR_LAST >= start_ts):  # 9.
            equity.append(EquityPoint(t - _HOUR_LAST, mark(cl[i])))
    return BResult(tuple(trades), tuple(equity), tuple(sigs), tuple(orders), tuple(fills), tuple(stops), end_fill,
                   tuple(events))


def _snapshot(sig: SignalB) -> dict:
    return {
        "signal_ts": sig.ts, "entry_ref": sig.entry_ref, "leg": [sig.leg_start, sig.leg_end],
        "pivots": [sig.p1_ts, sig.p2_ts], "level": sig.level, "reason": sig.reason,
    }


def _trade(pos: _Pos, exit_ts: int, reason: str, cs: float) -> Trade:
    pnl = pos.gross - pos.fees - pos.funding
    risk_usd = abs(pos.entry_price - pos.sig.stop) * pos.total * cs
    return Trade(
        entry_ts=pos.entry_ts, exit_ts=exit_ts, setup=SETUP, direction=pos.direction, contracts=pos.total,
        entry_price=pos.entry_price, exit_price_avg=pos.exit_value / pos.exit_qty, stop0=pos.sig.stop,
        tp1=pos.sig.tp1, tp2=pos.sig.tp2, pnl=pnl, fees=pos.fees, funding=pos.funding,
        r_multiple=pnl / risk_usd if risk_usd > 0.0 else 0.0, exit_reason=reason, snapshot=pos.snapshot,
    )
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_engine.py -q
```

예상: `13 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/engine.py tests/stratb/test_engine.py
git commit -m "feat(stratb): 15m engine with A's costs, sizing and breakers (stop first, half at TP1, 48h time stop)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 2024 가드, 단일 백테스트, 그리드, 워크포워드

**Files:**
- Create: `src/ewbot/stratb/research.py`
- Test: `tests/stratb/test_research.py`

**Interfaces:**
- Consumes: Task 1: `GRID_B`, `BConfig`, `Combo`, `StratBParams`, `combo_to_json`, `config_for`, `grid_combos`. Task 4: `Frame15`, `RefLeg`, `build_frame`, `generate_signals`, `reference_legs`. Task 5: `BResult`, `run_engine`. A: `ewbot.backtest.metrics.compute_metrics(equity, trades, start_ts=None, end_ts=None) -> Metrics`; `ewbot.backtest.walkforward`: `HOLDOUT_START`, `WF_START`, `MIN_TRAIN_TRADES = 12`, `MIN_ELIGIBLE_NEIGHBOUR_FRAC = 0.5`, `folds() -> list[tuple[int, int, int, int]]`, `oos_returns(res, ts0, ts1) -> list[tuple[int, float]]`(`res.equity`만 읽음); `ewbot.backtest.runner.utc_ts`; `ewbot.engine.loop.EquityPoint`.
- Produces: `ewbot.stratb.research`: `refuse_holdout_bars(df15, end_ts, include_holdout=False) -> None`(2024 봉을 볼 실행이면 `ValueError`); `backtest_b(df15, cfg, start_ts=None, end_ts=None, *, include_holdout=False) -> BResult`(end_ts 이전 봉으로 프레임을 만들고 신호·엔진 실행); `run_grid_b(df15, start_ts, end_ts, workers=1) -> dict[Combo, BResult]`(32개, 손실 한도 끔, `grid_combos()` 순서); `pick_plateau_b(calmar, n_trades, min_trades=12) -> Combo`(고를 것이 없으면 `ValueError`); `evaluate_folds_b(results, fold_list, equity0, min_trades=12) -> dict`(키 `keys`, `folds`[각 `train`, `test`, `chosen`, `train_calmar`, `oos`], `stitched_equity`, `stitched_metrics`, `oos_trades`, `chosen_final`, `chosen_final_from_fold`, `chosen_final_fallback`, `proceed`); `walk_forward_b(df15, workers=1) -> dict`(위 키 + `combo_table`).

명세 해석 1~3을 구현한다. A의 구간 함수와 지표는 import하고, A의 전역 그리드에 묶인 plateau 선택과 구간 평가만 B 그리드로 다시 쓴다(규칙은 문자 그대로 같다). 그리드는 프레임을 한 번만 만들고(15분 지표는 조합과 무관), 기준 구간은 `zz_k`마다 한 번만 계산한다. 다중 처리 도우미 이름은 `_pool_init`/`_pool_run`이다(A의 정적 검사가 `_init_worker`·`_map_tasks` 이름을 금지한다).

- [ ] **Step 1: 실패하는 테스트 작성**

2024 거부, 백테스트 = 엔진 실행, 2단계 그리드에서 plateau가 '자신의 Calmar 최대'로 퇴화함(동점이면 첫 조합), 17개 적격 조건, 구간 선택·이어 붙이기·`proceed`(음수 표본 외, fallback), 그리드 32회(1개와 2개 작업자가 같은 결과), 워크포워드가 2024를 자르고 A의 10개 구간을 쓰는지를 확인한다.

**파일 `tests/stratb/test_research.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.stratb import research
from ewbot.stratb.config import BConfig, StratBParams, grid_combos
from ewbot.stratb.engine import BResult, run_engine
from ewbot.stratb.research import backtest_b, evaluate_folds_b, pick_plateau_b, run_grid_b, walk_forward_b
from ewbot.stratb.signals import build_frame, generate_signals
from tests.stratb.helpers import SIGNAL_BAR, T0, long_setup_df, random_df

H1 = 3600
COMBOS = grid_combos()


def _result(points, trades=()):
    return BResult(tuple(trades), tuple(EquityPoint(t, e) for t, e in points), (), (), (), (), None, ())


def _trade(ts):
    return Trade(ts, ts + H1, "B", 1, 1, 100.0, 101.0, 99.0, 101.0, 102.0, 1.0, 0.0, 0.0, 1.0, "TP2", {})


def test_research_runs_refuse_2024_bars():
    df = random_df(400, 1, t0=HOLDOUT_START - 200 * 900)
    with pytest.raises(ValueError):
        backtest_b(df, BConfig())
    with pytest.raises(ValueError):
        backtest_b(df, BConfig(), end_ts=HOLDOUT_START + 900)
    with pytest.raises(ValueError):
        run_grid_b(df, int(df["ts"].iloc[0]), HOLDOUT_START + 900)
    assert backtest_b(df, BConfig(), end_ts=HOLDOUT_START).equity[-1].ts < HOLDOUT_START
    assert backtest_b(df, BConfig(), include_holdout=True).equity[-1].ts >= HOLDOUT_START


def test_a_backtest_is_the_engine_on_the_rule_signals():
    df = long_setup_df()
    p = StratBParams(zz_k=2.0)
    res = backtest_b(df, BConfig(params=p))
    f = build_frame(df, p)
    assert res == run_engine(f, generate_signals(f, p), BConfig(params=p))
    assert res.signals[0].i == SIGNAL_BAR and res.trades[0].entry_ts == int(f.ts[SIGNAL_BAR + 1])


def test_on_a_two_level_grid_the_plateau_is_the_best_own_calmar():
    cal = {c: float(k) for k, c in enumerate(COMBOS)}
    assert pick_plateau_b(cal, {c: 12 for c in COMBOS}) == COMBOS[-1]
    tie = {c: 1.0 for c in COMBOS}
    assert pick_plateau_b(tie, {c: 12 for c in COMBOS}) == COMBOS[0]  # ties: smallest grid index


def test_the_plateau_needs_17_eligible_combos_with_12_trades_and_a_finite_calmar():
    cal = {c: float(k) for k, c in enumerate(COMBOS)}
    few = {c: (12 if k < 16 else 11) for k, c in enumerate(COMBOS)}
    with pytest.raises(ValueError):
        pick_plateau_b(cal, few)
    few[COMBOS[20]] = 12
    assert pick_plateau_b(cal, few) == COMBOS[20]
    cal[COMBOS[20]] = float("inf")
    with pytest.raises(ValueError):
        pick_plateau_b(cal, few)


def test_folds_pick_on_training_stitch_the_test_windows_and_decide_proceed():
    t0 = 1_600_000_000 // H1 * H1
    folds = [(t0, t0 + 10 * H1, t0 + 10 * H1, t0 + 20 * H1), (t0 + 10 * H1, t0 + 20 * H1, t0 + 20 * H1, t0 + 30 * H1)]
    flat = [(t0 + k * H1, 100.0) for k in range(30)]
    win = [(t0 + k * H1, 100.0 + k + (-3.0 if k % 5 == 4 else 0.0)) for k in range(30)]
    results = {c: _result(flat) for c in COMBOS}
    results[COMBOS[7]] = _result(win, [_trade(t0 + 2 * H1), _trade(t0 + 22 * H1)])
    out = evaluate_folds_b(results, folds, 1_000.0, min_trades=0)
    assert [f["chosen"] for f in out["folds"]] == [COMBOS[7], COMBOS[7]]
    assert out["chosen_final"] == COMBOS[7] and out["chosen_final_from_fold"] == 1 and not out["chosen_final_fallback"]
    assert out["stitched_equity"][-1][1] == pytest.approx(1_000.0 * win[29][1] / win[9][1])
    assert out["proceed"] is True and len(out["oos_trades"]) == 1
    turn = [(t, e if k < 10 else 109.0 - 2.0 * (k - 9)) for k, (t, e) in enumerate(win)]  # wins, then loses
    results[COMBOS[7]] = _result(turn, [_trade(t0 + 2 * H1)])
    out = evaluate_folds_b(results, folds, 1_000.0, min_trades=0)
    assert [f["chosen"] for f in out["folds"]] == [COMBOS[7], COMBOS[0]]
    assert out["stitched_metrics"]["total_return"] < 0 and out["proceed"] is False
    first = [_trade(t0 + 2 * H1)]  # trades only in fold 1's training window: fold 2 has no selectable combo
    results = {c: _result(win if k == 7 else flat, first if k < 17 else ()) for k, c in enumerate(COMBOS)}
    out = evaluate_folds_b(results, folds, 1_000.0, min_trades=1)
    assert [f["chosen"] for f in out["folds"]] == [COMBOS[7], None]
    assert out["chosen_final"] == COMBOS[7] and out["chosen_final_fallback"] is True and out["proceed"] is False


def test_the_grid_runs_every_combo_once_with_the_breakers_off():
    df = random_df(3000, 3)
    start = int(df["ts"].iloc[400])
    out = run_grid_b(df, start, int(df["ts"].iloc[-1]) + 900, workers=1)
    assert list(out) == COMBOS and all(r.equity[0].ts >= start for r in out.values())
    assert any(r.trades for r in out.values())
    assert run_grid_b(df, start, int(df["ts"].iloc[-1]) + 900, workers=2) == out


def test_walk_forward_cuts_2024_and_uses_strategy_a_folds(monkeypatch):
    seen = {}

    def fake_grid(df, start, end, workers):
        seen.update(max_ts=int(df["ts"].max()), start=start, end=end)
        return {c: _result([(start, 10_000.0), (end - H1, 10_000.0)]) for c in COMBOS}

    monkeypatch.setattr(research, "run_grid_b", fake_grid)
    df = pd.concat([random_df(50, 1, t0=HOLDOUT_START - 50 * 900), random_df(50, 2, t0=HOLDOUT_START)])
    out = walk_forward_b(df, workers=1)
    assert seen == {"max_ts": HOLDOUT_START - 900, "start": 1_483_228_800, "end": HOLDOUT_START}
    assert len(out["folds"]) == 10 and out["folds"][0]["test"][0] == 1_546_300_800  # 2019-01-01
    assert out["chosen_final"] is None and out["proceed"] is False and len(out["combo_table"]) == 32
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_research.py -q
```

예상: `cannot import name 'research' from 'ewbot.stratb'`
예상: `1 error`

`ewbot.stratb.research`가 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

연구 모듈을 만든다.

**파일 `src/ewbot/stratb/research.py`** (새 파일, 전체 내용)

```python
"""Strategy-B research runs (Plan 4 spec 6.2-6.3): one backtest, the 32-combo grid, the walk-forward.

The walk-forward uses strategy A's rules (ewbot.backtest.walkforward, read 2026-10-07):
  - windows: A's folds() - 24-month training, 6-month test, rolling by 6 months, tests 2019-01 .. 2023-12
    (10 folds), all inside [WF_START, WF_END) = [2017-01-01, 2024-01-01);
  - every combo runs ONCE over that span with the circuit breakers disabled; the folds slice the runs;
  - eligible in a training window: >= MIN_TRAIN_TRADES (12) trades and a finite Calmar (A's metrics);
  - score: mean Calmar over the combo's grid neighbours (index distance <= 1 on every axis, itself
    included), eligible ones only; selectable only when >= MIN_ELIGIBLE_NEIGHBOUR_FRAC (50%) of its
    in-grid neighbours are eligible; highest score, ties -> higher own Calmar -> smallest grid index;
  - stitched out-of-sample curve: each fold's test-window hourly returns of its pick, compounded;
  - chosen_final: the pick of the last fold that picked (chosen_final_fallback when not the last fold).
A's pick_plateau reads A's own GRID, so the identical rule is implemented here over GRID_B. With two
values per axis every combo neighbours every other: the score is the same for all, the pick is the
highest own Calmar, and it needs >= 17 of the 32 combos eligible. Spec 6.3: a negative stitched
out-of-sample return ends strategy B (proceed = False); so does a fallback pick (A's CLI refuses it too)."""

from __future__ import annotations

import dataclasses
import math
import multiprocessing as mp
import sys
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import (
    HOLDOUT_START,
    MIN_ELIGIBLE_NEIGHBOUR_FRAC,
    MIN_TRAIN_TRADES,
    WF_START,
    folds,
    oos_returns,
)
from ewbot.engine.loop import EquityPoint
from ewbot.stratb.config import GRID_B, BConfig, Combo, StratBParams, combo_to_json, config_for, grid_combos
from ewbot.stratb.engine import BResult, run_engine
from ewbot.stratb.signals import Frame15, RefLeg, build_frame, generate_signals, reference_legs
from ewbot.types import H1


def refuse_holdout_bars(df15: pd.DataFrame, end_ts: int | None, include_holdout: bool = False) -> None:
    """ValueError when a run over df15 up to end_ts (exclusive; None = every bar) would see a bar at or
    after HOLDOUT_START, unless include_holdout (only the B holdout and the paper-B runner pass it)."""
    if include_holdout:
        return
    if end_ts is None:
        if len(df15) and int(df15["ts"].max()) >= HOLDOUT_START:
            raise ValueError("df15 has bars from 2024-01-01 on and no end_ts: research runs end by HOLDOUT_START")
    elif end_ts > HOLDOUT_START:
        raise ValueError(f"end_ts {end_ts} reaches into the holdout (HOLDOUT_START = {HOLDOUT_START})")


def backtest_b(
    df15: pd.DataFrame,
    cfg: BConfig,
    start_ts: int | None = None,
    end_ts: int | None = None,
    *,
    include_holdout: bool = False,
) -> BResult:
    """One strategy-B run over the bars before end_ts; entries from start_ts."""
    refuse_holdout_bars(df15, end_ts, include_holdout)
    d = df15 if end_ts is None else df15[df15["ts"] < end_ts]
    f = build_frame(d.reset_index(drop=True), cfg.params)
    return run_engine(f, generate_signals(f, cfg.params), cfg, start_ts)


# ----------------------------------------------------------------------------- grid

_POOL: dict[str, Any] = {}  # per process: the frame, the start and the legs per zz_k


def _pool_init(frame: Frame15, start_ts: int) -> None:
    _POOL.clear()
    _POOL.update(frame=frame, start=start_ts, legs={})


def _pool_run(combo: Combo) -> tuple[Combo, BResult]:
    cfg = config_for(combo, breakers_enabled=False)
    f: Frame15 = _POOL["frame"]
    legs: dict[float, list[RefLeg]] = _POOL["legs"]
    if cfg.params.zz_k not in legs:
        legs[cfg.params.zz_k] = reference_legs(f, cfg.params.zz_k, cfg.params)
    sigs = generate_signals(f, cfg.params, legs[cfg.params.zz_k])
    return combo, run_engine(f, sigs, cfg, _POOL["start"])


def run_grid_b(df15: pd.DataFrame, start_ts: int, end_ts: int, workers: int = 1) -> dict[Combo, BResult]:
    """Every grid combo once over the bars before end_ts (refused past HOLDOUT_START), breakers off."""
    refuse_holdout_bars(df15, end_ts)
    frame = build_frame(df15[df15["ts"] < end_ts].reset_index(drop=True), StratBParams())
    combos = grid_combos()
    out: dict[Combo, BResult] = {}
    if workers <= 1:
        _pool_init(frame, start_ts)
        for n, c in enumerate(combos, 1):
            out[c] = _pool_run(c)[1]
            print(f"[grid-b] {n}/{len(combos)} {combo_to_json(c)}", file=sys.stderr, flush=True)
    else:
        with mp.Pool(min(workers, len(combos)), initializer=_pool_init, initargs=(frame, start_ts)) as pool:
            for n, (c, res) in enumerate(pool.imap_unordered(_pool_run, combos), 1):
                out[c] = res
                print(f"[grid-b] {n}/{len(combos)} {combo_to_json(c)}", file=sys.stderr, flush=True)
    return {c: out[c] for c in combos}


# ----------------------------------------------------------------------------- plateau


def _index(combo: Combo) -> tuple[int, ...]:
    return tuple(axis.index(v) for axis, v in zip(GRID_B.values(), combo))


def _n_neighbours(index: tuple[int, ...]) -> int:
    n = 1
    for i, axis in zip(index, GRID_B.values()):
        n *= min(i + 1, len(axis) - 1) - max(i - 1, 0) + 1
    return n - 1


def _tie(x: float) -> float:
    return float(f"{x:.12g}")


def pick_plateau_b(
    calmar: Mapping[Combo, float], n_trades: Mapping[Combo, int], min_trades: int = MIN_TRAIN_TRADES
) -> Combo:
    """A's pick_plateau rule over GRID_B (see the module docstring). ValueError if nothing is selectable."""
    valid = {c: float(v) for c, v in calmar.items() if n_trades.get(c, 0) >= min_trades and math.isfinite(v)}
    if not valid:
        raise ValueError(f"no grid combo has >= {min_trades} trades in the training window")
    idx = {c: _index(c) for c in valid}
    best: tuple[tuple[float, float], Combo] | None = None
    for c in sorted(valid, key=lambda k: idx[k]):
        near = [o for o in valid if max(abs(a - b) for a, b in zip(idx[c], idx[o])) <= 1]
        if len(near) - 1 < MIN_ELIGIBLE_NEIGHBOUR_FRAC * _n_neighbours(idx[c]):
            continue
        key = (_tie(float(np.mean([valid[o] for o in near]))), _tie(valid[c]))
        if best is None or key > best[0]:
            best = (key, c)
    if best is None:
        raise ValueError(
            f"no combo with >= {min_trades} trades has {MIN_ELIGIBLE_NEIGHBOUR_FRAC:.0%} of its neighbours eligible"
        )
    return best[1]


# ----------------------------------------------------------------------------- folds


def evaluate_folds_b(
    results: Mapping[Combo, BResult],
    fold_list: Sequence[tuple[int, int, int, int]],
    equity0: float,
    min_trades: int = MIN_TRAIN_TRADES,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    stitched: list[EquityPoint] = []
    oos_trades = []
    cur = float(equity0)
    if fold_list:
        stitched.append(EquityPoint(ts=fold_list[0][2] - H1, equity=cur))
    chosen_final: Combo | None = None
    final_fold: int | None = None
    for k, (tr0, tr1, te0, te1) in enumerate(fold_list):
        cal: dict[Combo, float] = {}
        ntr: dict[Combo, int] = {}
        for c, res in results.items():
            m = compute_metrics(res.equity, res.trades, tr0, tr1)
            cal[c], ntr[c] = m.calmar, m.n_trades
        try:
            chosen: Combo | None = pick_plateau_b(cal, ntr, min_trades)
        except ValueError:
            chosen = None
        row: dict[str, Any] = {"train": (tr0, tr1), "test": (te0, te1), "chosen": chosen,
                               "train_calmar": cal[chosen] if chosen is not None else None, "oos": None}
        if chosen is None:
            stitched.extend(EquityPoint(ts=t, equity=cur) for t in range(te0, te1, H1))
        else:
            res = results[chosen]
            for t, r in oos_returns(res, te0, te1):
                cur *= 1.0 + r
                stitched.append(EquityPoint(ts=t, equity=cur))
            oos_trades.extend(t for t in res.trades if te0 <= t.entry_ts < te1)
            row["oos"] = dataclasses.asdict(compute_metrics(res.equity, res.trades, te0, te1))
            chosen_final, final_fold = chosen, k
        rows.append(row)
    fallback = final_fold is not None and final_fold != len(fold_list) - 1
    metrics = dataclasses.asdict(compute_metrics(stitched, oos_trades))
    proceed = chosen_final is not None and not fallback and metrics["total_return"] >= 0.0
    return {
        "keys": list(GRID_B),
        "folds": rows,
        "stitched_equity": [(p.ts, p.equity) for p in stitched],
        "stitched_metrics": metrics,
        "oos_trades": oos_trades,
        "chosen_final": chosen_final,
        "chosen_final_from_fold": final_fold,
        "chosen_final_fallback": fallback,
        "proceed": proceed,
    }


def walk_forward_b(df15: pd.DataFrame, workers: int = 1) -> dict[str, Any]:
    """The full walk-forward over [2017-01-01, 2024-01-01). Bars at/after 2024-01-01 are dropped here."""
    start, end = utc_ts(WF_START), HOLDOUT_START
    df15 = df15[df15["ts"] < end].reset_index(drop=True)
    results = run_grid_b(df15, start, end, workers)
    out = evaluate_folds_b(results, folds(), BConfig().initial_equity)
    full = {c: compute_metrics(r.equity, r.trades, start, end) for c, r in results.items()}
    out["combo_table"] = [
        {"combo": combo_to_json(c), "calmar": m.calmar, "sharpe": m.sharpe, "n_trades": m.n_trades,
         "total_return": m.total_return}
        for c, m in full.items()
    ]
    return out
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_research.py -q
```

예상: `7 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/research.py tests/stratb/test_research.py
git commit -m "feat(stratb): 2024 guard, 32-combo grid and walk-forward with strategy A's windows and plateau rule" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 매수 후 보유와 무작위 진입 대조군

**Files:**
- Create: `src/ewbot/stratb/baselines.py`
- Test: `tests/stratb/test_baselines.py`

**Interfaces:**
- Consumes: Task 1: `BConfig`. Task 2: `resample_1h`. Task 4: `Frame15`, `SignalB`(테스트: `build_frame`). Task 5: `run_engine`. Task 6(테스트): `backtest_b`. A: `ewbot.backtest.benchmarks.buy_hold(df1h, start_ts, end_ts, costs, equity0) -> (list[EquityPoint], list[Trade])`; `ewbot.backtest.metrics.compute_metrics`.
- Produces: `ewbot.stratb.baselines`: `RANDOM_METRICS = ("sharpe", "max_dd", "total_return", "calmar", "n_trades")`; `@dataclass(frozen=True) Shape(direction, stop_frac, tp1_r, tp2_r)`; `trade_shapes(trades) -> list[Shape]`(스냅숏의 `entry_ref` 기준); `buy_hold_b(f, start_ts, end_ts, cfg) -> (list[EquityPoint], list[Trade])`; `random_signals(f, shapes, n, start_ts, end_ts, seed) -> list[SignalB]`; `random_control(f, cfg, b_trades, start_ts, end_ts, runs=1000, seed=0, workers=1) -> dict`(`{"runs", "n_signals", "seed", "dist": {지표: {"mean", "p05", "p50", "p95"}}}`; B 거래가 없으면 `runs = 0`, `dist = {}`).

명세 해석 10을 구현한다. 매수 후 보유는 A의 함수를 15분 프레임에서 만든 1시간봉에 그대로 쓴다(현물, 전 자산, 양쪽 수수료·슬리피지). 대조군은 B와 같은 엔진·설정으로 돌고 진입 시각만 무작위다. 시드가 고정되어 있어 작업자 수와 관계없이 결과가 같다.

- [ ] **Step 1: 실패하는 테스트 작성**

A의 매수 후 보유와 같은 결과, 스냅숏 기준 거래 모양, 무작위 진입의 시드 재현·적격 봉·모양 1회씩 사용, 대조군의 분포 키와 결정성(1개와 2개 작업자), B 거래가 없을 때를 확인한다.

**파일 `tests/stratb/test_baselines.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import pandas as pd
import pytest

from ewbot.backtest.benchmarks import buy_hold
from ewbot.engine.loop import Trade
from ewbot.stratb.baselines import buy_hold_b, random_control, random_signals, trade_shapes
from ewbot.stratb.config import BConfig, StratBParams
from ewbot.stratb.data15 import resample_1h
from ewbot.stratb.research import backtest_b
from ewbot.stratb.signals import build_frame
from tests.stratb.helpers import random_df

M15 = 900


def _trade(d, ref, stop, tp1, tp2, ts=0):
    return Trade(ts, ts + 900, "B", d, 3, ref, ref, stop, tp1, tp2, 0.0, 0.0, 0.0, 0.0, "TP2", {"entry_ref": ref})


def test_buy_hold_is_strategy_a_buy_hold_on_the_complete_hours():
    df = random_df(1000, 1)
    f = build_frame(df)
    start, end = int(df["ts"].iloc[100]) // 3600 * 3600, int(df["ts"].iloc[900]) // 3600 * 3600
    eq, trades = buy_hold_b(f, start, end, BConfig())
    ref_eq, ref_trades = buy_hold(resample_1h(df), start, end, BConfig().costs, 10_000.0)
    assert eq == ref_eq and trades == ref_trades and trades[0].setup == "BH"


def test_shapes_come_from_the_signal_reference_price():
    (a, b) = trade_shapes([_trade(1, 100.0, 98.0, 103.0, 105.0), _trade(-1, 50.0, 51.0, 48.5, 47.0)])
    assert (a.direction, a.stop_frac, a.tp1_r, a.tp2_r) == (1, 0.02, 1.5, 2.5)
    assert (b.direction, b.stop_frac, b.tp1_r, b.tp2_r) == (-1, 0.02, 1.5, 3.0)


def test_random_entries_are_seeded_eligible_and_use_each_shape_once():
    df = random_df(2000, 2).drop(index=[500]).reset_index(drop=True)
    f = build_frame(df)
    shapes = trade_shapes([_trade(1, 100.0, 98.0, 102.0, 104.0), _trade(-1, 100.0, 103.0, 97.0, 94.0),
                           _trade(1, 100.0, 99.0, 101.5, 103.0)])
    start, end = int(f.ts[200]), int(f.ts[1999])
    a = random_signals(f, shapes, 3, start, end, seed=5)
    assert a == random_signals(f, shapes, 3, start, end, seed=5) != random_signals(f, shapes, 3, start, end, seed=6)
    assert len(a) == 3 and sorted(s.direction for s in a) == [-1, 1, 1]
    for s in a:
        assert start <= s.ts < end and s.i < len(f) - 1 and not f.synthetic[s.i] and not f.synthetic[s.i + 1]
        assert s.entry_ref == f.close[s.i] and s.direction * (s.entry_ref - s.stop) > 0
    many = random_signals(f, shapes, 1500, start, end, seed=1)
    assert len({s.i for s in many}) == len(many) and all(not f.synthetic[s.i + 1] for s in many)


def test_the_random_control_reuses_b_exits_and_sizing_and_is_deterministic():
    df = random_df(6000, 3)
    p = StratBParams(zz_k=3.0, zone=(0.5, 0.786))
    cfg = BConfig(params=p)
    res = backtest_b(df, cfg)
    assert res.trades
    f = build_frame(df, p)
    start, end = int(df["ts"].iloc[0]), int(df["ts"].iloc[-1]) + M15
    one = random_control(f, cfg, res.trades, start, end, runs=6, seed=10, workers=1)
    assert one["runs"] == 6 and one["n_signals"] == len(res.trades)
    assert set(one["dist"]) == {"sharpe", "max_dd", "total_return", "calmar", "n_trades"}
    assert set(one["dist"]["sharpe"]) == {"mean", "p05", "p50", "p95"}
    assert 0 <= one["dist"]["n_trades"]["p95"] <= len(res.trades)
    assert random_control(f, cfg, res.trades, start, end, runs=6, seed=10, workers=2) == one


def test_no_b_trade_means_no_random_run():
    f = build_frame(random_df(500, 1))
    out = random_control(f, BConfig(), [], int(f.ts[0]), int(f.ts[-1]), runs=10)
    assert out == {"runs": 0, "n_signals": 0, "seed": 0, "dist": {}}
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_baselines.py -q
```

예상: `No module named 'ewbot.stratb.baselines'`
예상: `1 error`

`ewbot.stratb.baselines`가 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

기준선 모듈을 만든다.

**파일 `src/ewbot/stratb/baselines.py`** (새 파일, 전체 내용)

```python
"""Strategy-B baselines (Plan 4 spec 6.5 item 1): buy & hold and the random-entry control.

Buy & hold is A's backtest.benchmarks.buy_hold on 1h bars built from the 15m frame (spot, whole equity,
one fee and one slippage each way, hourly marks). The random control keeps everything of strategy B but
the entry times: the same window, as many entries as B's trades in it, B's own trade shapes (direction,
stop distance as a fraction of the reference price, TP1 and TP2 in R) assigned in a seeded random order,
and the same engine (exits, 48 h time stop, remainder rule, 0.5% sizing, 3x cap, breakers, costs).
Entries land on uniformly drawn 15m bars of the window (not the last bar, no gap-filled bar or fill bar);
one that comes while a position is open is skipped, as for B."""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ewbot.backtest.benchmarks import buy_hold
from ewbot.backtest.metrics import compute_metrics
from ewbot.engine.loop import EquityPoint, Trade
from ewbot.stratb.config import BConfig
from ewbot.stratb.data15 import resample_1h
from ewbot.stratb.engine import run_engine
from ewbot.stratb.signals import Frame15, SignalB

RANDOM_METRICS = ("sharpe", "max_dd", "total_return", "calmar", "n_trades")


@dataclass(frozen=True, slots=True)
class Shape:
    direction: int
    stop_frac: float  # |entry_ref - stop| / entry_ref
    tp1_r: float  # |tp1 - entry_ref| / |entry_ref - stop|
    tp2_r: float


def trade_shapes(trades: Sequence[Trade]) -> list[Shape]:
    """Shapes of B's trades, from the signal's reference price kept in the trade snapshot."""
    out: list[Shape] = []
    for t in trades:
        ref = float(t.snapshot["entry_ref"])
        dist = abs(ref - t.stop0)
        if ref > 0.0 and dist > 0.0 and t.tp2 is not None:
            out.append(Shape(t.direction, dist / ref, abs(t.tp1 - ref) / dist, abs(t.tp2 - ref) / dist))
    return out


def buy_hold_b(f: Frame15, start_ts: int, end_ts: int, cfg: BConfig) -> tuple[list[EquityPoint], list[Trade]]:
    df = pd.DataFrame({"ts": f.ts, "open": f.open, "high": f.high, "low": f.low, "close": f.close, "volume": 0.0,
                       "synthetic": f.synthetic})
    return buy_hold(resample_1h(df), start_ts, end_ts, cfg.costs, cfg.initial_equity)


def random_signals(
    f: Frame15, shapes: Sequence[Shape], n: int, start_ts: int, end_ts: int, seed: int
) -> list[SignalB]:
    """n entries (at most one per bar) on eligible bars of [start_ts, end_ts), shapes in a seeded order."""
    if n <= 0 or not shapes:
        return []
    last = len(f) - 1
    idx = np.flatnonzero((f.ts >= start_ts) & (f.ts < end_ts))
    idx = idx[idx < last]
    idx = idx[~f.synthetic[idx] & ~f.synthetic[idx + 1]]
    k = min(int(n), int(idx.size))
    if k == 0:
        return []
    rng = np.random.default_rng(seed)
    times = np.sort(rng.choice(idx, size=k, replace=False))
    order = rng.permutation(len(shapes))
    out: list[SignalB] = []
    for m, i in enumerate(times.tolist()):
        s = shapes[int(order[m % len(shapes)])]
        ref = float(f.close[i])
        dist = s.stop_frac * ref
        d = s.direction
        out.append(SignalB(i=i, ts=int(f.ts[i]), direction=d, entry_ref=ref, stop=ref - d * dist,
                           tp1=ref + d * s.tp1_r * dist, tp2=ref + d * s.tp2_r * dist, reason="random entry"))
    return out


_POOL: dict[str, Any] = {}


def _pool_init(f: Frame15, cfg: BConfig, shapes: list[Shape], n: int, start_ts: int, end_ts: int) -> None:
    _POOL.clear()
    _POOL.update(f=f, cfg=cfg, shapes=shapes, n=n, start=start_ts, end=end_ts)


def _pool_run(seed: int) -> dict[str, float]:
    s = _POOL
    sigs = random_signals(s["f"], s["shapes"], s["n"], s["start"], s["end"], seed)
    res = run_engine(s["f"], sigs, s["cfg"], s["start"])
    m = compute_metrics(res.equity, res.trades, s["start"], s["end"])
    return {k: float(getattr(m, k)) for k in RANDOM_METRICS}


def _dist(values: Sequence[float]) -> dict[str, float]:
    a = np.asarray(values, dtype=np.float64)
    return {"mean": float(a.mean()), "p05": float(np.percentile(a, 5)), "p50": float(np.percentile(a, 50)),
            "p95": float(np.percentile(a, 95))}


def random_control(
    f: Frame15,
    cfg: BConfig,
    b_trades: Sequence[Trade],
    start_ts: int,
    end_ts: int,
    runs: int = 1000,
    seed: int = 0,
    workers: int = 1,
) -> dict[str, Any]:
    """`runs` random-entry runs (seeds seed .. seed + runs - 1) over [start_ts, end_ts). dist[metric] =
    {mean, p05, p50, p95}; no runs when B made no trade in the window (nothing to imitate)."""
    mine = [t for t in b_trades if start_ts <= t.entry_ts < end_ts]
    shapes = trade_shapes(mine)
    n = len(mine)
    out: dict[str, Any] = {"runs": 0, "n_signals": n, "seed": seed, "dist": {}}
    if runs <= 0 or n == 0 or not shapes:
        return out
    seeds = list(range(seed, seed + runs))
    args = (f, cfg, shapes, n, start_ts, end_ts)
    if workers <= 1:
        _pool_init(*args)
        rows = [_pool_run(s) for s in seeds]
    else:
        with mp.Pool(min(workers, runs), initializer=_pool_init, initargs=args) as pool:
            rows = pool.map(_pool_run, seeds, chunksize=max(1, runs // (4 * workers)))
    out["runs"] = len(rows)
    out["dist"] = {k: _dist([r[k] for r in rows]) for k in RANDOM_METRICS}
    return out
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_baselines.py -q
```

예상: `5 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/baselines.py tests/stratb/test_baselines.py
git commit -m "feat(stratb): buy & hold and the random-entry control (same exits, sizing and costs, random entry times)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: holdout-b.lock, 1회 홀드아웃, 합격 기준

**Files:**
- Create: `src/ewbot/stratb/holdout.py`
- Test: `tests/stratb/test_holdout.py`

**Interfaces:**
- Consumes: Task 1: `GRID_B`, `M15`, `combo_to_json`, `config_for`, `normalize_combo`, `scale_costs`, `stratb_fingerprint`. Task 2: `DEFAULT_LOCK_PATH`, `LOCK_KIND`, `REPLAY_ANCHOR`, `cache_path`, `load_15m`, `HoldoutGuardError`(테스트). Task 4: `build_frame`, `generate_signals`. Task 5: `run_engine`. Task 6: `backtest_b`. Task 7: `buy_hold_b`, `random_control`. A: `ewbot.backtest.holdout.data_fingerprint(df, start_ts, last_ts) -> str`, `git_state() -> dict`; `ewbot.backtest.metrics.Metrics`, `compute_metrics`; `ewbot.backtest.report.to_jsonable`; 테스트: `tests.paper.helpers.git(repo, *args)`.
- Produces: `ewbot.stratb.holdout`: `LOCK_VERSION = 1`, `RANDOM_RUNS = 1000`, `MAX_DD = 0.15`, `MIN_TRADES = 50`, `COST_MULT = 1.5`, `BOUND`(다이제스트 대상 필드); `class HoldoutBError(RuntimeError)`; `lock_digest_b(lock) -> str`; `read_lock(path) -> dict`(종류·다이제스트 확인); `committed_lock(path) -> dict`(git HEAD의 내용; 커밋 안 됐으면 `HoldoutBError`); `wf_data_record(df_wf) -> dict`; `create_lock(params, df_wf, *, params_source, now_ts, lock_path=DEFAULT_LOCK_PATH, random_runs=1000, random_seed=0) -> dict`; `evaluate_criteria_b(b, bh, rnd, cost) -> dict`(`c1_sharpe_beats_bh_and_random_p95`, `c2_max_dd_le_15pct`, `c3_trades_ge_50`, `c4_cost_x1_5_net_profit`, `passed` 등); `run_holdout_b(*, confirm, cache_dir, lock_path=DEFAULT_LOCK_PATH, workers=1, fingerprint=stratb_fingerprint) -> dict`(`{"lock_path", "result": BResult, "first_run"}`).
  잠금 파일 필드: `kind`, `version`, `keys`, `params`, `config`, `grid`, `holdout_start`, `replay_anchor`, `last_bar_ts`, `random_runs`, `random_seed`, `code_sha256`, `wf_data`{`first_ts`, `last_ts`, `n_bars`, `sha256`}, `sha256`, `params_source`, `locked_at`, `git_head`, `git_dirty`, `attempts`[{`started`, `data_sha256`, `git_head`, `git_dirty`, `completed`?}], `first_run`{`started`, `completed`, `start_ts`, `end_ts`, `last_bar_ts`, `data`{`start_ts`, `last_bar_ts`, `n_bars`, `sha256`}, `metrics`{`B`, `BH`, `B_cost_x1_5`}, `random`, `criteria`}.

명세 6.4~6.5와 해석 11을 구현한다. 잠금은 2024+ 데이터 없이 만들어지고(2024 이전 데이터 해시만 묶음), 커밋된 뒤에야 홀드아웃이 돈다. 홀드아웃은 `REPLAY_ANCHOR`부터 잠금의 마지막 봉까지를 손실 한도를 켠 채 다시 계산하고 2024-01-01부터 거래한다. 이어서 매수 후 보유, 무작위 대조군(잠금의 횟수·시드), 비용 1.5배 재실행을 같은 프레임에서 돌린다. 합격 기준 4개를 모두 `first_run.criteria`에 기록한다. 테스트의 합성 세계(`LONG_SETUP` 130번 반복, 2023-10-01부터)는 232봉마다 롱 하나가 나와 홀드아웃 구간에 62건, 네 기준 모두 합격이다(무작위 대조군은 테스트 속도를 위해 4회).

- [ ] **Step 1: 실패하는 테스트 작성**

잠금 전 2024 읽기 거부, 잠금 내용과 1회 작성, 그리드 밖·2024 데이터·이른 시각 거부, 필드 수정 감지, `--confirm`과 커밋 요구, B 코드 변경·2024 이전 데이터 변경 거부, 1회 실행과 기록(62건, 합격), 재실행 거부, 끝나지 않은 시도의 데이터 일치, 기준 하나하나의 실패를 확인한다.

**파일 `tests/stratb/test_holdout.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json

import pandas as pd
import pytest

from ewbot.backtest.metrics import Metrics
from ewbot.stratb.data15 import REPLAY_ANCHOR, HoldoutGuardError, cache_path, load_15m
from ewbot.stratb.fingerprint import stratb_fingerprint
from ewbot.stratb.holdout import (
    HoldoutBError,
    create_lock,
    evaluate_criteria_b,
    lock_digest_b,
    read_lock,
    run_holdout_b,
)
from tests.paper.helpers import git
from tests.stratb.helpers import LONG_SETUP, LONG_START, frame_df, path

HS = 1_704_067_200
NOW = HS + 150 * 86_400 + 1_234
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    cache = tmp_path / "cache"
    cache.mkdir()
    df = frame_df(path(LONG_START, LONG_SETUP * 130), REPLAY_ANCHOR)  # one long setup per 232 bars, to 2024-08
    df.to_parquet(cache_path(cache), index=False)
    return repo, cache, repo / "holdout-b.lock"


def _lock(world, **kw):
    _, cache, lock_path = world
    wf = load_15m(cache_path(cache), lock_path=lock_path)
    args = dict(params_source="test", now_ts=NOW, lock_path=lock_path, random_runs=4)
    args.update(kw)
    return create_lock(COMBO, wf, **args)


def _commit(world):
    repo, _, lock_path = world
    git(repo, "add", lock_path.name)
    git(repo, "commit", "-q", "-m", "lock")


def test_2024_bars_are_unreadable_until_the_lock_exists(world):
    _, cache, lock_path = world
    with pytest.raises(HoldoutGuardError):
        load_15m(cache_path(cache), include_holdout=True, lock_path=lock_path)
    _lock(world)
    assert int(load_15m(cache_path(cache), include_holdout=True, lock_path=lock_path)["ts"].max()) > HS


def test_the_lock_binds_params_code_data_and_window_and_is_written_once(world):
    lock = _lock(world)
    assert lock == read_lock(world[2])
    assert lock["params"] == COMBO and lock["sha256"] == lock_digest_b(lock)
    assert lock["last_bar_ts"] == NOW // 3600 * 3600 - 900 and (lock["holdout_start"], lock["replay_anchor"]) == (HS, REPLAY_ANCHOR)
    assert lock["code_sha256"] == stratb_fingerprint() and lock["random_runs"] == 4 and lock["first_run"] is None
    assert lock["wf_data"]["last_ts"] == HS - 900 and lock["config"]["risk"]["risk_pct"] == 0.005
    with pytest.raises(HoldoutBError, match="written once"):
        _lock(world)


def test_the_lock_refuses_off_grid_params_2024_walk_forward_data_and_an_early_clock(world):
    _, cache, lock_path = world
    wf = load_15m(cache_path(cache), lock_path=lock_path)
    with pytest.raises(HoldoutBError):
        create_lock([2.5, [0.382, 0.618], 1.0, 1.272, "breakeven"], wf, params_source="t", now_ts=NOW, lock_path=lock_path)
    full = pd.read_parquet(cache_path(cache))
    with pytest.raises(HoldoutBError, match="before 2024"):
        create_lock(COMBO, full, params_source="t", now_ts=NOW, lock_path=lock_path)
    with pytest.raises(HoldoutBError, match="after 2024"):
        create_lock(COMBO, wf, params_source="t", now_ts=HS + 600, lock_path=lock_path)
    assert not lock_path.exists()


def test_an_edited_bound_field_breaks_the_lock(world):
    _lock(world)
    lock = json.loads(world[2].read_text())
    lock["params"][2] = 1.5
    world[2].write_text(json.dumps(lock))
    with pytest.raises(HoldoutBError, match="digest"):
        read_lock(world[2])


def test_the_holdout_needs_confirm_and_the_committed_lock(world):
    _, cache, lock_path = world
    _lock(world)
    with pytest.raises(HoldoutBError, match="single-use"):
        run_holdout_b(confirm=False, cache_dir=cache, lock_path=lock_path)
    with pytest.raises(HoldoutBError, match="not committed"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_a_b_code_change_or_other_walk_forward_bars_are_refused(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    with pytest.raises(HoldoutBError, match="code changed"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path, fingerprint=lambda: "0" * 64)
    df = pd.read_parquet(cache_path(cache))
    df.loc[5, "close"] += 1.0
    df.to_parquet(cache_path(cache), index=False)
    with pytest.raises(HoldoutBError, match="before 2024-01-01 differ"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_the_holdout_runs_once_and_records_the_criteria(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    out = run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)
    lock = read_lock(lock_path)
    fr = lock["first_run"]
    assert fr == out["first_run"] and fr["end_ts"] == lock["last_bar_ts"] + 900
    assert fr["data"]["start_ts"] == REPLAY_ANCHOR and len(lock["attempts"]) == 1 and lock["attempts"][0]["completed"]
    crit = fr["criteria"]
    assert fr["metrics"]["B"]["n_trades"] == crit["n_trades"] == 62 and fr["random"]["runs"] == 4
    assert crit["passed"] is True  # the synthetic setups win every time: all four criteria hold
    assert all(HS <= t.entry_ts <= lock["last_bar_ts"] + 900 for t in out["result"].trades)
    with pytest.raises(HoldoutBError, match="already run once"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def test_an_unfinished_attempt_must_have_seen_the_same_bars(world):
    _, cache, lock_path = world
    _lock(world)
    _commit(world)
    lock = json.loads(lock_path.read_text())
    lock["attempts"].append({"started": "x", "data_sha256": "f" * 64})
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(HoldoutBError, match="other holdout bars"):
        run_holdout_b(confirm=True, cache_dir=cache, lock_path=lock_path)


def _m(**kw):
    base = dict(total_return=0.2, cagr=0.1, max_dd=0.1, sharpe=2.0, sortino=2.0, calmar=1.0, win_rate=0.5,
                avg_r=0.3, n_trades=60, exposure=0.2)
    base.update(kw)
    return Metrics(**base)


def test_each_criterion_can_fail_the_holdout():
    rnd = {"runs": 1000, "dist": {"sharpe": {"p95": 1.5}}}
    ok = evaluate_criteria_b(_m(), _m(sharpe=1.0), rnd, _m(total_return=0.05))
    assert ok["passed"] and ok["random_sharpe_p95"] == 1.5
    assert not evaluate_criteria_b(_m(sharpe=1.4), _m(sharpe=1.0), rnd, _m())["passed"]  # below random p95
    assert not evaluate_criteria_b(_m(), _m(sharpe=2.5), rnd, _m())["passed"]  # below buy & hold
    assert not evaluate_criteria_b(_m(max_dd=0.151), _m(sharpe=1.0), rnd, _m())["c2_max_dd_le_15pct"]
    assert not evaluate_criteria_b(_m(n_trades=49), _m(sharpe=1.0), rnd, _m())["passed"]
    assert not evaluate_criteria_b(_m(), _m(sharpe=1.0), rnd, _m(total_return=0.0))["passed"]
    assert not evaluate_criteria_b(_m(), _m(sharpe=1.0), {"runs": 0, "dist": {}}, _m())["passed"]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_holdout.py -q
```

예상: `No module named 'ewbot.stratb.holdout'`
예상: `1 error`

`ewbot.stratb.holdout`이 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

홀드아웃 모듈을 만든다.

**파일 `src/ewbot/stratb/holdout.py`** (새 파일, 전체 내용)

```python
"""Strategy-B holdout lock and its one-shot run (Plan 4 spec 6.4-6.5).

create_lock() writes <project root>/holdout-b.lock BEFORE any 2024+ candle can be read (data15 refuses
them until a lock exists): the walk-forward's final combo, the full run config, the grid, the B
fingerprint, the sha256 of the pre-2024 15m bars the walk-forward used, the holdout start, the replay
anchor (2023-10-01), the 1,000-run random control and its seed, and last_bar_ts - the last 15m bar of
the last complete hour at lock time, where the holdout ends (spec 6.4). The user commits it.

run_holdout_b() runs the holdout ONCE after the user's approval (confirm=True) and records the result in
first_run. It refuses (HoldoutBError) unless: the lock's sha256 is the digest of its own bound fields and
equals the committed version's (git HEAD); first_run is empty there and in the file (one completed run,
ever: no rerun, no other combo); the running B fingerprint equals code_sha256 (no B code change after the
lock); the pre-2024 bars hash to wf_data.sha256; the data reaches last_bar_ts; an unfinished earlier
attempt saw the same holdout bars. Each attempt is recorded in the lock before it runs.

Criteria (spec 6.5, all four): Sharpe above buy & hold and above the 95th percentile of the random
control; max drawdown <= 15%; >= 50 trades; total return > 0 with fees and slippage x 1.5."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint, git_state
from ewbot.backtest.metrics import Metrics, compute_metrics
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.stratb.baselines import buy_hold_b, random_control
from ewbot.stratb.config import GRID_B, M15, combo_to_json, config_for, normalize_combo, scale_costs
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH, LOCK_KIND, REPLAY_ANCHOR, cache_path, load_15m
from ewbot.stratb.engine import run_engine
from ewbot.stratb.fingerprint import stratb_fingerprint
from ewbot.stratb.research import backtest_b
from ewbot.stratb.signals import build_frame, generate_signals
from ewbot.types import H1

LOCK_VERSION = 1
RANDOM_RUNS = 1000
MAX_DD = 0.15
MIN_TRADES = 50
COST_MULT = 1.5
BOUND = ("kind", "version", "keys", "params", "config", "grid", "holdout_start", "replay_anchor", "last_bar_ts",
         "random_runs", "random_seed", "code_sha256", "wf_data")


class HoldoutBError(RuntimeError):
    """The strategy-B lock cannot be written, or the holdout may not run."""


def lock_digest_b(lock: dict[str, Any]) -> str:
    """sha256 of the sorted compact JSON of the lock's BOUND fields."""
    blob = json.dumps({k: lock.get(k) for k in BOUND}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write(path: Path, lock: dict[str, Any]) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(to_jsonable(lock), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_lock(path: Path) -> dict[str, Any]:
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HoldoutBError(f"{path} is missing or unreadable: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("kind") != LOCK_KIND:
        raise HoldoutBError(f"{path} is not a strategy-B holdout lock")
    if lock_digest_b(lock) != lock.get("sha256"):
        raise HoldoutBError(f"{path}: sha256 is not the digest of its bound fields (edited by hand?)")
    return lock


def committed_lock(path: Path) -> dict[str, Any]:
    """The version of the lock in git HEAD (the lock must be committed before the holdout runs)."""
    p = Path(path)
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "show", f"HEAD:./{p.name}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HoldoutBError(f"git is not available to check {p}: {exc}") from exc
    if r.returncode != 0:
        raise HoldoutBError(f"{p} is not committed: commit holdout-b.lock before the holdout")
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except ValueError as exc:
        raise HoldoutBError(f"the committed {p.name} is not JSON") from exc


def wf_data_record(df_wf: pd.DataFrame) -> dict[str, Any]:
    if df_wf.empty:
        raise HoldoutBError("no 15m bars before 2024-01-01")
    return {"first_ts": int(df_wf["ts"].iloc[0]), "last_ts": int(df_wf["ts"].iloc[-1]), "n_bars": int(len(df_wf)),
            "sha256": data_fingerprint(df_wf, 0, HOLDOUT_START - 1)}


def create_lock(
    params: Sequence[Any],
    df_wf: pd.DataFrame,
    *,
    params_source: str,
    now_ts: int,
    lock_path: Path = DEFAULT_LOCK_PATH,
    random_runs: int = RANDOM_RUNS,
    random_seed: int = 0,
) -> dict[str, Any]:
    """Write the lock (once). df_wf: the walk-forward's 15m bars, all before 2024-01-01."""
    path = Path(lock_path)
    if path.exists():
        raise HoldoutBError(f"{path} exists: the strategy-B lock is written once")
    try:
        combo = normalize_combo(params)
    except ValueError as exc:
        raise HoldoutBError(str(exc)) from exc
    if len(df_wf) and int(df_wf["ts"].max()) >= HOLDOUT_START:
        raise HoldoutBError("the walk-forward data must end before 2024-01-01")
    last_bar = int(now_ts) // H1 * H1 - M15
    if last_bar < HOLDOUT_START:
        raise HoldoutBError("the lock must be written after 2024-01-01")
    if int(random_runs) < 1:
        raise HoldoutBError("random_runs must be >= 1 (criterion 1 compares with the random control)")
    lock: dict[str, Any] = {
        "kind": LOCK_KIND,
        "version": LOCK_VERSION,
        "keys": list(GRID_B),
        "params": combo_to_json(combo),
        "config": to_jsonable(dataclasses.asdict(config_for(combo))),
        "grid": to_jsonable(GRID_B),
        "holdout_start": HOLDOUT_START,
        "replay_anchor": REPLAY_ANCHOR,
        "last_bar_ts": last_bar,
        "random_runs": int(random_runs),
        "random_seed": int(random_seed),
        "code_sha256": stratb_fingerprint(),
        "wf_data": wf_data_record(df_wf),
    }
    lock.update(sha256=lock_digest_b(lock), params_source=params_source, locked_at=_now(), **git_state(),
                attempts=[], first_run=None)
    _write(path, lock)
    return lock


def evaluate_criteria_b(b: Metrics, bh: Metrics, rnd: dict[str, Any], cost: Metrics) -> dict[str, Any]:
    p95 = (rnd.get("dist") or {}).get("sharpe", {}).get("p95") if rnd.get("runs") else None
    c1 = p95 is not None and b.sharpe > bh.sharpe and b.sharpe > p95
    c2 = b.max_dd <= MAX_DD
    c3 = b.n_trades >= MIN_TRADES
    c4 = cost.total_return > 0.0
    return {
        "sharpe": b.sharpe, "bh_sharpe": bh.sharpe, "random_sharpe_p95": p95, "random_runs": int(rnd.get("runs", 0)),
        "c1_sharpe_beats_bh_and_random_p95": bool(c1),
        "max_dd": b.max_dd, "c2_max_dd_le_15pct": bool(c2),
        "n_trades": int(b.n_trades), "c3_trades_ge_50": bool(c3),
        "cost_x1_5_total_return": cost.total_return, "c4_cost_x1_5_net_profit": bool(c4),
        "passed": bool(c1 and c2 and c3 and c4),
    }


def run_holdout_b(
    *,
    confirm: bool,
    cache_dir: Path,
    lock_path: Path = DEFAULT_LOCK_PATH,
    workers: int = 1,
    fingerprint: Callable[[], str] = stratb_fingerprint,
) -> dict[str, Any]:
    """The one-shot holdout over [2024-01-01, last_bar_ts], recomputed from REPLAY_ANCHOR (2023-10-01)."""
    if not confirm:
        raise HoldoutBError("the holdout is single-use: pass confirm=True (CLI: --confirm) after the user's approval")
    path = Path(lock_path)
    lock = read_lock(path)
    head = committed_lock(path)
    if head.get("sha256") != lock["sha256"]:
        raise HoldoutBError(f"{path} differs from the committed lock: commit the lock as written by `holdout lock`")
    if lock.get("first_run") is not None or head.get("first_run") is not None:
        raise HoldoutBError("the strategy-B holdout has already run once: no rerun, no other combo (spec 6.4)")
    if fingerprint() != lock["code_sha256"]:
        raise HoldoutBError("the strategy-B code changed after the lock (B fingerprint differs): refusing")
    df = load_15m(cache_path(cache_dir), include_holdout=True, lock_path=path)
    if wf_data_record(df[df["ts"] < HOLDOUT_START])["sha256"] != lock["wf_data"]["sha256"]:
        raise HoldoutBError("the 15m bars before 2024-01-01 differ from the ones the walk-forward used")
    last = int(lock["last_bar_ts"])
    if df.empty or int(df["ts"].iloc[-1]) < last:
        raise HoldoutBError(f"the cache ends before the lock's last bar {last}: run `ewbot stratb fetch` first")
    win = df[(df["ts"] >= REPLAY_ANCHOR) & (df["ts"] <= last)].reset_index(drop=True)
    data = {"start_ts": REPLAY_ANCHOR, "last_bar_ts": last, "n_bars": int(len(win)),
            "sha256": data_fingerprint(win, REPLAY_ANCHOR, last)}
    for a in lock["attempts"]:
        if a["data_sha256"] != data["sha256"]:
            raise HoldoutBError("an earlier unfinished attempt saw other holdout bars: refusing (inspect by hand)")
    attempt = {"started": _now(), "data_sha256": data["sha256"], **git_state()}
    lock["attempts"].append(attempt)
    _write(path, lock)

    cfg = config_for(normalize_combo(lock["params"]))
    end = last + M15
    res = backtest_b(win, cfg, HOLDOUT_START, end, include_holdout=True)
    f = build_frame(win, cfg.params)
    b = compute_metrics(res.equity, res.trades, HOLDOUT_START, end)
    bh_eq, bh_trades = buy_hold_b(f, HOLDOUT_START, end, cfg)
    bh = compute_metrics(bh_eq, bh_trades, HOLDOUT_START, end)
    rnd = random_control(f, cfg, res.trades, HOLDOUT_START, end, runs=int(lock["random_runs"]),
                         seed=int(lock["random_seed"]), workers=workers)
    dear = dataclasses.replace(cfg, costs=scale_costs(cfg.costs, COST_MULT))
    res_dear = run_engine(f, generate_signals(f, cfg.params), dear, HOLDOUT_START)
    cost = compute_metrics(res_dear.equity, res_dear.trades, HOLDOUT_START, end)
    criteria = evaluate_criteria_b(b, bh, rnd, cost)
    attempt["completed"] = _now()
    lock["first_run"] = to_jsonable({
        "started": attempt["started"], "completed": attempt["completed"], "start_ts": HOLDOUT_START, "end_ts": end,
        "last_bar_ts": last, "data": data,
        "metrics": {"B": dataclasses.asdict(b), "BH": dataclasses.asdict(bh), "B_cost_x1_5": dataclasses.asdict(cost)},
        "random": rnd, "criteria": criteria,
    })
    _write(path, lock)
    return {"lock_path": str(path), "result": res, "first_run": lock["first_run"]}
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_holdout.py -q
```

예상: `9 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/stratb/holdout.py tests/stratb/test_holdout.py
git commit -m "feat(stratb): holdout-b.lock written before any 2024 bar, one-shot holdout and the spec 6.5 criteria" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: 모의매매 어댑터: 장부, 판단, 알림

**Files:**
- Create: `src/ewbot/paperb/__init__.py`
- Create: `src/ewbot/paperb/ledger.py`
- Create: `src/ewbot/paperb/decisions.py`
- Create: `src/ewbot/paperb/notify.py`
- Test: `tests/paperb/__init__.py`
- Test: `tests/paperb/test_adapters.py`

**Interfaces:**
- Consumes: Task 5: `BResult`, `run_engine`(테스트), `SignalB`·`build_frame`(테스트). A(import만): `ewbot.paper.ledger.Ledger`, `current_epoch`, `ensure_epoch`, `replace_epoch_trades`, `save_ledger`, `load_ledger`; `ewbot.paper.decisions.decision_id(kind, ts, trade_key)`, `verify_decisions`(테스트); `ewbot.paper.notify`: `HEAD = "[ewbot paper]"`, `make_event(kind, ts, trade_key, text)`, `close_text(t, count, counted)`, `stop_move_text(dec, t)`, `failure_text`, `position_line(pos)`.
- Produces: `ewbot.paperb.ledger`: `PAPER_B_REQUIRED = 30`; `holdout_summary_b(lock) -> dict`(`lock_sha256`, `params`, `holdout_start`, `replay_anchor`, `last_bar_ts`, `data_sha256`, `n_trades: {"B": n}`, `passed`); `new_ledger_b(lock) -> Ledger`(구간 1 = 잠금의 B 지문, 시작 = 잠금의 마지막 봉); `paper_count_b(ledger) -> dict`(`epoch_id`, `eligible`, `paper`, `required`).
  `ewbot.paperb.decisions`: `split_result_b(res) -> (list[Trade], Trade | None)`; `build_decisions_b(res) -> list[dict]`(A의 판단 형식: `id`, `kind`, `ts`, `price`, `contracts`, `reason`, `trade_key`, `levels`, `setup`, `direction`); `open_position_b(open_trade, res, epoch_start) -> dict`(A의 `position_line`이 읽는 키).
  `ewbot.paperb.notify`: `HEAD_B = "[ewbot paper-B]"`; `b_head(text) -> str`(A 메시지의 접두어 교체, A 메시지가 아니면 `ValueError`); `entry_text_b(t) -> str`; `close_text_b(t, n_at, required, counted) -> str`; `holdout_trades_text_b(expected, got) -> str`; `daily_text_b(ny_date, lines) -> str`; `decision_events_b(fresh, trades, open_key, epoch_start, count, cur_closed) -> list[dict]`.

A의 `paper/` 부품을 수정하지 않고 재사용하기 위한 얇은 어댑터다. 장부는 A의 `Ledger` 스키마(버전 1)와 원자적 입출력을 그대로 쓰고, 묶이는 대상만 `holdout-b.lock`으로 바꾼다. 판단은 A의 형식으로 만들어 A의 대조 함수를 그대로 쓴다(해석 13). 메시지는 A의 형식을 쓰되 접두어를 `[ewbot paper-B]`로 바꾸고, 파동 카운트를 설명하는 A의 진입 메시지만 B용으로 새로 쓴다.

- [ ] **Step 1: 실패하는 테스트 작성**

장부가 A 스키마로 저장·적재되는지, 카운트가 30건 기준이고 지문 변경 시 0이 되는지, 판단 종류·수량·본절 기록과 A의 대조 결과(그대로면 불일치 없음, 가격을 바꾸면 CHANGED·UNEXPLAINED), 마지막 봉 포지션 분리, 접두어, 알림 이벤트를 확인한다.

```bash
mkdir -p src/ewbot/paperb tests/paperb
```

**파일 `tests/paperb/__init__.py`** (새 파일, 전체 내용)

```python
"""Tests for ewbot.paperb (Plan 4)."""
```

**파일 `tests/paperb/test_adapters.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import pandas as pd
import pytest

from ewbot.paper import notify as a_notify
from ewbot.paper.decisions import verify_decisions
from ewbot.paper.ledger import Ledger, ensure_epoch, load_ledger, replace_epoch_trades, save_ledger
from ewbot.paper.notify import position_line
from ewbot.paperb.decisions import build_decisions_b, open_position_b, split_result_b
from ewbot.paperb.ledger import PAPER_B_REQUIRED, holdout_summary_b, new_ledger_b, paper_count_b
from ewbot.paperb.notify import HEAD_B, b_head, close_text_b, daily_text_b, decision_events_b, entry_text_b
from ewbot.stratb.config import BConfig
from ewbot.stratb.engine import run_engine
from ewbot.stratb.signals import SignalB, build_frame

T0 = 1_700_006_400
M15 = 900
LOCK = {
    "sha256": "ab" * 32, "params": [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"], "holdout_start": 1_704_067_200,
    "replay_anchor": 1_696_118_400, "last_bar_ts": 1_717_027_200 - 900, "code_sha256": "cd" * 32, "git_head": "f00",
    "first_run": {"data": {"sha256": "ef" * 32}, "metrics": {"B": {"n_trades": 62}}, "criteria": {"passed": True}},
}


def _frame(rows):
    return build_frame(pd.DataFrame([(T0 + k * M15, o, h, lo, c, 1.0) for k, (o, h, lo, c) in enumerate(rows)],
                                    columns=["ts", "open", "high", "low", "close", "volume"]))


def _run(rows, end_open=False):
    f = _frame(rows)
    s = SignalB(i=2, ts=int(f.ts[2]), direction=1, entry_ref=20_000.0, stop=19_900.0, tp1=20_100.0, tp2=20_300.0,
                reason="long div 1/2 break 3.00")
    return f, run_engine(f, [s], BConfig())


FLAT = [(20_000.0, 20_005.0, 19_995.0, 20_000.0)]
TP1_THEN_BE = FLAT * 4 + [(20_000.0, 20_150.0, 19_950.0, 20_120.0), (20_120.0, 20_120.0, 19_850.0, 19_860.0)] + FLAT * 2


def test_the_ledger_is_strategy_a_schema_bound_to_the_b_lock(tmp_path):
    led = new_ledger_b(LOCK)
    assert led.baseline == {"fingerprint": "cd" * 32, "commit": "f00"}
    assert led.epochs == [{"id": 1, "fingerprint": "cd" * 32, "started_ts": LOCK["last_bar_ts"],
                           "reason": "baseline: holdout-b.lock", "through_ts": LOCK["last_bar_ts"]}]
    assert holdout_summary_b(LOCK)["n_trades"] == {"B": 62} and holdout_summary_b(LOCK)["passed"] is True
    save_ledger(tmp_path, led)
    assert isinstance(load_ledger(tmp_path), Ledger) and load_ledger(tmp_path).holdout == led.holdout


def test_the_paper_count_needs_30_and_resets_with_a_new_b_fingerprint():
    led = new_ledger_b(LOCK)
    _, res = _run(TP1_THEN_BE)
    trade = res.trades[0]
    replace_epoch_trades(led, 1, [trade])
    assert paper_count_b(led) == {"epoch_id": 1, "eligible": True, "paper": 1, "required": PAPER_B_REQUIRED}
    ensure_epoch(led, "99" * 32, LOCK["last_bar_ts"] + 3600)
    assert paper_count_b(led) == {"epoch_id": 2, "eligible": False, "paper": 0, "required": 30}


def test_decisions_are_strategy_a_records_and_replay_cleanly():
    f, res = _run(TP1_THEN_BE)
    dec = build_decisions_b(res)
    assert [d["kind"] for d in dec] == ["SIGNAL", "ENTRY", "TP1", "BREAKEVEN", "STOP"]
    sig, entry, tp1, be, stop = dec
    assert (sig["ts"], sig["contracts"], sig["levels"], sig["trade_key"]) == (int(f.ts[2]), 41, [19_900.0, 20_100.0, 20_300.0], int(f.ts[2]))
    assert entry["id"] == f"ENTRY:{int(f.ts[3])}:{int(f.ts[3])}" and entry["reason"] == "B"
    assert (be["contracts"], be["levels"], be["price"]) == (21, [19_900.0], entry["price"])
    assert stop["reason"] == "STOP" and tp1["contracts"] == 20
    epoch = {"id": 1, "started_ts": 0, "through_ts": int(f.ts[-1])}
    rec = [{**d, "epoch_id": 1} for d in dec]
    assert verify_decisions(rec, dec, epoch, lambda d: False, lambda d: False) == []
    changed = [dict(d) for d in dec]
    changed[2]["price"] += 1.0
    (disc,) = verify_decisions(rec, changed, epoch, lambda d: False, lambda d: False)
    assert (disc["type"], disc["cause"]) == ("CHANGED", "UNEXPLAINED")


def test_a_position_open_at_the_last_bar_is_split_off_and_described():
    f, res = _run(FLAT * 8)
    closed, open_trade = split_result_b(res)
    assert closed == [] and open_trade.exit_reason == "END"
    pos = open_position_b(open_trade, res, epoch_start=int(f.ts[0]))
    assert (pos["remaining"], pos["stop"], pos["counted"], pos["tp1_done"]) == (41, 19_900.0, True, False)
    assert position_line(pos).startswith("포지션: B 롱 41/41계약")
    assert "ENTRY" in [d["kind"] for d in build_decisions_b(res)] and "EXIT" not in [d["kind"] for d in build_decisions_b(res)]


def test_messages_carry_the_paper_b_prefix():
    _, res = _run(TP1_THEN_BE)
    t = res.trades[0]
    assert entry_text_b(t).startswith(f"{HEAD_B} 신규 진입") and "근거: long div 1/2 break 3.00" in entry_text_b(t)
    assert close_text_b(t, 3, 30, True).startswith(f"{HEAD_B} 청산 · STOP") and "3/30" in close_text_b(t, 3, 30, True)
    assert b_head(a_notify.failure_text("data", "x", None)) == f"{HEAD_B} 실패 · 단계 data\nx"
    assert daily_text_b("2024-06-01", ["a", "b"]) == f"{HEAD_B} 일일 요약 2024-06-01 (뉴욕)\na\nb"
    with pytest.raises(ValueError):
        b_head("[LIVE] x")


def test_decision_events_announce_entries_breakeven_moves_and_closes():
    f, res = _run(TP1_THEN_BE)
    dec = build_decisions_b(res)
    trades = {t.entry_ts: t for t in res.trades}
    events = decision_events_b(dec, trades, None, epoch_start=0, count={"required": 30}, cur_closed=list(res.trades))
    assert [e["kind"] for e in events] == ["ENTRY", "BREAKEVEN", "CLOSE"]
    assert all(e["text"].startswith(HEAD_B) for e in events) and "1/30" in events[-1]["text"]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/paperb/test_adapters.py -q
```

예상: `No module named 'ewbot.paperb`
예상: `1 error`

`ewbot.paperb`가 없어 수집 단계에서 실패한다(빈 폴더 때문에 메시지는 `No module named 'ewbot.paperb.decisions'`처럼 나온다).

- [ ] **Step 3: 구현**

패키지와 어댑터 3개를 만든다.

**파일 `src/ewbot/paperb/__init__.py`** (새 파일, 전체 내용)

```python
"""Paper trading of strategy B (Plan 4 spec 7): an hourly deterministic recompute on the `paper-b` branch.

Outside the B fingerprint (stratb/ only) and outside strategy A's paper runtime (paper/ is imported, never
changed)."""
```

**파일 `src/ewbot/paperb/ledger.py`** (새 파일, 전체 내용)

```python
"""Paper-B ledger: strategy A's ledger schema and IO (ewbot.paper.ledger) bound to holdout-b.lock.

Epoch 1 starts at the lock's last bar with the lock's B fingerprint; another B fingerprint opens a new
epoch (A's ensure_epoch) and the count starts again (spec 7). Paper trades are the current epoch's closed
trades that entered after its start (A's paper_trades); 30 are needed (spec 7.1). Holdout trades never
count."""

from __future__ import annotations

from typing import Any

from ewbot.paper.ledger import Ledger, current_epoch

PAPER_B_REQUIRED = 30


def holdout_summary_b(lock: dict[str, Any]) -> dict[str, Any]:
    """The part of holdout-b.lock the ledger is bound to (the precheck validated the keys)."""
    fr = lock["first_run"]
    return {
        "lock_sha256": lock["sha256"],
        "params": lock["params"],
        "holdout_start": int(lock["holdout_start"]),
        "replay_anchor": int(lock["replay_anchor"]),
        "last_bar_ts": int(lock["last_bar_ts"]),
        "data_sha256": fr["data"]["sha256"],
        "n_trades": {"B": int(fr["metrics"]["B"]["n_trades"])},
        "passed": bool(fr["criteria"]["passed"]),
    }


def new_ledger_b(lock: dict[str, Any]) -> Ledger:
    hs = holdout_summary_b(lock)
    start = hs["last_bar_ts"]
    fp = lock["code_sha256"]
    epoch = {"id": 1, "fingerprint": fp, "started_ts": start, "reason": "baseline: holdout-b.lock", "through_ts": start}
    return Ledger(baseline={"fingerprint": fp, "commit": str(lock.get("git_head") or "holdout-b.lock")}, holdout=hs,
                  epochs=[epoch])


def paper_count_b(ledger: Ledger) -> dict[str, Any]:
    cur = current_epoch(ledger)
    n = sum(1 for t in ledger.trades if t["epoch_id"] == cur["id"])
    return {"epoch_id": int(cur["id"]), "eligible": cur["fingerprint"] == ledger.baseline["fingerprint"], "paper": n,
            "required": PAPER_B_REQUIRED}
```

**파일 `src/ewbot/paperb/decisions.py`** (새 파일, 전체 내용)

```python
"""Decisions of one strategy-B recompute in strategy A's decision format (ewbot.paper.decisions), so that
A's replay check - verify_decisions, decisions_to_record, classify (DATA_REVISION, FINGERPRINT_CHANGE,
ENVIRONMENT_CHANGE, UNEXPLAINED), same_decision - runs unchanged on them.

  SIGNAL     ts = confirming 15m bar, price = entry_ref, contracts = queued size, levels = [stop, tp1, tp2]
  ENTRY      ts = fill bar (the next 15m open), reason = setup "B"
  TP1 / TP2 / STOP / EXIT   fills (EXIT reason: TIME or BREAKER)
  BREAKEVEN  the rest's stop moved to the entry price after TP1: price = new stop, levels = [old stop]
The END close of a position still open at the last bar is not a decision. A run whose last bar is T
knows every fill and stop move with ts <= T and every signal with ts <= T - 15m (no entry is evaluated on
the last bar); A's known_at() dates a SIGNAL at ts + 1h, which only ever delays its APPEARED check."""

from __future__ import annotations

from typing import Any, Sequence

from ewbot.engine.loop import Trade
from ewbot.paper.decisions import decision_id
from ewbot.stratb.engine import BResult

_ORDER = {k: i for i, k in enumerate(("EXIT", "ENTRY", "STOP", "TP1", "TP2", "BREAKEVEN", "TRAIL", "SIGNAL"))}


def split_result_b(res: BResult) -> tuple[list[Trade], Trade | None]:
    """(closed trades, the position the engine force-closed at the last bar)."""
    trades = list(res.trades)
    if res.end_fill is None:
        return trades, None
    last = trades[-1]
    if last.exit_reason != "END" or last.entry_ts != res.end_fill.trade_key:
        raise ValueError("the END-closed trade is not the last trade of the run")
    return trades[:-1], last


def _decision(kind: str, ts: int, price: float, contracts: int, reason: str, key: int,
              levels: Sequence[float | None], setup: str, direction: int) -> dict[str, Any]:
    return {"id": decision_id(kind, ts, key), "kind": kind, "ts": int(ts), "price": float(price),
            "contracts": int(contracts), "reason": reason, "trade_key": int(key),
            "levels": [None if v is None else float(v) for v in levels], "setup": setup, "direction": int(direction)}


def build_decisions_b(res: BResult) -> list[dict[str, Any]]:
    """Every decision of one run, ordered by (ts, kind) as A's build_decisions."""
    by_entry = {t.entry_ts: t for t in res.trades}
    out = [
        _decision("SIGNAL", s.ts, s.entry_ref, o.contracts, s.reason, s.ts, [s.stop, s.tp1, s.tp2], "B", s.direction)
        for s, o in zip(res.signals, res.orders, strict=True)
    ]
    tp1_done: list[tuple[int, int, int]] = []
    for f in res.fills:
        t = by_entry[f.trade_key]
        reason = t.setup if f.kind == "ENTRY" else (t.exit_reason if f.kind == "EXIT" else f.kind)
        out.append(_decision(f.kind, f.ts, f.price, f.contracts, reason, f.trade_key, [], t.setup, t.direction))
        if f.kind == "TP1":
            tp1_done.append((f.ts, f.trade_key, f.contracts))
    for s in res.stops:
        t = by_entry[s.trade_key]
        left = t.contracts - sum(n for ts, key, n in tp1_done if key == s.trade_key and ts <= s.ts)
        out.append(_decision(s.kind, s.ts, s.new, left, s.kind, s.trade_key, [s.old], t.setup, t.direction))
    out.sort(key=lambda d: (d["ts"], _ORDER[d["kind"]]))
    return out


def open_position_b(open_trade: Trade, res: BResult, epoch_start: int) -> dict[str, Any]:
    """The position held after the last bar, in the shape A's notify.position_line reads."""
    key = open_trade.entry_ts
    tp1 = sum(f.contracts for f in res.fills if f.trade_key == key and f.kind == "TP1")
    moves = [s for s in res.stops if s.trade_key == key]
    return {
        "entry_ts": key, "setup": open_trade.setup, "direction": open_trade.direction,
        "contracts": open_trade.contracts, "remaining": open_trade.contracts - tp1,
        "entry_price": open_trade.entry_price, "stop0": open_trade.stop0,
        "stop": moves[-1].new if moves else open_trade.stop0, "tp1": open_trade.tp1, "tp2": open_trade.tp2,
        "tp1_done": tp1 > 0, "mark_ts": None if res.end_fill is None else res.end_fill.ts,
        "pnl_if_closed": open_trade.pnl, "r_if_closed": open_trade.r_multiple, "counted": key > int(epoch_start),
    }
```

**파일 `src/ewbot/paperb/notify.py`** (새 파일, 전체 내용)

```python
"""Paper-B Telegram messages (spec 7): strategy A's formats and delivery (ewbot.paper.notify), with the
prefix `[ewbot paper-B]`. Only the entry message is B's own (A's describes wave counts)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from ewbot.engine.loop import Trade
from ewbot.paper import notify as a_notify
from ewbot.paper.notify import make_event

HEAD_B = "[ewbot paper-B]"


def b_head(text: str) -> str:
    """A's message with the paper-B prefix in place of `[ewbot paper]`."""
    if not text.startswith(a_notify.HEAD):
        raise ValueError(f"not a paper message: {text[:40]!r}")
    return HEAD_B + text[len(a_notify.HEAD):]


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")


def entry_text_b(t: Trade) -> str:
    snap = t.snapshot or {}
    return "\n".join([
        f"{HEAD_B} 신규 진입",
        f"B {'롱' if t.direction > 0 else '숏'} · {_utc(t.entry_ts)}",
        f"진입 {t.entry_price:,.2f} · 손절 {t.stop0:,.2f} · {t.contracts}계약",
        f"TP1 {t.tp1:,.2f} (절반) · TP2 {float(t.tp2 or 0.0):,.2f}",
        f"근거: {snap.get('reason', '-')}",
    ])


def close_text_b(t: Trade, n_at: int, required: int, counted: bool) -> str:
    return b_head(a_notify.close_text(t, {"total": n_at, "required": required}, counted))


def holdout_trades_text_b(expected: int, got: int) -> str:
    return "\n".join([
        f"{HEAD_B} 경고: 홀드아웃 재현 불일치",
        f"재계산한 홀드아웃 구간 B 거래 {got}건 · 잠금 파일 {expected}건",
        "실행은 계속함 (모의 카운트는 잠금 이후 진입만 셈, 재현성 손상 기록)",
    ])


def daily_text_b(ny_date: str, lines: Sequence[str]) -> str:
    return "\n".join([f"{HEAD_B} 일일 요약 {ny_date} (뉴욕)", *lines])


def decision_events_b(
    fresh: Sequence[dict[str, Any]],
    trades: Mapping[int, Trade],
    open_key: int | None,
    epoch_start: int,
    count: Mapping[str, Any],
    cur_closed: Sequence[Trade],
) -> list[dict[str, Any]]:
    """ENTRY, BREAKEVEN and CLOSE events for the decisions first recorded in this run (A's rules)."""
    out: list[dict[str, Any]] = []
    for d in fresh:
        t = trades.get(d["trade_key"])
        if t is None:
            continue  # SIGNAL: its ENTRY fill carries the message
        if d["kind"] == "ENTRY":
            out.append(make_event("ENTRY", d["ts"], d["trade_key"], entry_text_b(t)))
        elif d["kind"] == "BREAKEVEN":
            out.append(make_event("BREAKEVEN", d["ts"], d["trade_key"], b_head(a_notify.stop_move_text(d, t))))
        elif d["kind"] in ("STOP", "TP1", "TP2", "EXIT") and t.entry_ts != open_key and t.exit_ts == d["ts"]:
            counted = t.entry_ts > int(epoch_start)
            n_at = sum(1 for x in cur_closed if x.exit_ts <= t.exit_ts)
            out.append(make_event("CLOSE", d["ts"], d["trade_key"], close_text_b(t, n_at, count["required"], counted)))
    return out
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/paperb/test_adapters.py -q
```

예상: `6 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/paperb/__init__.py src/ewbot/paperb/ledger.py src/ewbot/paperb/decisions.py src/ewbot/paperb/notify.py tests/paperb/__init__.py tests/paperb/test_adapters.py
git commit -m "feat(paperb): ledger, decision and message adapters over strategy A's paper components" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: 모의매매 실행기

**Files:**
- Create: `src/ewbot/paperb/runner.py`
- Test: `tests/paperb/helpers.py`
- Test: `tests/paperb/conftest.py`
- Test: `tests/paperb/test_runner.py`

**Interfaces:**
- Consumes: Task 1: `config_for`, `normalize_combo`, `stratb_fingerprint`. Task 2: `DEFAULT_LOCK_PATH`, `REPLAY_ANCHOR`, `load_15m`, `update_cache_15m`, `cache_path`(테스트). Task 6: `backtest_b`. Task 8: `HoldoutBError`, `committed_lock`, `read_lock`, `lock_digest_b`(테스트), `create_lock`·`run_holdout_b`(테스트 도우미). Task 9: 모든 어댑터. A(import만): `ewbot.backtest.holdout.data_fingerprint`; `ewbot.backtest.report.to_jsonable`; `ewbot.paper.decisions`: `decisions_to_record`, `known_at`, `paper_trades`, `verify_decisions`; `ewbot.paper.environment`: `environment_digest`, `runtime_environment`; `ewbot.paper.ledger`: `RUNS_LOG`, `Ledger`, `LedgerError`, `append_log_line`, `ensure_epoch`, `load_ledger`, `replace_epoch_trades`, `save_ledger`; `ewbot.paper.notify`: `NY`, `Sender`, `daily_summary_due`, `deliver`, `discrepancy_text`, `epoch_text`, `make_event`, `position_line`, `queue_events`. 테스트: `tests.paper.helpers.Clock`, `FakeSender`, `git`.
- Produces: `ewbot.paperb.runner`: `FETCH_ATTEMPTS = 3`, `DAILY_HOUR_NY = 9`; `class PaperBRunError(RuntimeError)`(`.stage`, `.message`); `default_fetch_b(cache_dir, lock_path) -> Path`; `@dataclass PaperBEnv(ledger_dir, lock_path=DEFAULT_LOCK_PATH, cache_dir=Path("data/cache"), fetch=default_fetch_b, sender=None, clock=<벽시계>, fingerprint=stratb_fingerprint, sleep=time.sleep, fetch_backoff_s=10.0, environment=runtime_environment)`; `@dataclass(frozen=True) RunOutcomeB(run_id, last_bar_ts, epoch_id, new_decisions, new_discrepancies, events, sent, pending, notes, count)`; `precheck_lock_b(lock_path) -> dict`; `status_lines_b(ledger, count) -> list[str]`; `run_paper_b(env) -> RunOutcomeB`. 실행 기록(`ledger.runs[]`)의 키: `id`, `ts`, `status`, `last_bar_ts`, `data_sha256`, `holdout_trades_recomputed`, `epoch_id`, `paper_trades`, `new_decisions`, `new_discrepancies`, `env`, `env_sha256`, `warnings`, `notify`. 장부 디렉터리에는 `ledger.json`과 `runs.log`만 쓴다.
  `tests/paperb/helpers.py`: `HS`, `LOCK_NOW`, `LAST_BAR`, `COMBO`, `M15`, `world_df()`, `build_passed_lock(tmp) -> str`, `commit_lock_b(repo, text) -> Path`, `FakeFetchB(source, clock, fail=0)`, `make_env(tmp_path, lock_text, now, source=None, **kw) -> (PaperBEnv, Clock, FakeSender)`. `tests/paperb/conftest.py`: 세션 픽스처 `lock_text`(합성 홀드아웃을 실제로 한 번 돌린 잠금 파일 내용).

명세 7·8과 해석 12~14를 구현한다. 단계: 사전 점검(잠금) → 장부(이 잠금에서 시작된 장부인지) → 데이터(3회 재시도, `REPLAY_ANCHOR` 이후, 잠금의 마지막 봉 이상, 홀드아웃 구간 해시 일치) → 지문·구간 → 재계산(`backtest_b(..., HOLDOUT_START, None, include_holdout=True)`, 홀드아웃 거래 수 재현 확인) → A의 대조 → 이벤트(진입, 본절, 청산, 불일치, 구간, 뉴욕 9시 일일 요약 n/30) → 쓰기 → 알림. 각 단계의 실패는 `PaperBRunError(stage)`이며, 쓰기 단계 전에는 아무것도 쓰지 않는다. 일일 요약의 실거래 전환 조건 4줄(명세 7.1)은 `status_lines_b`가 만든다. 시간마다 전체를 다시 계산하는 데 수 초가 걸린다(3년치 15분봉 약 10만 개).

- [ ] **Step 1: 실패하는 테스트 작성**

테스트 도우미(합성 세계, 실제로 한 번 돈 합성 홀드아웃의 잠금, 시계 기준으로 마감된 봉만 쓰는 가짜 수집기)와 실행기 테스트를 만든다.

**파일 `tests/paperb/helpers.py`** (새 파일, 전체 내용)

```python
"""Shared helpers for tests/paperb: one synthetic world (a long setup every 232 15m bars from 2023-10-01
to 2024-08), a strategy-B lock whose holdout really ran on it, a fake 15m fetcher. A's fake sender, clock
and git helper (tests.paper.helpers) are reused. No network, no real cache, no real lock."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.paperb.runner import PaperBEnv
from ewbot.stratb.data15 import REPLAY_ANCHOR, cache_path, load_15m
from ewbot.stratb.holdout import create_lock, run_holdout_b
from tests.paper.helpers import Clock, FakeSender, git
from tests.stratb.helpers import LONG_SETUP, LONG_START, frame_df, path

HS = 1_704_067_200
LOCK_NOW = HS + 150 * 86_400 + 1_234  # the lock's clock: last bar 2024-05-30 00:45 UTC
LAST_BAR = LOCK_NOW // 3600 * 3600 - 900
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]
M15 = 900


def world_df() -> pd.DataFrame:
    return frame_df(path(LONG_START, LONG_SETUP * 130), REPLAY_ANCHOR)


def build_passed_lock(tmp: Path) -> str:
    """The text of holdout-b.lock after a real (synthetic) holdout run that passed."""
    repo, cache = tmp / "repo", tmp / "cache"
    repo.mkdir()
    cache.mkdir()
    git(repo, "init", "-q")
    world_df().to_parquet(cache_path(cache), index=False)
    lp = repo / "holdout-b.lock"
    create_lock(COMBO, load_15m(cache_path(cache), lock_path=lp), params_source="test", now_ts=LOCK_NOW, lock_path=lp,
                random_runs=2)
    git(repo, "add", lp.name)
    git(repo, "commit", "-q", "-m", "lock")
    run_holdout_b(confirm=True, cache_dir=cache, lock_path=lp)
    lock = json.loads(lp.read_text(encoding="utf-8"))
    assert lock["first_run"]["criteria"]["passed"] is True
    return lp.read_text(encoding="utf-8")


def commit_lock_b(repo: Path, text: str) -> Path:
    """Write <repo>/holdout-b.lock and commit it (initialising the repo if needed)."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
    p = repo / "holdout-b.lock"
    p.write_text(text, encoding="utf-8")
    git(repo, "add", p.name)
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode != 0:
        git(repo, "commit", "-q", "-m", "lock")
    return p


class FakeFetchB:
    """Stands in for update_cache_15m: writes the bars of `source` closed by clock() into the 15m cache."""

    def __init__(self, source: pd.DataFrame | Callable[[], pd.DataFrame], clock: Callable[[], int], fail: int = 0):
        self.source, self.clock, self.fail, self.calls = source, clock, fail, 0

    def __call__(self, cache_dir: Path, lock_path: Path) -> Path:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("coinbase down")
        df = self.source() if callable(self.source) else self.source
        p = cache_path(cache_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        df[df["ts"] < int(self.clock()) // M15 * M15].reset_index(drop=True).to_parquet(p, index=False)
        return p


def make_env(tmp_path: Path, lock_text: str, now: int, source: Any = None, **kw: Any) -> tuple[PaperBEnv, Clock, FakeSender]:
    clock = Clock(now)
    sender = FakeSender()
    env = PaperBEnv(
        ledger_dir=tmp_path / "ledger", lock_path=commit_lock_b(tmp_path / "repo", lock_text),
        cache_dir=tmp_path / "cache", fetch=FakeFetchB(world_df() if source is None else source, clock),
        sender=sender, clock=clock, sleep=lambda s: None, environment=lambda: {"python": "3.12.13"},
    )
    for k, v in kw.items():
        setattr(env, k, v)
    return env, clock, sender
```

**파일 `tests/paperb/conftest.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import pytest

from tests.paperb.helpers import build_passed_lock


@pytest.fixture(scope="session")
def lock_text(tmp_path_factory) -> str:
    """holdout-b.lock after one real synthetic holdout run (computed once per session)."""
    return build_passed_lock(tmp_path_factory.mktemp("paperb-lock"))
```

**파일 `tests/paperb/test_runner.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json
from datetime import datetime

import pytest

from ewbot.paper.decisions import known_at
from ewbot.paper.ledger import load_ledger
from ewbot.paper.notify import NY
from ewbot.paperb.notify import HEAD_B
from ewbot.paperb.runner import PaperBRunError, precheck_lock_b, run_paper_b
from ewbot.stratb.holdout import lock_digest_b
from tests.paper.helpers import git
from tests.paperb.helpers import LAST_BAR, FakeFetchB, commit_lock_b, make_env, world_df

H1 = 3600


def _ny(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=NY).timestamp())


def test_the_precheck_needs_the_committed_passed_lock_of_this_code(tmp_path, lock_text):
    with pytest.raises(PaperBRunError, match="missing"):
        precheck_lock_b(tmp_path / "x" / "holdout-b.lock")
    p = commit_lock_b(tmp_path / "a", lock_text)
    assert precheck_lock_b(p)["first_run"]["criteria"]["passed"] is True
    lock = json.loads(lock_text)
    p.write_text(json.dumps({**lock, "note": "edited"}), encoding="utf-8")
    with pytest.raises(PaperBRunError, match="differs from the committed"):
        precheck_lock_b(p)
    (tmp_path / "b").mkdir()
    git(tmp_path / "b", "init", "-q")
    (tmp_path / "b" / "holdout-b.lock").write_text(lock_text, encoding="utf-8")
    with pytest.raises(PaperBRunError, match="not committed"):
        precheck_lock_b(tmp_path / "b" / "holdout-b.lock")
    with pytest.raises(PaperBRunError, match="has not run"):
        precheck_lock_b(commit_lock_b(tmp_path / "c", json.dumps({**lock, "first_run": None})))
    failed = json.loads(lock_text)
    failed["first_run"]["criteria"]["passed"] = False
    with pytest.raises(PaperBRunError, match="strategy B ended"):
        precheck_lock_b(commit_lock_b(tmp_path / "d", json.dumps(failed)))
    other = json.loads(lock_text)
    other["config"]["initial_equity"] = 20_000.0
    other["sha256"] = lock_digest_b(other)
    with pytest.raises(PaperBRunError, match="not this code's config"):
        precheck_lock_b(commit_lock_b(tmp_path / "e", json.dumps(other)))


def test_the_first_run_reproduces_the_holdout_and_writes_only_the_b_ledger(tmp_path, lock_text):
    env, _, _ = make_env(tmp_path, lock_text, LAST_BAR + 900 + H1 + 60)
    out = run_paper_b(env)
    led = load_ledger(env.ledger_dir)
    assert (out.epoch_id, out.last_bar_ts, out.notes) == (1, LAST_BAR + H1, [])
    assert led.runs[0]["holdout_trades_recomputed"] == led.holdout["n_trades"]["B"] == 62
    assert led.runs[0]["warnings"] == [] and all(known_at(d) > LAST_BAR for d in led.decisions)
    assert sorted(p.name for p in env.ledger_dir.iterdir()) == ["ledger.json", "runs.log"]
    assert "paper=0/30" in (env.ledger_dir / "runs.log").read_text()


def test_hourly_runs_count_paper_trades_and_notify_with_the_b_prefix(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 60)
    for k in range(25):
        clock.now = LAST_BAR + 900 + 60 + k * 6 * H1
        out = run_paper_b(env)
        assert out.new_discrepancies == 0
    led = load_ledger(env.ledger_dir)
    assert out.count["paper"] >= 2 and all(t["entry_ts"] > LAST_BAR for t in led.trades)
    assert any(x.startswith(f"{HEAD_B} 신규 진입") for x in sender.texts)
    assert any(x.startswith(f"{HEAD_B} 청산") for x in sender.texts)
    assert all(x.startswith(HEAD_B) for x in sender.texts) and led.discrepancies == []
    fresh, _, _ = make_env(tmp_path / "fresh", lock_text, clock.now)
    run_paper_b(fresh)
    assert {d["id"] for d in load_ledger(fresh.ledger_dir).decisions} == {d["id"] for d in led.decisions}


def test_a_changed_recorded_decision_is_an_unexplained_discrepancy(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 3 * 86_400)
    run_paper_b(env)
    raw = json.loads((env.ledger_dir / "ledger.json").read_text())
    assert raw["decisions"]
    raw["decisions"][0]["price"] += 1.0
    (env.ledger_dir / "ledger.json").write_text(json.dumps(raw))
    clock.now += H1
    out = run_paper_b(env)
    (disc,) = load_ledger(env.ledger_dir).discrepancies
    assert out.new_discrepancies == 1 and (disc["type"], disc["cause"]) == ("CHANGED", "UNEXPLAINED")
    assert sender.texts[-1].startswith(f"{HEAD_B} 경고: 결정 불일치 1건")


def test_a_new_b_fingerprint_opens_a_new_epoch_and_resets_the_count(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, LAST_BAR + 900 + 4 * 86_400)
    first = run_paper_b(env)
    assert first.count["paper"] >= 1
    env.fingerprint = lambda: "9" * 64
    clock.now += H1
    out = run_paper_b(env)
    assert out.epoch_id == 2 and out.count == {"epoch_id": 2, "eligible": False, "paper": 0, "required": 30}
    assert any(x.startswith(f"{HEAD_B} 경고: 전략 지문 변경 · 구간 2") for x in sender.texts)


def test_revised_holdout_bars_refuse_the_run(tmp_path, lock_text):
    df = world_df()
    df.loc[(df["ts"] - LAST_BAR).abs().idxmin() - 500, "close"] += 0.5
    env, _, _ = make_env(tmp_path, lock_text, LAST_BAR + 900 + 60, source=df)
    with pytest.raises(PaperBRunError) as exc:
        run_paper_b(env)
    assert exc.value.stage == "data" and "spec 8" in exc.value.message and not (env.ledger_dir / "ledger.json").exists()


def test_candle_updates_are_retried_three_times(tmp_path, lock_text):
    env, clock, _ = make_env(tmp_path, lock_text, LAST_BAR + 900 + 60)
    env.fetch = FakeFetchB(world_df(), clock, fail=2)
    run_paper_b(env)
    env.fetch = FakeFetchB(world_df(), clock, fail=3)
    with pytest.raises(PaperBRunError) as exc:
        run_paper_b(env)
    assert exc.value.stage == "data" and env.fetch.calls == 3


def test_one_daily_summary_per_new_york_date_from_9_am(tmp_path, lock_text):
    env, clock, sender = make_env(tmp_path, lock_text, _ny(2024, 6, 3, 8, 30))
    run_paper_b(env)
    assert not any("일일 요약" in x for x in sender.texts)
    clock.now = _ny(2024, 6, 3, 9, 5)
    run_paper_b(env)
    (daily,) = [x for x in sender.texts if "일일 요약" in x]
    assert daily.startswith(f"{HEAD_B} 일일 요약 2024-06-03 (뉴욕)") and "모의 " in daily and "/30" in daily
    assert "1. 홀드아웃 합격: 충족" in daily and "3. 원인 불명 불일치 0건" in daily
    clock.now = _ny(2024, 6, 3, 10, 5)
    run_paper_b(env)
    assert len([x for x in sender.texts if "일일 요약" in x]) == 1
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/paperb/test_runner.py -q
```

예상: `No module named 'ewbot.paperb.runner'`

`ewbot.paperb.runner`가 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

실행기를 만든다.

**파일 `src/ewbot/paperb/runner.py`** (새 파일, 전체 내용)

```python
"""One `ewbot stratb paper run` (Plan 4 spec 7, 8): precheck -> data -> fingerprint/epoch -> full recompute
from REPLAY_ANCHOR -> replay check -> ledger and runs.log -> Telegram.

Inputs are frozen by holdout-b.lock: the params and the whole run config come from it (the precheck refuses
a lock whose sha256 is not the digest of its fields, that is not committed unchanged, whose holdout has
not run or failed - strategy B ended -, or whose config is not config_for(params) of this code). The
recompute starts at REPLAY_ANCHOR (2023-10-01) and trades from HOLDOUT_START, exactly like the holdout
run, so it reproduces the holdout and continues it; trades entering after the lock's last bar are paper
trades. The holdout window's bars must still hash to first_run.data.sha256, else the run is refused
(spec 8). A different B fingerprint opens a new epoch (spec 7). Decisions, discrepancies, the
environment record and Telegram delivery are strategy A's (paper/ imported, never changed).

Every step runs inside a named stage: any failure raises PaperBRunError(stage); the CLI sends the failure
alert. Nothing is written before the write stage."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.backtest.holdout import data_fingerprint
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.paper.decisions import decisions_to_record, known_at, paper_trades, verify_decisions
from ewbot.paper.environment import environment_digest, runtime_environment
from ewbot.paper.ledger import (
    RUNS_LOG,
    Ledger,
    LedgerError,
    append_log_line,
    ensure_epoch,
    load_ledger,
    replace_epoch_trades,
    save_ledger,
)
from ewbot.paper.notify import (
    NY,
    Sender,
    daily_summary_due,
    deliver,
    discrepancy_text,
    epoch_text,
    make_event,
    position_line,
    queue_events,
)
from ewbot.paperb.decisions import build_decisions_b, open_position_b, split_result_b
from ewbot.paperb.ledger import holdout_summary_b, new_ledger_b, paper_count_b
from ewbot.paperb.notify import b_head, daily_text_b, decision_events_b, holdout_trades_text_b
from ewbot.stratb.config import config_for, normalize_combo
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH, REPLAY_ANCHOR, load_15m, update_cache_15m
from ewbot.stratb.fingerprint import stratb_fingerprint
from ewbot.stratb.holdout import HoldoutBError, committed_lock, read_lock
from ewbot.stratb.research import backtest_b
from ewbot.types import H1

FETCH_ATTEMPTS = 3
DAILY_HOUR_NY = 9


class PaperBRunError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


def default_fetch_b(cache_dir: Path, lock_path: Path) -> Path:
    """Extend the 15m cache (an empty one starts at REPLAY_ANCHOR: the recompute needs nothing older)."""
    return update_cache_15m(Path(cache_dir), lock_path=Path(lock_path), start_ts=REPLAY_ANCHOR)


def _wall_clock() -> int:
    return int(time.time())


@dataclass
class PaperBEnv:
    """Everything a run touches; tests inject fakes (no network, temp git repos)."""

    ledger_dir: Path
    lock_path: Path = DEFAULT_LOCK_PATH
    cache_dir: Path = Path("data/cache")
    fetch: Callable[[Path, Path], Path] = default_fetch_b
    sender: Sender | None = None
    clock: Callable[[], int] = _wall_clock
    fingerprint: Callable[[], str] = stratb_fingerprint
    sleep: Callable[[float], None] = time.sleep
    fetch_backoff_s: float = 10.0
    environment: Callable[[], dict[str, Any]] = runtime_environment


@dataclass(frozen=True)
class RunOutcomeB:
    run_id: str
    last_bar_ts: int
    epoch_id: int
    new_decisions: int
    new_discrepancies: int
    events: list[str]
    sent: list[str]
    pending: int
    notes: list[str]
    count: dict[str, Any]


@contextmanager
def _stage(name: str, *, detail: bool = True) -> Iterator[None]:
    try:
        yield
    except PaperBRunError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its stage
        if detail:
            raise PaperBRunError(name, f"{type(exc).__name__}: {exc}") from exc
        raise PaperBRunError(name, f"{type(exc).__name__} (details withheld: may contain the token)") from None


def precheck_lock_b(lock_path: Path) -> dict[str, Any]:
    """The committed, passed strategy-B lock whose config is this code's config for its params."""
    try:
        lock = read_lock(lock_path)
        if committed_lock(lock_path) != lock:
            raise PaperBRunError("precheck", f"{lock_path} differs from the committed version")
    except HoldoutBError as exc:
        raise PaperBRunError("precheck", str(exc)) from exc
    fr = lock.get("first_run")
    if not isinstance(fr, dict) or "criteria" not in fr or "data" not in fr:
        raise PaperBRunError("precheck", "the strategy-B holdout has not run: paper trading starts after it passes")
    if not fr["criteria"].get("passed"):
        raise PaperBRunError("precheck", "the strategy-B holdout failed (spec 6.5): strategy B ended, no paper trading")
    try:
        combo = normalize_combo(lock["params"])
    except ValueError as exc:
        raise PaperBRunError("precheck", str(exc)) from exc
    if int(lock["holdout_start"]) != HOLDOUT_START or int(lock["replay_anchor"]) != REPLAY_ANCHOR:
        raise PaperBRunError("precheck", "the lock's holdout start or replay anchor is not this code's")
    if lock["config"] != to_jsonable(dataclasses.asdict(config_for(combo))):
        raise PaperBRunError("precheck", "the lock's run config is not this code's config for its params")
    return lock


def _open_ledger(ledger_dir: Path, lock: dict[str, Any]) -> Ledger:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    try:
        ledger = load_ledger(ledger_dir)
    except LedgerError as exc:
        raise PaperBRunError("ledger", str(exc)) from exc
    if ledger is None:
        return new_ledger_b(lock)
    bound = holdout_summary_b(lock)
    diff = sorted(k for k in bound.keys() | ledger.holdout.keys() if ledger.holdout.get(k) != bound.get(k))
    if diff:
        raise PaperBRunError("precheck", f"ledger.json was started from another holdout-b.lock (differs in: {diff})")
    return ledger


def _load_data(env: PaperBEnv) -> pd.DataFrame:
    path: Path | None = None
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            path = Path(env.fetch(Path(env.cache_dir), Path(env.lock_path)))
            break
        except Exception as exc:  # noqa: BLE001 - network / API errors are retried
            last = exc
            if attempt < FETCH_ATTEMPTS - 1:
                env.sleep(env.fetch_backoff_s * 2**attempt)
    if path is None:
        raise PaperBRunError("data", f"candle update failed {FETCH_ATTEMPTS} times: {type(last).__name__}: {last}")
    with _stage("data"):
        df = load_15m(path, include_holdout=True, lock_path=env.lock_path)
        df = df[df["ts"] >= REPLAY_ANCHOR].reset_index(drop=True)
        if df.empty:
            raise PaperBRunError("data", f"{path} has no bars from the replay anchor on")
        return df


def _recording_floor(ledger: Ledger) -> int:
    if len(ledger.epochs) == 1:
        return int(ledger.epochs[0]["started_ts"])
    return int(ledger.epochs[-2]["through_ts"])


def _run_id(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


def status_lines_b(ledger: Ledger, count: dict[str, Any]) -> list[str]:
    """Go-live checklist (spec 7.1), Korean, for the daily summary and `paper status`."""
    cur = int(count["epoch_id"])
    causes = [d["cause"] for d in ledger.discrepancies if int(d["epoch_id"]) == cur]
    unexplained = causes.count("UNEXPLAINED")
    lines = ["실거래 전환 조건 (Plan 4 명세 7.1)"]
    if not count["eligible"]:
        lines.append(f"현재 구간 {cur}의 B 지문이 잠금 파일과 다름: 실거래 전환 판정 대상 아님")
    def mark(ok: bool) -> str:
        return "충족" if ok else "미충족"

    lines.append(f"1. 홀드아웃 합격: {mark(bool(ledger.holdout.get('passed')))}")
    lines.append(f"2. 모의 거래 {count['paper']}/{count['required']}: {mark(count['paper'] >= count['required'])}")
    lines.append(f"3. 원인 불명 불일치 {unexplained}건 (전체 {len(causes)}건): {mark(unexplained == 0)}")
    lines.append("4. 사용자 승인: 수동")
    return lines


def run_paper_b(env: PaperBEnv) -> RunOutcomeB:
    with _stage("precheck"):
        now = int(env.clock())
        run_id = _run_id(now)
        lock = precheck_lock_b(Path(env.lock_path))
        cfg = config_for(normalize_combo(lock["params"]))
        ledger_dir = Path(env.ledger_dir)
        run_env = dict(env.environment())
        env_sha = environment_digest(run_env)
    with _stage("ledger"):
        ledger = _open_ledger(ledger_dir, lock)
        hs = ledger.holdout
    df = _load_data(env)
    with _stage("data"):
        last_bar = int(df["ts"].iloc[-1])
        if last_bar < hs["last_bar_ts"]:
            raise PaperBRunError("data", f"data ends at {last_bar}, before the lock's last bar {hs['last_bar_ts']}")
        if data_fingerprint(df, REPLAY_ANCHOR, hs["last_bar_ts"]) != hs["data_sha256"]:
            raise PaperBRunError("data", "the holdout window's 15m bars differ from the holdout run's (spec 8)")
    with _stage("fingerprint"):
        epoch, created = ensure_epoch(ledger, env.fingerprint(), last_bar)
        floor = _recording_floor(ledger)
        if created:
            epoch["through_ts"] = floor
    with _stage("recompute"):
        res = backtest_b(df, cfg, HOLDOUT_START, None, include_holdout=True)
        closed, open_trade = split_result_b(res)
        all_trades = closed + ([open_trade] if open_trade is not None else [])
        by_entry = {t.entry_ts: t for t in all_trades}
        holdout_n = sum(1 for t in all_trades if HOLDOUT_START <= t.entry_ts <= hs["last_bar_ts"])
        cur_closed = paper_trades(closed, int(epoch["started_ts"]))
        replace_epoch_trades(ledger, epoch["id"], cur_closed)
        start = int(epoch["started_ts"])
        ledger.open_position = None if open_trade is None else open_position_b(open_trade, res, start)
        memo: dict[int, str] = {}

        def prefix(ts: int) -> str:
            if ts not in memo:
                memo[ts] = data_fingerprint(df, 0, int(ts))
            return memo[ts]

        ok_runs = sorted((r for r in ledger.runs if r.get("status") == "ok"), key=lambda r: r["last_bar_ts"])
        prev_env = ok_runs[-1].get("env_sha256") if ok_runs else None

        def first_knowing(r: dict[str, Any]) -> dict[str, Any] | None:
            return next((x for x in ok_runs if x["last_bar_ts"] >= known_at(r)), None)

        def appeared_revised(r: dict[str, Any]) -> bool:
            run = first_knowing(r)
            return run is not None and prefix(run["last_bar_ts"]) != run["data_sha256"]

        def appeared_env(r: dict[str, Any]) -> str | None:
            run = first_knowing(r)
            return None if run is None else run.get("env_sha256")

        recomputed = build_decisions_b(res)
        found = verify_decisions(ledger.decisions, recomputed, epoch, lambda d: prefix(d["ts"]) != d["data_sha256"],
                                 appeared_revised, appeared_after=floor, env_sha256=env_sha, appeared_env=appeared_env,
                                 previous_env_sha256=prev_env)
        known = {d["id"] for d in ledger.discrepancies}
        new_disc = [{**d, "epoch_id": epoch["id"], "detected_ts": last_bar, "detected_run": run_id}
                    for d in found if d["id"] not in known]
        ledger.discrepancies.extend(new_disc)
        fresh = decisions_to_record(recomputed, epoch, {d["id"] for d in ledger.decisions})
        for d in fresh:
            d.update(run_id=run_id, data_sha256=prefix(d["ts"]), epoch_id=epoch["id"], env_sha256=env_sha)
        ledger.decisions.extend(fresh)
        epoch["through_ts"] = max(int(epoch["through_ts"]), last_bar)
    with _stage("events"):
        count = paper_count_b(ledger)
        events: list[dict[str, Any]] = []
        warnings: list[str] = []
        lock_n = int(hs["n_trades"]["B"])
        if holdout_n != lock_n:
            warnings.append(f"holdout trades recomputed {holdout_n} != lock {lock_n}")
            events.append(make_event("HOLDOUT_TRADES", hs["last_bar_ts"], holdout_n, holdout_trades_text_b(lock_n, holdout_n)))
        if created:
            events.append(make_event("EPOCH", epoch["started_ts"], epoch["id"], b_head(epoch_text(epoch))))
        events += decision_events_b(fresh, by_entry, None if open_trade is None else open_trade.entry_ts,
                                    int(epoch["started_ts"]), count, cur_closed)
        if new_disc:
            text = b_head(discrepancy_text(new_disc))
            events.append(make_event("DISCREPANCY", last_bar, len(ledger.discrepancies), text))
        data_sha = prefix(last_bar)
        run: dict[str, Any] = {
            "id": run_id, "ts": now, "status": "ok", "last_bar_ts": last_bar, "data_sha256": data_sha,
            "holdout_trades_recomputed": holdout_n, "epoch_id": epoch["id"], "paper_trades": count["paper"],
            "new_decisions": len(fresh), "new_discrepancies": len(new_disc), "env": run_env, "env_sha256": env_sha,
            "warnings": warnings,
        }
        ledger.runs.append(run)
        due = daily_summary_due(now, ledger.daily_summary_date)
        if due is not None:
            eq = {p.ts: p.equity for p in res.equity}
            base = eq.get(hs["last_bar_ts"] // H1 * H1, cfg.initial_equity)
            last_eq = res.equity[-1].equity if res.equity else cfg.initial_equity
            lines = [position_line(ledger.open_position),
                     f"자산 {last_eq:,.2f} USD (잠금 이후 {last_eq / base - 1:+.2%})",
                     f"모의 {count['paper']}/{count['required']} · 구간 {epoch['id']}",
                     *status_lines_b(ledger, count)]
            y, m, d = (int(x) for x in due.split("-"))
            day_ts = int(datetime(y, m, d, DAILY_HOUR_NY, tzinfo=NY).timestamp())
            events.append(make_event("DAILY", day_ts, 0, daily_text_b(due, lines)))
            ledger.daily_summary_date = due
        queued = queue_events(ledger, events)
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
        append_log_line(
            ledger_dir / RUNS_LOG,
            f"{datetime.fromtimestamp(now, UTC).isoformat(timespec='seconds')}\tok\tlast_bar="
            f"{datetime.fromtimestamp(last_bar, UTC).isoformat(timespec='minutes')}\tdata={data_sha[:12]}\t"
            f"paper={count['paper']}/{count['required']}\tepoch={epoch['id']}\tnew_decisions={len(fresh)}",
        )
    with _stage("notify", detail=False):
        remaining, sent, errors = deliver(ledger.pending_events, ledger.sent_event_ids, env.sender, sleep=env.sleep)
        ledger.pending_events = remaining
        ledger.sent_event_ids.extend(sent)
        run["notify"] = {"sent": len(sent), "pending": len(remaining), "errors": errors}
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
    return RunOutcomeB(run_id, last_bar, int(epoch["id"]), len(fresh), len(new_disc), queued, sent, len(remaining),
                       warnings + errors, count)

```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/paperb/test_runner.py -q
```

예상: `8 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/paperb/runner.py tests/paperb/helpers.py tests/paperb/conftest.py tests/paperb/test_runner.py
git commit -m "feat(paperb): hourly deterministic paper-B run from REPLAY_ANCHOR with A's replay check and ledger" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: CLI `ewbot stratb …`

**Files:**
- Modify: `src/ewbot/cli.py` (6곳)
- Test: `tests/stratb/test_cli_stratb.py`
- Test: `tests/paperb/test_cli_paper_b.py`

**Interfaces:**
- Consumes: Task 1: `combo_to_json`, `config_for`, `normalize_combo`. Task 2: `DEFAULT_LOCK_PATH`(cli.py에서 `STRATB_LOCK`), `cache_path`(`b_cache_path`), `gap_report`, `load_15m`, `lock_exists`, `update_cache_15m`. Task 6: `backtest_b`, `walk_forward_b`. Task 8: `HoldoutBError`, `create_lock`, `run_holdout_b`. Task 9: `paper_count_b`, `b_head`. Task 10: `PaperBEnv`, `PaperBRunError`, `run_paper_b`, `status_lines_b`. cli.py에 이미 있는 것: `_refuse`, `_requested_window`, `_print_comparison`, `to_jsonable`, `compute_metrics`, `deliver`, `failure_text`, `make_event`, `position_line`, `sender_from_env`, `load_ledger`, `LedgerError`, `current_epoch`, `RUN_URL_ENV`, `FAILURE_MARKER_ENV`, `WF_END`.
- Produces: `ewbot stratb fetch [--cache-dir]`; `ewbot stratb backtest --params zz_k,z_lo,z_hi,tp1_r,ext,remainder [--start] [--end] [--cache-dir] [--out reports/stratb-backtest.json]`; `ewbot stratb walkforward [--workers] [--cache-dir] [--out reports/stratb-walkforward.json]`; `ewbot stratb holdout lock --params-from <walkforward json> [--cache-dir]`; `ewbot stratb holdout run --confirm [--workers] [--cache-dir]`; `ewbot stratb paper run --ledger-dir <dir> [--cache-dir]`; `ewbot stratb paper status --ledger-dir <dir>`. cli.py의 새 이름: `STRATB_LOCK`, `_b_params(text) -> Combo`(그리드 밖이면 종료 코드 2), `_b_df`, `_b_write_json`, `_b_fetch`, `_b_backtest`, `_b_walkforward`, `_b_holdout_lock`, `_b_holdout_run`, `_b_paper_env(args) -> PaperBEnv`(테스트가 바꿔 끼움), `_b_alert_failure`, `_b_paper_run`, `_b_paper_status`, `cmd_stratb`. 잠금 경로는 CLI 옵션이 아니다(항상 `<저장소 루트>/holdout-b.lock`).

명세 3절의 CLI를 `cli.py`에 추가한다(`cli.py`는 A의 지문 대상이 아니다). 연구 명령은 `load_15m`의 가드 때문에 2024 이후 봉을 읽지 못하고, `--start`/`--end`가 홀드아웃에 닿으면 A와 같은 `_requested_window`가 종료 코드 2로 거부한다. `holdout lock`은 워크포워드 JSON의 `proceed`가 참이고 fallback이 아닐 때만 잠금을 쓰고 커밋 명령을 출력한다. `holdout run`은 `--confirm` 없이는 2를 돌려주고, 잠금이 있으면 캐시를 지금까지 늘린 뒤(네트워크) 1회 실행한다. `paper run`의 실패 알림은 A와 같은 방식(실행 로그 링크, 실패 표시 파일)이며 접두어만 `[ewbot paper-B]`다. 수정은 여섯 군데이며 각 '찾을 부분'은 `cli.py`에서 한 번만 나온다.

- [ ] **Step 1: 실패하는 테스트 작성**

연구 명령 테스트(`tests/stratb/`)와 모의매매 명령 테스트(`tests/paperb/`, 세션 픽스처 `lock_text`를 쓰므로 이 폴더)를 만든다.

**파일 `tests/stratb/test_cli_stratb.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.stratb.data15 import REPLAY_ANCHOR, cache_path
from ewbot.stratb.holdout import read_lock
from tests.stratb.helpers import LONG_SETUP, LONG_START, frame_df, path

HS = 1_704_067_200
COMBO = [2.0, [0.382, 0.618], 1.0, 1.272, "breakeven"]


@pytest.fixture
def cache(tmp_path):
    c = tmp_path / "cache"
    c.mkdir()
    frame_df(path(LONG_START, LONG_SETUP * 130), REPLAY_ANCHOR).to_parquet(cache_path(c), index=False)
    return c


def test_the_parser_has_the_spec_subcommands():
    p = build_parser()
    assert p.parse_args(["stratb", "fetch"]).b_cmd == "fetch"
    ns = p.parse_args(["stratb", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven"])
    assert (ns.b_cmd, ns.start, ns.end) == ("backtest", None, None)
    assert p.parse_args(["stratb", "walkforward", "--workers", "2"]).workers == 2
    assert p.parse_args(["stratb", "holdout", "lock", "--params-from", "wf.json"]).bh_cmd == "lock"
    assert p.parse_args(["stratb", "holdout", "run", "--confirm"]).confirm is True
    assert p.parse_args(["stratb", "paper", "run", "--ledger-dir", "x"]).bp_cmd == "run"
    assert p.parse_args(["stratb", "paper", "status", "--ledger-dir", "x"]).bp_cmd == "status"
    for argv in (["stratb", "paper", "run", "--ledger-dir", "x", "--params", "1"], ["stratb", "holdout", "run", "--params", "1"]):
        with pytest.raises(SystemExit):
            p.parse_args(argv)


def test_params_must_be_a_grid_combo():
    assert cli._b_params("3,0.5,0.786,1.5,1.618,original") == (3.0, (0.5, 0.786), 1.5, 1.618, "original")
    with pytest.raises(SystemExit):
        cli._b_params("2.5,0.382,0.618,1,1.272,breakeven")


def test_fetch_reports_the_guarded_cache(tmp_path, cache, monkeypatch, capsys):
    monkeypatch.setattr(cli, "STRATB_LOCK", tmp_path / "none.lock")
    monkeypatch.setattr(cli, "update_cache_15m", lambda c, lock_path: cache_path(c))
    assert main(["stratb", "fetch", "--cache-dir", str(cache)]) == 0
    out = capsys.readouterr().out
    assert "bars before 2024-01-01: 8832" in out and "stops at 2024-01-01 (no lock)" in out


def test_backtest_writes_json_and_never_reaches_into_2024(tmp_path, cache, capsys):
    out = tmp_path / "bt.json"
    assert main(["stratb", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven", "--cache-dir", str(cache),
                 "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["params"] == COMBO and res["window"][1] == HS and res["metrics"]["n_trades"] == len(res["trades"]) > 0
    assert all(t["entry_ts"] < HS for t in res["trades"])
    with pytest.raises(SystemExit) as exc:
        main(["stratb", "backtest", "--params", "2,0.382,0.618,1,1.272,breakeven", "--cache-dir", str(cache),
              "--end", "2024-02-01"])
    assert exc.value.code == 2


def test_walkforward_writes_json_and_says_whether_to_proceed(tmp_path, cache, monkeypatch, capsys):
    def fake(df, workers):
        assert int(df["ts"].max()) < HS
        return {"folds": [], "stitched_metrics": {k: 0.0 for k in ("total_return", "cagr", "max_dd", "sharpe", "sortino",
                "calmar", "win_rate", "avg_r")} | {"n_trades": 0, "exposure": 0.0}, "oos_trades": [],
                "chosen_final": None, "chosen_final_fallback": False, "proceed": False}

    monkeypatch.setattr(cli, "walk_forward_b", fake)
    out = tmp_path / "wf.json"
    assert main(["stratb", "walkforward", "--cache-dir", str(cache), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["proceed"] is False and "oos_trades" not in json.loads(out.read_text())
    assert "strategy B ends here" in capsys.readouterr().err


def test_holdout_lock_needs_a_walk_forward_that_proceeds(tmp_path, cache, monkeypatch, capsys):
    lock_path = tmp_path / "holdout-b.lock"
    monkeypatch.setattr(cli, "STRATB_LOCK", lock_path)
    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps({"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": False}))
    with pytest.raises(SystemExit) as exc:
        main(["stratb", "holdout", "lock", "--params-from", str(wf), "--cache-dir", str(cache)])
    assert exc.value.code == 2 and not lock_path.exists()
    wf.write_text(json.dumps({"chosen_final": COMBO, "chosen_final_fallback": False, "proceed": True}))
    assert main(["stratb", "holdout", "lock", "--params-from", str(wf), "--cache-dir", str(cache)]) == 0
    lock = read_lock(lock_path)
    assert lock["params"] == COMBO and lock["random_runs"] == 1000 and lock["params_source"].startswith("walkforward:")
    assert "Commit it NOW" in capsys.readouterr().out


def test_holdout_run_needs_confirm_and_a_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "STRATB_LOCK", tmp_path / "holdout-b.lock")
    assert main(["stratb", "holdout", "run"]) == 2
    assert "--confirm" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main(["stratb", "holdout", "run", "--confirm"])
    assert exc.value.code == 2
```

**파일 `tests/paperb/test_cli_paper_b.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

from pathlib import Path

import pytest

from ewbot import cli
from ewbot.cli import build_parser, main
from ewbot.paperb.notify import HEAD_B
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH
from tests.paperb.helpers import LAST_BAR, make_env


@pytest.fixture
def senders(tmp_path, monkeypatch, lock_text):
    built = []

    def factory(args):
        env, _, sender = make_env(tmp_path / "w", lock_text, LAST_BAR + 900 + 3 * 86_400)
        env.ledger_dir = args.ledger_dir
        built.append((env, sender))
        return env

    monkeypatch.setattr(cli, "_b_paper_env", factory)
    return built


def test_production_wiring_uses_the_repo_lock_and_env_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = cli._b_paper_env(build_parser().parse_args(["stratb", "paper", "run", "--ledger-dir", str(tmp_path)]))
    assert env.lock_path == DEFAULT_LOCK_PATH and DEFAULT_LOCK_PATH.name == "holdout-b.lock" and env.sender is None
    assert env.ledger_dir == tmp_path and env.cache_dir == Path("data/cache")


def test_paper_b_run_then_status(senders, tmp_path, capsys):
    ledger_dir = tmp_path / "paper-b"
    assert main(["stratb", "paper", "run", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("paper-B run ") and "epoch 1" in out and "/30" in out
    assert main(["stratb", "paper", "status", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "실거래 전환 조건 (Plan 4 명세 7.1)" in out and "1. 홀드아웃 합격: 충족" in out
    assert "holdout trades recomputed 62 (lock: 62)" in out and "discrepancies 0" in out


def test_a_failed_run_alerts_with_the_paper_b_prefix(senders, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "notified"
    monkeypatch.setenv("EWBOT_RUN_URL", "https://github.com/o/r/actions/runs/9")
    monkeypatch.setenv("EWBOT_FAILURE_MARKER", str(marker))
    real = cli._b_paper_env

    def failing(args):
        env = real(args)
        env.fetch.fail = 3
        return env

    monkeypatch.setattr(cli, "_b_paper_env", failing)
    assert main(["stratb", "paper", "run", "--ledger-dir", str(tmp_path / "pb")]) == 1
    assert "failed at stage data" in capsys.readouterr().err
    (_, sender), = senders
    assert sender.texts == [f"{HEAD_B} 실패 · 단계 data\ncandle update failed 3 times: ConnectionError: coinbase down\n"
                            "로그: https://github.com/o/r/actions/runs/9"]
    assert marker.read_text() == "data" and not (tmp_path / "pb" / "ledger.json").exists()


def test_status_without_a_ledger_fails(tmp_path, capsys):
    assert main(["stratb", "paper", "status", "--ledger-dir", str(tmp_path)]) == 1
    assert "no ledger.json" in capsys.readouterr().err
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/stratb/test_cli_stratb.py tests/paperb/test_cli_paper_b.py -q
```

예상: `9 failed, 2 errors`

`stratb` 하위 명령과 `_b_…` 함수가 아직 없어 실패한다.

- [ ] **Step 3: 구현**

`src/ewbot/cli.py`를 여섯 군데 고친다(모듈 docstring, import, 파서, 처리 함수, 명령 표).

**수정 `src/ewbot/cli.py`** (1/6) — 다음 부분을 찾아:

```python
"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout | paper | live.
```

바꿀 내용:

```python
"""`ewbot` command line: fetch | backtest | compare | walkforward | holdout | paper | live | stratb.
```

**수정 `src/ewbot/cli.py`** (2/6) — 다음 부분을 찾아:

```python
inspects the live-trading runtime (ewbot.live); like `paper` it has no strategy or config options."""
```

바꿀 내용:

```python
inspects the live-trading runtime (ewbot.live); like `paper` it has no strategy or config options.
`stratb fetch | backtest | walkforward | holdout lock | holdout run | paper run | paper status` (Plan 4) is
strategy B (ewbot.stratb, ewbot.paperb) with its own 15m cache, lock (holdout-b.lock) and ledger (paper-b
branch); its research commands never read a bar from 2024-01-01 on (data15 filters them out)."""
```

**수정 `src/ewbot/cli.py`** (3/6) — 다음 부분을 찾아:

```python
from ewbot.types import H1
```

바꿀 내용:

```python
from ewbot.paperb.ledger import paper_count_b
from ewbot.paperb.notify import b_head
from ewbot.paperb.runner import PaperBEnv, PaperBRunError, run_paper_b, status_lines_b
from ewbot.stratb.config import combo_to_json, config_for, normalize_combo
from ewbot.stratb.data15 import DEFAULT_LOCK_PATH as STRATB_LOCK
from ewbot.stratb.data15 import cache_path as b_cache_path
from ewbot.stratb.data15 import gap_report, load_15m, lock_exists, update_cache_15m
from ewbot.stratb.holdout import HoldoutBError, create_lock, run_holdout_b
from ewbot.stratb.research import backtest_b, walk_forward_b
from ewbot.types import H1
```

**수정 `src/ewbot/cli.py`** (4/6) — 다음 부분을 찾아:

```python
    p_bk.add_argument("--out", type=Path, required=True)
    return parser
```

바꿀 내용:

```python
    p_bk.add_argument("--out", type=Path, required=True)

    p_b = sub.add_parser("stratb", help="strategy B (Plan 4): 15m data, research, one-shot holdout, paper trading")
    b_sub = p_b.add_subparsers(dest="b_cmd", required=True)
    b_cache = argparse.ArgumentParser(add_help=False)
    b_cache.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    b_sub.add_parser("fetch", parents=[b_cache], help="update the 15m cache (stops before 2024 until holdout-b.lock exists)")
    p_bb = b_sub.add_parser("backtest", parents=[b_cache], help="one strategy-B backtest before 2024")
    p_bb.add_argument("--params", required=True, help="zz_k,z_lo,z_hi,tp1_r,ext,remainder e.g. 2.0,0.382,0.618,1.0,1.272,breakeven")
    p_bb.add_argument("--start", default=None, help="UTC date YYYY-MM-DD (default: first bar)")
    p_bb.add_argument("--end", default=None, help=f"UTC date YYYY-MM-DD, exclusive, at most {WF_END}")
    p_bb.add_argument("--out", type=Path, default=Path("reports/stratb-backtest.json"))
    p_bw = b_sub.add_parser("walkforward", parents=[b_cache], help="32-combo walk-forward 2017-01..2023-12 (A's rules)")
    p_bw.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_bw.add_argument("--out", type=Path, default=Path("reports/stratb-walkforward.json"))
    p_bh = b_sub.add_parser("holdout", help="holdout-b.lock and the ONE-SHOT strategy-B holdout")
    bh_sub = p_bh.add_subparsers(dest="bh_cmd", required=True)
    p_bhl = bh_sub.add_parser("lock", parents=[b_cache], help="write holdout-b.lock from the walk-forward (commit it)")
    p_bhl.add_argument("--params-from", type=Path, required=True, help="reports/stratb-walkforward.json")
    p_bhr = bh_sub.add_parser("run", parents=[b_cache], help="run the holdout ONCE (the user's approval: --confirm)")
    p_bhr.add_argument("--confirm", action="store_true")
    p_bhr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_bp = b_sub.add_parser("paper", help="paper trading of strategy B (ledger on the paper-b branch)")
    bp_sub = p_bp.add_subparsers(dest="bp_cmd", required=True)
    p_bpr = bp_sub.add_parser("run", parents=[b_cache], help="one paper-B run; params come from holdout-b.lock only")
    p_bpr.add_argument("--ledger-dir", type=Path, required=True)
    p_bps = bp_sub.add_parser("status", help="print the paper-B ledger's status (read-only, no network)")
    p_bps.add_argument("--ledger-dir", type=Path, required=True)
    return parser
```

**수정 `src/ewbot/cli.py`** (5/6) — 다음 부분을 찾아:

```python


_COMMANDS = {
```

바꿀 내용:

```python


# ----------------------------------------------------------------------------- strategy B (Plan 4)


def _b_params(text: str) -> tuple:
    """"zz_k,z_lo,z_hi,tp1_r,ext,remainder" -> the grid combo; exit 2 when it is not one."""
    parts = [x.strip() for x in text.split(",")]
    try:
        if len(parts) != 6:
            raise ValueError(f"expected 6 values, got {len(parts)}")
        return normalize_combo([parts[0], [parts[1], parts[2]], parts[3], parts[4], parts[5]])
    except ValueError as exc:
        _refuse(f"--params {text}: {exc}")


def _b_df(args: argparse.Namespace) -> pd.DataFrame:
    """The 15m bars before 2024-01-01 (data15 filters the rest out while reading)."""
    path = b_cache_path(args.cache_dir)
    if not path.exists():
        raise SystemExit(f"15m cache not found: {path} - run `ewbot stratb fetch` first")
    df = load_15m(path)
    rep = gap_report(df)
    print(f"[data] {rep.n} 15m bars before {WF_END}, {len(rep.missing_ts)} missing", file=sys.stderr)
    return df


def _b_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(obj), indent=1), encoding="utf-8")


def _b_fetch(args: argparse.Namespace) -> int:
    path = update_cache_15m(args.cache_dir, lock_path=STRATB_LOCK)
    rep = gap_report(load_15m(path))
    unlocked = "2024+ included (holdout-b.lock exists)" if lock_exists(STRATB_LOCK) else f"stops at {WF_END} (no lock)"
    print(f"cache: {path}\nbars before {WF_END}: {rep.n}  missing: {len(rep.missing_ts)}  duplicate: "
          f"{len(rep.duplicate_ts)}\nfetch window: {unlocked}")
    return 0


def _b_backtest(args: argparse.Namespace) -> int:
    combo = _b_params(args.params)
    start, end = _requested_window(args)
    df = _b_df(args)
    start = start if start is not None else int(df["ts"].iloc[0])
    end = end if end is not None else int(df["ts"].iloc[-1]) + 900
    res = backtest_b(df, config_for(combo), start, end)
    m = compute_metrics(res.equity, res.trades, start, end)
    _print_comparison({"B": dataclasses.asdict(m)})
    _b_write_json(args.out, {"params": combo_to_json(combo), "window": [start, end], "metrics": m,
                             "trades": list(res.trades), "events": list(res.events)})
    print(f"json: {args.out}")
    return 0


def _b_walkforward(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    wf = walk_forward_b(_b_df(args), args.workers)
    _b_write_json(args.out, {k: v for k, v in wf.items() if k != "oos_trades"})
    _print_comparison({"WF-OOS": wf["stitched_metrics"]})
    chosen = None if wf["chosen_final"] is None else combo_to_json(wf["chosen_final"])
    print(f"walk-forward B: {len(wf['folds'])} folds in {time.perf_counter() - t0:.0f}s; chosen_final = {chosen}; "
          f"fallback = {wf['chosen_final_fallback']}; proceed = {wf['proceed']}; json: {args.out}")
    if not wf["proceed"]:
        print("spec 6.3: no holdout - the stitched out-of-sample return is negative or there is no final combo "
              "from the last fold; strategy B ends here", file=sys.stderr)
    return 0


def _b_holdout_lock(args: argparse.Namespace) -> int:
    wf = json.loads(Path(args.params_from).read_text(encoding="utf-8"))
    if not wf.get("proceed") or not wf.get("chosen_final") or wf.get("chosen_final_fallback"):
        _refuse(f"{args.params_from}: proceed is false (spec 6.3: negative out-of-sample, fallback or no pick); "
                "strategy B ends without a holdout")
    try:
        lock = create_lock(wf["chosen_final"], _b_df(args), params_source=f"walkforward:{args.params_from}#chosen_final",
                           now_ts=int(time.time()), lock_path=STRATB_LOCK)
    except HoldoutBError as exc:
        _refuse(str(exc))
    print(f"holdout-b.lock written: {STRATB_LOCK} (sha256 {lock['sha256'][:12]}, params {lock['params']}, "
          f"last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(lock['last_bar_ts']))} UTC).\n"
          "Commit it NOW, before the holdout runs:\n"
          f'  git add "{STRATB_LOCK}"\n  git commit -m "chore(stratb): lock the strategy-B holdout {lock["sha256"][:12]}"')
    return 0


def _b_holdout_run(args: argparse.Namespace) -> int:
    if not args.confirm:
        print("refusing: the strategy-B holdout runs once, after the user's approval: re-run with --confirm",
              file=sys.stderr)
        return 2
    if not lock_exists(STRATB_LOCK):
        _refuse(f"{STRATB_LOCK} does not exist: `ewbot stratb holdout lock` first")
    update_cache_15m(args.cache_dir, lock_path=STRATB_LOCK)
    try:
        out = run_holdout_b(confirm=True, cache_dir=args.cache_dir, lock_path=STRATB_LOCK, workers=args.workers)
    except HoldoutBError as exc:
        _refuse(str(exc))
    fr = out["first_run"]
    _print_comparison({k: fr["metrics"][k] for k in ("B", "BH", "B_cost_x1_5")})
    print(json.dumps(fr["criteria"], indent=1))
    print(f"holdout {'PASSED' if fr['criteria']['passed'] else 'FAILED - strategy B ends (spec 6.5)'}; result in "
          f"{STRATB_LOCK} first_run. Commit it:\n  git add \"{STRATB_LOCK}\"\n"
          '  git commit -m "chore(stratb): record the strategy-B holdout run"')
    return 0


def _b_paper_env(args: argparse.Namespace) -> PaperBEnv:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    return PaperBEnv(ledger_dir=args.ledger_dir, cache_dir=args.cache_dir, sender=sender_from_env())


def _b_alert_failure(env: PaperBEnv, stage: str, message: str) -> None:
    if env.sender is None:
        return
    text = b_head(failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([make_event("FAILURE", env.clock(), stage, text)], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _b_paper_run(args: argparse.Namespace) -> int:
    env = _b_paper_env(args)
    try:
        out = run_paper_b(env)
    except PaperBRunError as exc:
        print(f"paper-B run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _b_alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _b_alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    print(f"paper-B run {out.run_id}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(out.last_bar_ts))} UTC, "
          f"epoch {out.epoch_id}, paper {out.count['paper']}/{out.count['required']}, {out.new_decisions} new "
          f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending")
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _b_paper_status(args: argparse.Namespace) -> int:
    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    for line in status_lines_b(led, paper_count_b(led)):
        print(line)
    print(position_line(led.open_position))
    cur = current_epoch(led)
    print(f"epoch {cur['id']} of {len(led.epochs)}: B fingerprint {cur['fingerprint'][:12]} "
          f"(lock {led.baseline['fingerprint'][:12]})")
    if led.runs:
        r = led.runs[-1]
        print(f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
              f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['B']})")
    print(f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)}, "
          f"pending events {len(led.pending_events)}")
    return 0


def cmd_stratb(args: argparse.Namespace) -> int:
    if args.b_cmd == "holdout":
        return _b_holdout_lock(args) if args.bh_cmd == "lock" else _b_holdout_run(args)
    if args.b_cmd == "paper":
        return _b_paper_status(args) if args.bp_cmd == "status" else _b_paper_run(args)
    return {"fetch": _b_fetch, "backtest": _b_backtest, "walkforward": _b_walkforward}[args.b_cmd](args)


_COMMANDS = {
```

**수정 `src/ewbot/cli.py`** (6/6) — 다음 부분을 찾아:

```python
    "live": cmd_live,
}
```

바꿀 내용:

```python
    "live": cmd_live,
    "stratb": cmd_stratb,
}
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/stratb/test_cli_stratb.py tests/paperb/test_cli_paper_b.py tests/backtest/test_cli.py tests/paper/test_cli_paper.py -q
```

예상: `37 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/cli.py tests/stratb/test_cli_stratb.py tests/paperb/test_cli_paper_b.py
git commit -m "feat(cli): ewbot stratb fetch | backtest | walkforward | holdout lock | holdout run | paper run | paper status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: `paper-b` 동기화, 워크플로, CI 보호 검사, 운영 문서

**Files:**
- Create: `src/ewbot/paperb/sync.py`
- Create: `.github/workflows/paper-b.yml`
- Create: `docs/strategy-b.md`
- Modify: `README.md` (5곳)
- Modify: `tests/paper/test_static.py` (1곳)
- Test: `tests/paperb/test_sync.py`
- Test: `tests/paperb/test_workflow.py`

**Interfaces:**
- Consumes: Task 10·11: `ewbot stratb paper run --ledger-dir`. A의 `paper.yml`(구조만 따름), A의 `tests/paper/test_static.py`의 `SRC`, `WORKFLOWS`, `yaml`.
- Produces: `ewbot.paperb.sync`: `BRANCH = "paper-b"`, `LEDGER_FILES = ("ledger.json", "runs.log")`, `ATTEMPTS = 3`, `class SyncError(RuntimeError)`, `origin_branch(cwd) -> str`, `checkout(repo, paper_dir) -> None`, `commit_ledger(paper_dir) -> bool`, `push(paper_dir, recompute, *, attempts=3, backoff=5.0, sleep=time.sleep) -> str`, `main(argv=None) -> int`(`python -m ewbot.paperb.sync checkout | push RECOMPUTE...`, 환경 변수 `PAPER_B_DIR`, `PAPER_B_PUSH_BACKOFF`). `.github/workflows/paper-b.yml`: `workflow_dispatch` + `schedule: "47 */3 * * *"`, 동시성 그룹 `paper-b`, `if: vars.PAPER_B_ENABLED == 'true'`, 캐시 키 `btc-usd-15m-`, 텔레그램 시크릿은 실행·푸시·대체 알림 단계에만. `docs/strategy-b.md`: 운영 안내.

A의 `paper.yml` 구조를 따르는 별도 워크플로를 만든다(해석 15~17). 이 작업에서 `tests/paper/test_static.py`를 고쳐야 한다: 이 파일의 `test_the_repository_has_exactly_the_paper_and_ci_workflows`는 워크플로가 정확히 `ci.yml`과 `paper.yml`뿐이라고 단언하므로 `paper-b.yml`을 더하면 `ci.yml`이 실패한다. 이 한 테스트를 세 파일로 바꾸고, `ci.yml`이 실행하는 파일이 이것뿐이므로 B 보호 검사 두 개(B 파일이 A 지문 밖, `paper-b.yml`이 A의 것을 건드리지 않음)를 여기에 더한다. A의 다른 테스트와 `ci.yml` 자체는 바꾸지 않는다. 동기화 모듈은 A의 `scripts/paper_sync.sh`와 같은 규칙(고아 브랜치, 얕은 가져오기, 3회 푸시, 원격이 움직였을 때만 재계산)을 `paper-b`에 적용한다. 문서는 연구·잠금·승인·홀드아웃 순서, 휴면 해제(`PAPER_B_ENABLED`), 두 번째 Cloudflare Worker 코드, Actions 사용량 확인, 실패 대응을 담는다.

- [ ] **Step 1: 실패하는 테스트 작성**

동기화 테스트(로컬 bare 저장소: A의 `paper` 브랜치가 이미 있는 원격, 다른 작성자, pre-push 훅), 워크플로 정적 테스트를 만들고 `tests/paper/test_static.py`를 고친다.

**파일 `tests/paperb/test_sync.py`** (새 파일, 전체 내용)

```python
"""ewbot.paperb.sync against local bare repositories (no network): a bare origin that already has
strategy A's `paper` branch, the workflow's master checkout, the paper-b worktree and another writer."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from ewbot.paperb import sync
from ewbot.paperb.sync import BRANCH, SyncError, checkout, commit_ledger, push


def _git_version() -> tuple[int, ...]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    return tuple(int(x) for x in re.findall(r"\d+", out)[:2])


pytestmark = pytest.mark.skipif(_git_version() < (2, 42), reason="needs git >= 2.42 (worktree --orphan)")


def g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = master\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin, work, other = tmp_path / "origin.git", tmp_path / "work", tmp_path / "other"
    g(tmp_path, "init", "-q", "--bare", str(origin))
    g(tmp_path, "init", "-q", str(work))
    (work / "README.md").write_text("code\n")
    g(work, "add", "README.md")
    g(work, "commit", "-q", "-m", "code")
    g(work, "remote", "add", "origin", str(origin))
    g(work, "push", "-q", "origin", "master")
    g(work, "switch", "-q", "--orphan", "paper")  # strategy A's ledger branch exists on origin
    (work / "ledger.json").write_text('{"a": 1}\n')
    g(work, "add", "ledger.json")
    g(work, "commit", "-q", "-m", "a ledger")
    g(work, "push", "-q", "origin", "paper")
    g(work, "switch", "-q", "master")
    g(tmp_path, "clone", "-q", str(origin), str(other))
    return {"origin": origin, "work": work, "other": other, "paper": tmp_path / "rt" / "paper-b", "tmp": tmp_path}


def _write(d, n):
    (d / "ledger.json").write_text(f'{{"run": {n}}}\n')
    with open(d / "runs.log", "a") as fh:
        fh.write(f"run {n}\n")


def _other_writer(w):
    d = w["other"]
    g(d, "fetch", "-q", "origin")
    g(d, "checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}")
    _write(d, 99)
    g(d, "add", "ledger.json", "runs.log")
    g(d, "commit", "-q", "-m", "other")
    g(d, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")


def test_checkout_starts_an_orphan_paper_b_then_tracks_it_and_leaves_paper_alone(world):
    a_before = g(world["origin"], "rev-parse", "refs/heads/paper")
    checkout(world["work"], world["paper"])
    assert g(world["paper"], "branch", "--show-current") == BRANCH
    _write(world["paper"], 1)
    assert push(world["paper"], lambda: 0, backoff=0).startswith("pushed")
    files = g(world["origin"], "ls-tree", "--name-only", f"refs/heads/{BRANCH}").split()
    assert sorted(files) == ["ledger.json", "runs.log"]
    assert g(world["origin"], "rev-parse", "refs/heads/paper") == a_before
    g(world["work"], "worktree", "remove", "--force", str(world["paper"]))
    checkout(world["work"], world["paper"])
    assert (world["paper"] / "runs.log").read_text() == "run 1\n"


def test_an_unreadable_origin_never_starts_a_new_branch(world):
    g(world["work"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    with pytest.raises(SyncError, match="not starting a new paper-b"):
        checkout(world["work"], world["paper"])


def test_nothing_to_commit_is_a_success(world):
    checkout(world["work"], world["paper"])
    _write(world["paper"], 1)
    push(world["paper"], lambda: 0, backoff=0)
    assert push(world["paper"], lambda: 0, backoff=0) == "no ledger change: nothing to push"
    assert commit_ledger(world["paper"]) is False


def test_a_moved_origin_means_one_recompute_on_top_of_it(world):
    checkout(world["work"], world["paper"])
    _write(world["paper"], 1)
    push(world["paper"], lambda: 0, backoff=0)
    _write(world["paper"], 2)
    _other_writer(world)
    calls = []

    def recompute():
        calls.append(1)
        _write(world["paper"], 3)
        return 0

    out = push(world["paper"], recompute, backoff=0)
    assert out.startswith("pushed") and len(calls) == 1
    log = g(world["origin"], "show", f"refs/heads/{BRANCH}:runs.log").split("\n")
    assert log == ["run 1", "run 99", "run 3"]


def test_failed_pushes_retry_the_same_commit_and_give_up_after_three(world, monkeypatch):
    checkout(world["work"], world["paper"])
    hooks = world["tmp"] / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    (hooks / "pre-push").chmod(0o755)
    g(world["paper"], "config", "core.hooksPath", str(hooks))
    _write(world["paper"], 1)
    waits = []
    with pytest.raises(SyncError, match="push failed 3 times"):
        push(world["paper"], lambda: pytest.fail("no recompute when origin did not move"), backoff=2.0,
             sleep=waits.append)
    assert waits == [2.0, 4.0]


def test_main_needs_paper_b_dir_and_a_known_command(world, monkeypatch, capsys):
    monkeypatch.delenv("PAPER_B_DIR", raising=False)
    assert sync.main(["checkout"]) == 1 and "PAPER_B_DIR is not set" in capsys.readouterr().err
    monkeypatch.setenv("PAPER_B_DIR", str(world["paper"]))
    monkeypatch.chdir(world["work"])
    assert sync.main(["checkout"]) == 0
    _write(world["paper"], 1)
    monkeypatch.setenv("PAPER_B_PUSH_BACKOFF", "0")
    assert sync.main(["push", sys.executable, "-c", "pass"]) == 0
    assert sync.main(["status"]) == 1
```

**파일 `tests/paperb/test_workflow.py`** (새 파일, 전체 내용)

```python
"""Static checks of .github/workflows/paper-b.yml: the shape of strategy A's paper.yml, and nothing of A's."""

from __future__ import annotations

from pathlib import Path

import yaml

import ewbot

ROOT = Path(ewbot.__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "paper-b.yml"
SETUP_UV = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
SECRETS = {"TELEGRAM_BOT_TOKEN": "${{ secrets.TELEGRAM_BOT_TOKEN }}", "TELEGRAM_CHAT_ID": "${{ secrets.TELEGRAM_CHAT_ID }}"}
RUN = 'uv run ewbot stratb paper run --ledger-dir "$PAPER_B_DIR"'


def _wf():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper-b"]["steps"]


def _step(steps, step_id):
    return next(s for s in steps if s.get("id") == step_id)


def test_triggers_concurrency_permissions_and_runner():
    wf, _ = _wf()
    assert wf[True] == {"schedule": [{"cron": "47 */3 * * *"}], "workflow_dispatch": None}
    assert wf["concurrency"] == {"group": "paper-b", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "write"} and wf["jobs"]["paper-b"]["runs-on"] == "ubuntu-24.04"
    assert wf["jobs"]["paper-b"]["if"] == "vars.PAPER_B_ENABLED == 'true'"  # dormant until a passed holdout


def test_steps_run_the_b_runtime_and_its_sync():
    wf, steps = _wf()
    assert [s["uses"] for s in steps if "uses" in s] == ["actions/checkout@v7", SETUP_UV, "actions/cache@v6"]
    assert steps[0]["with"] == {"ref": "master"}
    assert _step(steps, "paper-b")["run"] == RUN
    assert _step(steps, "push")["run"] == f"uv run python -m ewbot.paperb.sync push {RUN}"
    assert "uv run python -m ewbot.paperb.sync checkout" in _step(steps, "checkout-paper-b")["run"]
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "btc-usd-15m-"
    budget = sum(s.get("timeout-minutes", 0) for s in steps)
    assert wf["jobs"]["paper-b"]["timeout-minutes"] >= budget + 10


def test_secrets_reach_only_the_steps_that_send_and_the_alert_has_the_b_prefix():
    _, steps = _wf()
    with_secrets = [s.get("id") for s in steps if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["paper-b", "push", "alert"]
    for i in with_secrets:
        assert {k: v for k, v in _step(steps, i)["env"].items() if "secrets." in v} == SECRETS
    alert = _step(steps, "alert")
    assert alert["if"] == "failure() || cancelled()" and "[ewbot paper-B] 실패" in alert["run"]


def test_nothing_of_strategy_a_is_referenced():
    text = WORKFLOW.read_text(encoding="utf-8")
    for a_thing in ("paper_sync.sh", "btc-usd-1h", "ewbot paper run", "group: paper\n", "refs/heads/paper\n", "holdout.lock"):
        assert a_thing not in text
```

**수정 `tests/paper/test_static.py`** (1/1) — 다음 부분을 찾아:

```python
def test_the_repository_has_exactly_the_paper_and_ci_workflows():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper.yml"]
```

바꿀 내용:

```python
def test_the_repository_has_exactly_the_paper_ci_and_paper_b_workflows():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper-b.yml", "paper.yml"]


def test_strategy_b_code_is_outside_the_strategy_a_fingerprint():
    """Plan 4 spec 2, 9 (A protection, run by ci.yml): strategy B lives in stratb/ and paperb/ only, and
    none of its files is in A's fingerprinted set (A's fingerprint itself is pinned in test_fingerprint.py)."""
    from ewbot.paper.fingerprint import is_fingerprinted, tree_files

    b_files = [f.relative_to(SRC).as_posix() for d in ("stratb", "paperb") for f in (SRC / d).rglob("*.py")]
    assert b_files and not [r for r in b_files if is_fingerprinted(r)]
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratb/", "src/ewbot/paperb/"))]


def test_the_paper_b_workflow_never_touches_strategy_a():
    text = (WORKFLOWS / "paper-b.yml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["concurrency"]["group"] == "paper-b"
    for a_thing in ("paper_sync.sh", "btc-usd-1h", "ewbot paper run", "holdout.lock"):
        assert a_thing not in text
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/paperb/test_sync.py tests/paperb/test_workflow.py tests/paper/test_static.py -q
```

예상: `cannot import name 'sync' from 'ewbot.paperb'`
예상: `1 error`

`ewbot.paperb.sync`가 없어 수집 단계에서 실패한다.

- [ ] **Step 3: 구현**

동기화 모듈, 워크플로, 운영 문서를 만들고 `README.md`를 고친다.

**파일 `src/ewbot/paperb/sync.py`** (새 파일, 전체 내용)

```python
"""Git side of .github/workflows/paper-b.yml (Plan 4 spec 7): strategy A's scripts/paper_sync.sh rules
for the orphan branch `paper-b` (A's script and its `paper` branch are never touched). Run from the master
checkout:

  python -m ewbot.paperb.sync checkout
      worktree of origin's paper-b at $PAPER_B_DIR (shallow), or an orphan paper-b when origin has none
      (ls-remote exit 2); any other ls-remote failure fails: a network or auth error must not start a
      fresh ledger.
  python -m ewbot.paperb.sync push RECOMPUTE...
      commit ledger.json and runs.log in $PAPER_B_DIR and push to origin's paper-b, 3 pushes at most.
      After a failed push: origin unmoved or unreadable -> the same commit again after a wait (no
      recompute, so no Telegram message twice); origin already at our commit -> done; origin moved ->
      reset to it, run RECOMPUTE (`uv run ewbot stratb paper run ...`), commit and push on top. Never
      recomputes after the last push; fails when every push failed.
Environment: PAPER_B_DIR (required); PAPER_B_PUSH_BACKOFF = seconds, retry n waits n times it (default 5)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

BRANCH = "paper-b"
LEDGER_FILES = ("ledger.json", "runs.log")
ATTEMPTS = 3


class SyncError(RuntimeError):
    """A git step of the paper-B sync failed."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def origin_branch(cwd: Path) -> str:
    """The sha of origin's paper-b, "" when origin has none; SyncError on any other ls-remote failure."""
    r = _git(cwd, "ls-remote", "--exit-code", "origin", f"refs/heads/{BRANCH}", check=False)
    if r.returncode == 0:
        return r.stdout.split()[0]
    if r.returncode == 2:
        return ""
    raise SyncError(f"git ls-remote origin failed (exit {r.returncode}): not starting a new {BRANCH} branch")


def checkout(repo: Path, paper_dir: Path) -> None:
    if origin_branch(repo):
        _git(repo, "fetch", "--no-tags", "--depth=1", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
        _git(repo, "worktree", "add", "-B", BRANCH, str(paper_dir), f"origin/{BRANCH}")
    else:
        print(f"origin has no {BRANCH} branch: starting an orphan one")
        _git(repo, "worktree", "add", "--orphan", "-b", BRANCH, str(paper_dir))


def commit_ledger(paper_dir: Path) -> bool:
    """Commit the ledger files; False when nothing changed."""
    _git(paper_dir, "add", "--", *[f for f in LEDGER_FILES if (paper_dir / f).exists()])
    if _git(paper_dir, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(paper_dir, "commit", "-q", "-m", f"paper-b: run {datetime.now(UTC).strftime('%Y-%m-%dT%H:%MZ')}")
    return True


def push(
    paper_dir: Path,
    recompute: Callable[[], int],
    *,
    attempts: int = ATTEMPTS,
    backoff: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    if not commit_ledger(paper_dir):
        return "no ledger change: nothing to push"
    for attempt in range(1, attempts + 1):
        if _git(paper_dir, "push", "origin", f"HEAD:refs/heads/{BRANCH}", check=False).returncode == 0:
            return f"pushed {_git(paper_dir, 'rev-parse', '--short', 'HEAD').stdout.strip()} (attempt {attempt})"
        head = _git(paper_dir, "rev-parse", "HEAD").stdout.strip()
        parent = _git(paper_dir, "rev-parse", "-q", "--verify", "HEAD^", check=False).stdout.strip()
        try:
            remote: str | None = origin_branch(paper_dir)
        except SyncError:
            remote = None
        if remote == head:
            return f"origin/{BRANCH} is at our commit: the push landed despite the error"
        if attempt >= attempts:
            raise SyncError(f"push failed {attempts} times")
        if remote is None or remote == parent:
            sleep(backoff * attempt)
            continue
        if not remote:
            raise SyncError(f"origin's {BRANCH} branch disappeared")
        print(f"push rejected (attempt {attempt}): origin/{BRANCH} moved to {remote[:12]}, recompute on top of it")
        _git(paper_dir, "fetch", "--no-tags", "--depth=1", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
        _git(paper_dir, "reset", "-q", "--hard", f"origin/{BRANCH}")
        rc = recompute()
        if rc != 0:
            raise SyncError(f"recompute failed (exit {rc})")
        if not commit_ledger(paper_dir):
            return "no ledger change after the recompute: nothing to push"
    raise SyncError(f"push failed {attempts} times")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paper_dir = os.environ.get("PAPER_B_DIR", "")
    try:
        if not paper_dir:
            raise SyncError("PAPER_B_DIR is not set")
        if args == ["checkout"]:
            checkout(Path.cwd(), Path(paper_dir))
            return 0
        if len(args) > 1 and args[0] == "push":
            backoff = float(os.environ.get("PAPER_B_PUSH_BACKOFF", "5"))
            print(push(Path(paper_dir), lambda: subprocess.run(args[1:]).returncode, backoff=backoff))
            return 0
    except SyncError as exc:
        print(f"paper-b sync: {exc}", file=sys.stderr)
        return 1
    print("usage: python -m ewbot.paperb.sync checkout | push RECOMPUTE...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

**파일 `.github/workflows/paper-b.yml`** (새 파일, 전체 내용)

```yaml
# ewbot paper trading of strategy B (Plan 4 spec 7): one deterministic recompute per run.
# Code comes from master; ledger.json / runs.log live on the orphan `paper-b` branch, which only this
# workflow writes (docs/strategy-b.md: never push to it by hand). Strategy A's workflow (paper.yml), its
# `paper` branch, its sync script and its candle cache key are never touched.
# The git side (paper-b worktree, commit, push and retry) is `python -m ewbot.paperb.sync`.
name: paper-b

# Primary trigger: a Cloudflare Workers cron calls workflow_dispatch at minute 47 of every hour
# (docs/strategy-b.md, user step). `schedule` is only a sparse backup (minute 47, every 3 hours).
on:
  schedule:
    - cron: "47 */3 * * *"
  workflow_dispatch:

concurrency:
  group: paper-b
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  paper-b:
    # Dormant until the user sets the repository variable PAPER_B_ENABLED=true after a passed holdout
    # (docs/strategy-b.md): a skipped job uses no runner minutes and sends nothing.
    if: vars.PAPER_B_ENABLED == 'true'
    runs-on: ubuntu-24.04
    timeout-minutes: 60
    env:
      EWBOT_RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
    steps:
      - name: Check out master
        uses: actions/checkout@v7
        with:
          ref: master

      - name: Install uv (with its cache)
        uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          python-version: "3.12.13"
          enable-cache: true
          cache-dependency-glob: uv.lock

      - name: Sync dependencies
        timeout-minutes: 10
        run: uv sync --frozen

      - name: Check out the paper-b branch (orphan on first use)
        id: checkout-paper-b
        timeout-minutes: 5
        run: |
          set -euo pipefail
          echo "PAPER_B_DIR=$RUNNER_TEMP/paper-b" >> "$GITHUB_ENV"
          echo "EWBOT_FAILURE_MARKER=$RUNNER_TEMP/ewbot-b-failure-notified" >> "$GITHUB_ENV"
          git config --global user.name "ewbot-paper-b"
          git config --global user.email "ewbot-paper-b@users.noreply.github.com"
          PAPER_B_DIR="$RUNNER_TEMP/paper-b" uv run python -m ewbot.paperb.sync checkout

      - name: Restore the 15m candle cache
        uses: actions/cache@v6
        with:
          path: data/cache
          key: btc-usd-15m-${{ github.run_id }}-${{ github.run_attempt }}
          restore-keys: |
            btc-usd-15m-

      - name: Paper-B run
        id: paper-b
        timeout-minutes: 15
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run ewbot stratb paper run --ledger-dir "$PAPER_B_DIR"

      - name: Commit and push the ledger
        id: push
        timeout-minutes: 15
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run python -m ewbot.paperb.sync push uv run ewbot stratb paper run --ledger-dir "$PAPER_B_DIR"

      - name: Telegram failure alert (fallback when ewbot did not send one)
        id: alert
        if: failure() || cancelled()
        timeout-minutes: 2
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          JOB_STATUS: ${{ job.status }}
        run: |
          set -euo pipefail
          if [ -f "${EWBOT_FAILURE_MARKER:-/nonexistent}" ]; then
            echo "ewbot already sent the failure alert"
            exit 0
          fi
          if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
            echo "no Telegram secrets: skipping the alert"
            exit 0
          fi
          text="[ewbot paper-B] 실패 · 단계 workflow (checkout / setup / sync / timeout / push) · ${JOB_STATUS}"$'\n'"로그: ${EWBOT_RUN_URL}"
          curl -sS --fail-with-body --retry 3 --max-time 20 -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${text}"
          echo
```

**파일 `docs/strategy-b.md`** (새 파일, 전체 내용)

````markdown
# Strategy B (Plan 4) — research, one-shot holdout, paper trading

Spec: `docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md`. Plan: `docs/superpowers/plans/2026-10-07-ewbot-plan4-strategy-b.md`.

Strategy B takes the swing structure from 1h bars (built from 15m bars, A's ATR ZigZag), waits for a retracement into a Fibonacci zone that overlaps an earlier swing, and enters on the next 15m open after a 15m RSI divergence and a breakout close. Half is taken at TP1, the rest runs to the leg's extension with a breakeven or original stop, everything is closed after 48 hours. Risk 0.5% per trade, leverage cap 3x, A's breakers (-3% day, -15% total) and A's costs.

## Isolation from strategy A

- B's code lives in `src/ewbot/stratb/` (the B fingerprint hashes every `*.py` there) and `src/ewbot/paperb/` (the paper-B runtime). A's fingerprinted folders, `paper/`, `backtest/`, `live/`, `holdout.lock`, `paper.yml`, `ci.yml`, `scripts/` and `uv.lock` are never changed; B only imports from them.
- B has its own cache file (`data/cache/BTC-USD_15m.parquet`), lock (`holdout-b.lock`), ledger branch (`paper-b`), workflow (`.github/workflows/paper-b.yml`) and Actions cache key (`btc-usd-15m-`).
- `tests/stratb/test_isolation.py` checks A's fingerprint, that B is outside A's fingerprinted folders and that no A path changed since `master`; `tests/paper/test_static.py` (run by `ci.yml`) carries the CI part.

## Commands

```bash
uv run ewbot stratb fetch                                   # 15m cache; stops at 2024-01-01 until holdout-b.lock exists
uv run ewbot stratb backtest --params 2.0,0.382,0.618,1.0,1.272,breakeven   # one run, 2017..2023 only
uv run ewbot stratb walkforward                             # 32 combos, A's windows and rules -> reports/stratb-walkforward.json
uv run ewbot stratb holdout lock --params-from reports/stratb-walkforward.json   # writes holdout-b.lock
uv run ewbot stratb holdout run --confirm                   # ONCE, after your approval
uv run ewbot stratb paper status --ledger-dir <paper-b worktree>   # read-only
```

`--params` is `zz_k,z_lo,z_hi,tp1_r,ext,remainder`. No research command can read a bar from 2024-01-01 on before the lock exists: the 15m loader filters them out while reading the parquet file and the fetcher does not download them.

## Research and holdout (in this order)

1. `uv run ewbot stratb fetch` (about 820 requests to the public Coinbase API, a few minutes). Check the printed missing-bar count.
2. `uv run ewbot stratb walkforward`. It prints the stitched out-of-sample metrics, `chosen_final` and `proceed`. If `proceed` is false (negative out-of-sample return, or the last fold had no selectable combo), strategy B ends here (spec 6.3).
3. `uv run ewbot stratb holdout lock --params-from reports/stratb-walkforward.json`, then commit `holdout-b.lock` right away. The lock fixes the combo, the run config, the B fingerprint, the hash of the pre-2024 bars and the holdout's last bar (the last 15m bar of the last complete hour at lock time).
4. Ask the user for approval. Only then: `uv run ewbot stratb holdout run --confirm`, and commit `holdout-b.lock` again (it now holds `first_run`). The holdout runs once: a second run, a changed B file or other pre-2024 bars are refused.
5. If `first_run.criteria.passed` is false, strategy B ends (spec 6.5): no paper trading, no retuning on the same data.

## Paper trading (only after a passed holdout)

What runs: `.github/workflows/paper-b.yml` recomputes everything from 2023-10-01 (the replay anchor) with the params in the committed `holdout-b.lock`, records the decisions in `ledger.json` and `runs.log` on the orphan branch `paper-b` (only the workflow writes it: never push to it by hand), and sends Telegram messages prefixed `[ewbot paper-B]` with the same bot and secrets as strategy A. Paper trades are the trades that enter after the lock's last bar; 30 are needed (spec 7.1), with no unexplained discrepancy. A change of any file in `src/ewbot/stratb/` opens a new epoch and restarts the count.

One-time setup (you):

1. Make sure `master` has the committed, passed `holdout-b.lock` (the workflow reads it from `master`). Until step 2 the workflow is dormant: its job only runs when the repository variable `PAPER_B_ENABLED` is `true`, so the backup schedule neither runs nor alerts before the holdout passed.
2. Switch it on: `gh variable set PAPER_B_ENABLED --body true` (to stop paper-B later: `gh variable delete PAPER_B_ENABLED`).
3. Hourly trigger: create a second Cloudflare Worker (A's worker stays as it is: its code always dispatches `paper.yml`, so a second cron line in it would start A's workflow twice). In **Workers & Pages** → **Create Worker**, name it `ewbot-paper-b-trigger`, paste the code below, deploy; add the secret `GITHUB_TOKEN` (the same fine-grained token as A's worker: this repository, Actions read and write); add the cron trigger `47 * * * *`.

   ```js
   const DISPATCH_URL =
     "https://api.github.com/repos/taejunoh/elliott-wave-btc-bot/actions/workflows/paper-b.yml/dispatches";

   export default {
     async scheduled(event, env, ctx) {
       const response = await fetch(DISPATCH_URL, {
         method: "POST",
         headers: {
           Authorization: `Bearer ${env.GITHUB_TOKEN}`,
           Accept: "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "ewbot-paper-b-trigger",
           "Content-Type": "application/json",
         },
         body: JSON.stringify({ ref: "master" }),
       });
       if (!response.ok) {
         const text = await response.text();
         console.error(`workflow_dispatch failed: HTTP ${response.status} ${text}`);
         throw new Error(`workflow_dispatch failed: HTTP ${response.status}`);
       }
     },
     async fetch() {
       return new Response("Not found", { status: 404 });
     },
   };
   ```

   The workflow's own `schedule` (`47 */3 * * *`) is only a backup.
4. Start the first run by hand: `gh workflow run paper-b.yml --ref master`, then `gh run watch`.
5. Actions usage (spec 7): after the first week, compare the minutes used by `paper-b` with the free allowance at <https://github.com/settings/billing>. If the month would run over, change the worker's cron to `47 */2 * * *` (every 2 hours): decisions do not change (every run recomputes everything), only the messages come later.

## Failures

- A failed run sends `[ewbot paper-B] 실패 · 단계 <stage>` with the log link (the workflow's fallback alert covers checkout, setup and push failures). Nothing is committed after a failed run; the next run recomputes everything.
- Stage `precheck`: `holdout-b.lock` is missing, not committed, edited, not run, failed, or its config is not the code's. Stage `data`: the candle update failed three times, or the holdout window's 15m bars no longer hash to the holdout run's (spec 8: the run is refused until this is understood).
- Discrepancies (`[ewbot paper-B] 경고: 결정 불일치`) are classified like A's: `DATA_REVISION`, `FINGERPRINT_CHANGE`, `ENVIRONMENT_CHANGE`, `UNEXPLAINED`. Any `UNEXPLAINED` one blocks the go-live condition.

## Go-live (Plan 5, never automatic)

Holdout passed, at least 30 paper trades after the lock's last bar, zero unexplained discrepancies in the paper period, and your explicit approval. Live trading of B needs its own Coinbase account or portfolio (A and B cannot share one BIP position) and is Plan 5.
````

**수정 `README.md`** (1/5) — 다음 부분을 찾아:

```markdown
| **Live trading** (Plan 3) | Turns the same decisions into Coinbase CFM futures orders (`BIP-20DEC30-CDE`, nano BTC perp, 0.01 BTC per contract) with exchange-side stops | A US-East VPS, in Docker | **SHADOW** since 2026-10-05 (no real orders) |
```

바꿀 내용:

```markdown
| **Live trading** (Plan 3) | Turns the same decisions into Coinbase CFM futures orders (`BIP-20DEC30-CDE`, nano BTC perp, 0.01 BTC per contract) with exchange-side stops | A US-East VPS, in Docker | **SHADOW** since 2026-10-05 (no real orders) |
| **Strategy B** (Plan 4) | A separate 15m strategy (1h swing structure, Fibonacci zone with an earlier swing, 15m RSI divergence and breakout). Its own walk-forward, one-shot holdout (`holdout-b.lock`) and paper trading (`paper-b` branch). Never touches A. | Your laptop, then GitHub Actions (`paper-b.yml`) | Research ([`docs/strategy-b.md`](docs/strategy-b.md)) |
```

**수정 `README.md`** (2/5) — 다음 부분을 찾아:

```markdown
- **Never re-run or tune the holdout,** and never edit `holdout.lock`.
```

바꿀 내용:

```markdown
- **Never re-run or tune the holdout,** and never edit `holdout.lock`. The same holds for strategy B's `holdout-b.lock`.
- **Strategy B never touches A:** its code stays in `src/ewbot/stratb/` and `src/ewbot/paperb/`, and any change in `stratb/` restarts B's paper count (B fingerprint), never A's.
```

**수정 `README.md`** (3/5) — 다음 부분을 찾아:

```markdown
  - [`docs/backtest-runtime.md`](docs/backtest-runtime.md): backtest runtime.
```

바꿀 내용:

```markdown
  - [`docs/backtest-runtime.md`](docs/backtest-runtime.md): backtest runtime.
  - [`docs/strategy-b.md`](docs/strategy-b.md): strategy B research, holdout and paper trading.
```

**수정 `README.md`** (4/5) — 다음 부분을 찾아:

```markdown
  - [Plan 3: live trading](docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md)
```

바꿀 내용:

```markdown
  - [Plan 3: live trading](docs/superpowers/specs/2026-10-03-ewbot-plan3-live-trading-design.md)
  - [Plan 4: strategy B](docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md)
```

**수정 `README.md`** (5/5) — 다음 부분을 찾아:

```markdown
src/ewbot/live/       live runtime (Plan 3): exchange adapter, reconcile, executor, watchdog, Telegram, stages
```

바꿀 내용:

```markdown
src/ewbot/live/       live runtime (Plan 3): exchange adapter, reconcile, executor, watchdog, Telegram, stages
src/ewbot/stratb/     strategy B (Plan 4): 15m data, rules, engine, walk-forward, holdout (B fingerprint)
src/ewbot/paperb/     strategy B paper trading (Plan 4): runner, ledger adapters, paper-b git sync
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/paperb/test_sync.py tests/paperb/test_workflow.py tests/paper/test_static.py tests/paper/test_fingerprint.py -q
```

예상: `56 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/paperb/sync.py .github/workflows/paper-b.yml docs/strategy-b.md README.md tests/paperb/test_sync.py tests/paperb/test_workflow.py tests/paper/test_static.py
git commit -m "feat(paperb): paper-b workflow and git sync, CI guard for strategy A, strategy-B ops guide" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: 최종 검증 (전체 테스트, A 지문, A 보호)

**Files:** 없음. 이 작업은 코드를 바꾸지 않고, 병합 전에 확인만 한다.

**Interfaces:**
- Consumes: Task 1~12의 결과 전체.
- Produces: 병합 가능 여부의 판단 근거(전체 테스트 수, A 지문 값, 바뀐 파일 목록).

- [ ] **Step 1: 전체 테스트**

```bash
uv run --group live pytest -q
```

예상: `1360 passed` 또는 `1359 passed, 1 skipped`

`data/cache/BTC-USD_1h.parquet`가 없으면 `tests/backtest/test_e2e_real.py` 1개가 건너뛰어진다. 기준선보다 정확히 114개가 많아야 한다.

- [ ] **Step 2: A 지문이 그대로인지 확인**

```bash
uv run python -c "from ewbot.paper.fingerprint import current_fingerprint; print(current_fingerprint())"
```

예상: `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`

- [ ] **Step 3: A 보호 검사를 건너뜀 없이 실행**

```bash
uv run --group live pytest tests/stratb/test_isolation.py tests/paper/test_fingerprint.py tests/paper/test_static.py -q -rs
```

예상: `51 passed`

출력에 `SKIPPED`가 없어야 한다: `master`가 있는 작업 트리에서는 `test_strategy_b_work_changes_no_strategy_a_path`(merge-base 이후 B를 바꾼 이 브랜치가 A 경로를 바꾸지 않음)와 `test_baseline_recomputes_from_git_and_equals_the_working_tree`(holdout 커밋 `64e628a`에서 다시 계산한 A 지문)가 실제로 돈다.

- [ ] **Step 4: `master` 대비 바뀐 파일이 허용 목록뿐인지 확인**

```bash
git diff --name-only "$(git merge-base HEAD master)" -- . ':(exclude)docs/superpowers'
```

예상 출력(정확히 이 목록):

```text
.github/workflows/paper-b.yml
README.md
docs/strategy-b.md
src/ewbot/cli.py
src/ewbot/paperb/__init__.py
src/ewbot/paperb/decisions.py
src/ewbot/paperb/ledger.py
src/ewbot/paperb/notify.py
src/ewbot/paperb/runner.py
src/ewbot/paperb/sync.py
src/ewbot/stratb/__init__.py
src/ewbot/stratb/baselines.py
src/ewbot/stratb/config.py
src/ewbot/stratb/data15.py
src/ewbot/stratb/engine.py
src/ewbot/stratb/fingerprint.py
src/ewbot/stratb/holdout.py
src/ewbot/stratb/research.py
src/ewbot/stratb/rules.py
src/ewbot/stratb/signals.py
tests/paper/test_static.py
tests/paperb/__init__.py
tests/paperb/conftest.py
tests/paperb/helpers.py
tests/paperb/test_adapters.py
tests/paperb/test_cli_paper_b.py
tests/paperb/test_runner.py
tests/paperb/test_sync.py
tests/paperb/test_workflow.py
tests/stratb/__init__.py
tests/stratb/helpers.py
tests/stratb/test_baselines.py
tests/stratb/test_cli_stratb.py
tests/stratb/test_config.py
tests/stratb/test_data15.py
tests/stratb/test_engine.py
tests/stratb/test_fingerprint_b.py
tests/stratb/test_holdout.py
tests/stratb/test_isolation.py
tests/stratb/test_research.py
tests/stratb/test_rules.py
tests/stratb/test_signals.py
```

목록에 A의 경로(`src/ewbot/` 아래 `stratb/`·`paperb/`·`cli.py` 밖의 소스, `holdout.lock`, `paper.yml`, `ci.yml`, `scripts/`, `uv.lock`, `pyproject.toml`)가 하나라도 있으면 병합하지 않는다.

- [ ] **Step 5: 작업 트리가 깨끗한지 확인**

```bash
git status --porcelain
```

예상: 출력 없음.

---

### Task 14: 수동 절차 — 병합, 수집, 워크포워드, 잠금, 승인, 홀드아웃 1회, 모의매매 시작 (컨트롤러와 사용자만, 서브에이전트 금지)

이 작업은 실제 네트워크와 실제 데이터를 쓰고, 되돌릴 수 없는 1회 실행을 포함한다. 각 단계의 결과를 사용자에게 보고하고, 표시된 곳에서는 사용자의 명시적 승인을 받은 뒤에만 진행한다. 명세 10절의 순서를 따른다.

- [ ] **Step 1: PR과 병합(사용자)**

```bash
git push -u origin feat/plan4-strategy-b
gh pr create --base master --title "Plan 4: strategy B research and paper-B runtime" --body-file - <<'BODY'
Strategy B (Plan 4): 15m data, rules, engine, walk-forward, one-shot holdout lock, paper-B runtime and workflow (dormant until PAPER_B_ENABLED). Strategy A's fingerprint e0435223… is unchanged; no strategy-A file, workflow, lock or ledger is modified.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
```

`ci` 체크(A 지문·정적 검사)가 통과해야 병합한다. `paper-b.yml`은 병합되어도 `PAPER_B_ENABLED`가 없어 돌지 않는다.

- [ ] **Step 2: 연구 브랜치와 15분봉 수집(네트워크)**

```bash
git switch master && git pull --ff-only && git switch -c chore/stratb-holdout
uv run ewbot stratb fetch
```

약 820번의 공개 API 요청(수 분). 출력의 `bars before 2024-01-01`(약 245,000)과 `missing`(누락 봉 수)을 기록한다. 중단되면 다시 실행한다(이어서 받는다). 잠금 전이므로 2024 이후 봉은 받지 않는다(`fetch window: stops at 2024-01-01 (no lock)`).

- [ ] **Step 3: 워크포워드 실행과 보고(2024 데이터 없음)**

```bash
uv run ewbot stratb walkforward
```

몇 분 안에 끝난다(조합 32개). 사용자에게 보고한다: 구간별 선택 조합, 이어 붙인 표본 외 성과(`WF-OOS` 줄), 조합표(`reports/stratb-walkforward.json`의 `combo_table`: 조합별 Calmar·샤프·거래 수), `chosen_final`, `chosen_final_fallback`, `proceed`. 2단계 그리드에서는 선택이 '자신의 Calmar 최대'와 같다는 점(명세 해석 2)도 함께 보고한다. 월 거래 수가 명세 목표(월 10~15건)에서 크게 벗어나면 그것도 보고한다(규칙은 바꾸지 않는다). **`proceed`가 거짓이면 B를 여기서 끝낸다**(명세 6.3): 잠금을 만들지 않는다.

- [ ] **Step 4: 잠금 작성과 즉시 커밋**

```bash
git status --porcelain src/ewbot/stratb
uv run ewbot stratb holdout lock --params-from reports/stratb-walkforward.json
git add holdout-b.lock
git commit -m "chore(stratb): lock the strategy-B holdout" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

첫 줄의 출력이 비어 있어야 한다(잠금의 B 지문은 작업 트리의 `stratb/`로 계산된다). 잠금의 `last_bar_ts`는 지금 시각의 마지막 완성 시간의 마지막 15분봉이며 홀드아웃의 끝이다.

- [ ] **Step 5: 사용자 승인(필수)**

사용자에게 잠금 내용(조합, `last_bar_ts`, 무작위 대조군 1,000회)과 합격 기준 4개(명세 6.5)를 보여 주고, 홀드아웃을 **1회** 실행해도 되는지 명시적 승인을 받는다. 승인이 없으면 멈춘다.

- [ ] **Step 6: 홀드아웃 1회 실행과 커밋(네트워크)**

```bash
uv run ewbot stratb holdout run --confirm
git add holdout-b.lock
git commit -m "chore(stratb): record the strategy-B holdout run" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

캐시를 지금까지 늘린 뒤 2023-10-01부터 잠금의 마지막 봉까지 다시 계산하고, 매수 후 보유·무작위 대조군 1,000회·비용 1.5배를 돈다(작업자 수에 따라 1~5분). 출력의 기준 4개와 `PASSED`/`FAILED`를 그대로 사용자에게 보고한다. 다시 실행하거나, 기준을 낮추거나, 다른 조합을 시험하지 않는다(명세 6.5). 같은 명령을 다시 실행하면 거부된다.

- [ ] **Step 7: 결과 병합(사용자)**

```bash
git push -u origin chore/stratb-holdout
gh pr create --base master --title "chore(stratb): strategy-B holdout lock and its one run" --body "holdout-b.lock with first_run. 🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

`FAILED`여도 잠금은 영구 기록으로 병합하고 B를 끝낸다(모의매매를 켜지 않는다).

- [ ] **Step 8: 모의매매 시작(합격한 경우에만, 사용자)**

`docs/strategy-b.md`의 "Paper trading" 절차를 따른다: `gh variable set PAPER_B_ENABLED --body true`, 두 번째 Cloudflare Worker(크론 `47 * * * *`), 첫 실행 `gh workflow run paper-b.yml --ref master`와 `gh run watch`. 첫 실행 뒤 텔레그램에 `[ewbot paper-B]` 메시지가 오는지(뉴욕 9시 이후 첫 실행이면 일일 요약), `paper-b` 브랜치의 `runs.log`에 `holdout trades recomputed`가 잠금의 거래 수와 같은지(`ewbot stratb paper status`) 확인한다:

```bash
git fetch origin paper-b
git worktree add "$TMPDIR/paper-b" origin/paper-b
uv run ewbot stratb paper status --ledger-dir "$TMPDIR/paper-b"
git worktree remove "$TMPDIR/paper-b"
```

- [ ] **Step 9: 첫 주 사용량 확인(사용자)**

1주 뒤 GitHub Billing에서 `paper-b`의 Actions 사용 시간을 확인한다. 무료 한도를 넘을 것 같으면 Worker의 크론을 `47 */2 * * *`로 바꾼다(판단은 같고 알림만 늦어진다, 명세 7).

---

## 자기 검토 (Self-Review)

**1. 명세 대응표**

| 명세 | 요구 | 작업 |
|---|---|---|
| 1 목표·결정 사항 | 별도 전략 B, 1h 방향·15m 진입, 확인 후 다음 봉 시가 진입, TP1 절반·잔량 규칙 그리드, 0.5%·3배·−3%/−15% | 1, 4, 5 |
| 1 비목표 | A 변경 없음, 실거래 없음(Plan 5) | Global Constraints, 1(`test_isolation`), 12(`test_static`), 13 |
| 2 격리 | `stratb/`·`paperb/`만, import만, `uv.lock` 불변, CI A 지문 통과, B 검사 추가 | 1, 12, 13 |
| 3 구조 | `data15`, `rules`, `engine`, `config`, `research`, `baselines`, `fingerprint`, `paperb/runner`, `paper-b.yml`, `holdout-b.lock`, `paper-b` 브랜치, CLI 6개 | 1~12(해석 18) |
| 4.1 큰 구조 | 15m→1h, A의 ZigZag(`zz_k`), 가장 최근 확정 상승 구간 | 4(해석 4) |
| 4.2 후보 구간 | 되돌림 구간, 매물대 ±0.25×ATR1h, L 아래 종가 무효 | 3, 4(해석 5) |
| 4.3 확인·진입 | RSI(14) 다이버전스(좌우 3봉), 두 번째 저점 구간 안, 종가 돌파, 다음 봉 시가, 72시간 | 3, 4, 5(해석 6) |
| 4.4 손절·청산·수량 | 손절 −0.2×ATR15, 2.5×ATR1h 걸러내기, 1포지션, TP1 `tp1_r`R 절반, TP2 확장, TP2가 가까우면 제외, 잔량 규칙, 48시간, 0.5%·3배, A의 손실 한도 | 3, 4, 5(해석 7, 9) |
| 4.5 제외 항목 | 주관 확률·추세선·거시·분할 진입·지정가 선주문 없음 | 구현하지 않음 |
| 5 그리드 | 32개, 고정값 | 1 |
| 6.1 데이터 | 2017-01부터 15m, 1h 합성, 누락 봉 기록·미보충, 확정 봉만 | 2, 4(해석 8) |
| 6.2 체결 | 15m 체결, A의 비용, 손절 우선, A의 초기 자금, 0.01 BTC 정수 내림 | 5 |
| 6.3 워크포워드 | A의 구간 길이·최소 거래·선택 점수·최종 조합(실제 값 확인), 음수면 종료, 2024 가드와 테스트 | 2, 6(해석 1~3), 11 |
| 6.4 잠금·홀드아웃 1회 | 조합·B 지문·데이터 해시·그리드·시작일, 승인 후 1회, `first_run`, `REPLAY_ANCHOR` 2023-10-01 | 2, 8(해석 11), 11, 14 |
| 6.5 합격 기준 | 샤프 > BH·무작위 p95(1,000회), MDD ≤ 15%, 거래 ≥ 50, 비용 1.5배 순이익 > 0 | 7, 8(해석 10) |
| 6.6 해석의 한계 | 합격 = 버릴 이유 없음, 확인은 모의매매 | 14(보고), 문서 |
| 7 모의매매 | 앵커부터 결정적 재계산, A의 불일치 분류, `paper-b.yml`(매시 47분, 예비 schedule), 사용량, orphan `paper-b`, A 장부 스키마, 잠금 이후 진입만 카운트, B 지문 구간, `[ewbot paper-B]` 진입·청산·일일 요약(n/30, 불일치, 손익), 실패 알림 | 9, 10, 11, 12(해석 12~17), 14 |
| 7.1 실거래 전환 조건 | 합격, 모의 30건, 원인 불명 0건, 사용자 승인 | 10(`status_lines_b`), 문서 |
| 8 오류 처리 | 수집 재시도 후 알림·중단, 누락 봉 판단 없음, 잠금 불일치 시 거부·알림, A와 충돌 없음(테스트) | 2, 4, 10, 11, 1, 12 |
| 9 테스트 | 규칙 단위, 미래 정보 차단(잘라내기), 체결, 홀드아웃 보호, A 보호(CI), 모의매매(A 도우미 재사용) | 3, 4, 5, 8, 1·12·13, 9·10·11·12 |
| 10 진행 절차 | 승인 → 계획 → 작업별 구현·검토 → PR(A의 CI 통과) → 수집·워크포워드·보고 → 잠금 → 승인 → 홀드아웃 1회 → 합격 시 크론·모의매매 | 1~13, 14 |

**2. 자리 표시자 검사:** "TBD", "TODO", "나중에", "적절히 처리", "Task N과 같이" 같은 표현이 없다. 모든 코드 단계에 전체 코드가 있고, 모든 명령에 예상 출력이 있다.

**3. 타입 일관성:** `Combo`(튜플)와 JSON 조합(리스트)은 `normalize_combo`/`combo_to_json`으로만 오간다. `SignalB.i`는 프레임 인덱스, 비교는 `ts`로 한다. `BResult`의 `signals`와 `orders`는 같은 순서다(`build_decisions_b`가 `zip(strict=True)`). `run_holdout_b`, `create_lock`, `run_paper_b`, `precheck_lock_b`, `PaperBEnv`의 이름과 인자는 Task 8·10·11·12에서 같다. 잠금 경로는 어디서나 `stratb.data15.DEFAULT_LOCK_PATH`(cli.py에서 `STRATB_LOCK`)다.

**4. 검증:** 이 계획은 작성 시점(2026-10-07)에 새 클론(`feat/plan4-strategy-b`, Python 3.12.13)에 문서 그대로 적용해 확인했다: 모든 파일과 수정을 적힌 대로 쓰고 모든 명령을 실행했으며, 39개의 `예상` 항목이 모두 맞았다(각 Step 2는 적힌 대로 실패, 각 Step 4는 적힌 수로 통과, Task 13의 전체 테스트 `1359 passed, 1 skipped`(실제 데이터 캐시 없음), A 지문 `e0435223…` 그대로, 바뀐 파일 목록 일치, 작업 트리 깨끗함). 적용 결과의 42개 파일은 개발 사본과 바이트 단위로 같았다.
