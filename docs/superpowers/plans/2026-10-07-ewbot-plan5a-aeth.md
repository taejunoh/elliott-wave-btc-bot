# ewbot Plan 5a — A-ETH(고정된 전략 A를 ETH에) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 A를 전혀 바꾸지 않고(코드, `holdout.lock`의 파라미터, 손실 한도, 지문 `e0435223…`) Coinbase `ETH-USD` 1시간봉에 그대로 적용하는 별도 연구·모의매매 트랙 A-ETH를 만든다: 2024 가드가 있는 ETH 데이터, 1회짜리 단계 1(2017~2023), `holdout-aeth.lock`과 1회 ETH 홀드아웃(A의 `evaluate_criteria` 그대로), ETH 전용 카운트를 쌓는 모의매매 런타임(`paper-aeth`).

**Architecture:** 새 코드는 모두 `src/ewbot/aeth/`(A의 지문 대상 폴더 밖)에 두고, A의 모듈(설정 기본값, 홀드아웃 설정·다이제스트·데이터 해시, `compare`·`run_backtest`, 성과 지표, `evaluate_criteria`, 모의매매의 추적 엔진·판단 대조·장부·알림, Coinbase `fetch_1h`)은 import만 한다. 설정은 `holdout_config(BotConfig(), A의 잠금 파라미터)`에서 계약 크기만 0.1 ETH로 바꾼 것이다. 2024 이후 ETH 봉은 유효한 `holdout-aeth.lock`이 생기기 전에는 수집 단계에서 요청되지 않고 parquet을 읽을 때 걸러진다. 단계 1 기록(`aeth-stage1.json`)과 잠금이 각 단계를 1회로 묶고, 모의매매는 A의 `paper/runner.py`를 상품·비용·잠금만 바꿔 옮긴 런타임으로 `paper-aeth` 브랜치에 기록한다. `cli.py`는 `ewbot.aeth`를 `aeth` 명령 안에서만 import한다.

**Tech Stack:** Python 3.12 (uv), 기존 의존성만(numpy, pandas, pyarrow, requests, PyYAML); pytest; GitHub Actions(`actions/checkout@v7`, `astral-sh/setup-uv` v10.2.0 커밋 `c18668ad3cf93ea998bef934396af7bb5c839dc7`, `actions/cache@v6`); Coinbase Exchange 공개 캔들 API(granularity 3600, `ETH-USD`); Telegram Bot API(A와 같은 봇·시크릿).

**Spec:** `docs/superpowers/specs/2026-10-07-ewbot-plan5a-aeth-design.md`(사용자 승인 2026-10-07; 결정 사항 원문은 명세 1.3절). 상위 명세: Plan 1 `2026-10-01-elliott-wave-btc-bot-design.md`(9.6절), Plan 2 `2026-10-02-ewbot-plan2-paper-trading-design.md`, Plan 3 `2026-10-03-ewbot-plan3-live-trading-design.md`, Plan 4 `2026-10-07-ewbot-plan4-strategy-b-design.md`(격리 방식의 선례).

## Global Constraints

- 작업 브랜치 `feat/aeth`(`master` `cbf57ea`에서: 전략 B 종료 기록(PR #12)까지 병합된 상태), 저장소 루트에서 실행한다. Python 3.12(uv). 테스트 명령은 `uv run --group live pytest …`. 기준선(`master`, 이 계획 이전): `1381 passed`(`data/cache/BTC-USD_1h.parquet`가 없으면 `1380 passed, 1 skipped`).
- **사전 등록**: 이 계획과 명세는 ETH 1시간봉을 처음 받기 전에 커밋되었다. Task 1~8은 네트워크와 실제 데이터를 쓰지 않는다. ETH 데이터는 Task 9(컨트롤러 전용, 2024 이전만)에서 처음 받고, 2024 이후 ETH 데이터는 Task 10의 홀드아웃 실행(사용자 승인 뒤)에서 처음 받는다.
- **A의 지문 대상은 절대 수정하지 않는다**: `src/ewbot/types.py`, `config.py`, `indicators.py`, `data/candles.py`, `waves/**`, `strategy/**`, `engine/**`, `execution/**`, `risk/**`. 지문은 폴더 전체를 해시하므로 이 폴더들 안에 **파일을 추가하지도 않는다**.
- 다음도 수정하지 않는다: `holdout.lock`, `src/ewbot/backtest/**`, `src/ewbot/data/coinbase_history.py`(그리고 `data/` 폴더 전체), `src/ewbot/paper/**`, `src/ewbot/live/**`, `src/ewbot/stratb/**`, `src/ewbot/paperb/**`, `.github/workflows/paper.yml`, `.github/workflows/ci.yml`, `.github/workflows/paper-b.yml`, `scripts/**`, `ops/**`, `deploy/**`, `pyproject.toml`, `uv.lock`. 새 의존성을 추가하지 않는다.
- 만들 수 있는 것: `src/ewbot/aeth/**`, `tests/aeth/**`, `.github/workflows/paper-aeth.yml`, `docs/aeth.md`. 수정할 수 있는 것: `src/ewbot/cli.py`(`ewbot aeth …` 하위 명령만), `README.md`, 그리고 `tests/paper/test_static.py`의 최소 테스트 수정(Task 1: A-ETH가 A 지문 밖이라는 CI 검사 1개 추가; Task 4·5: `ALLOWED_TRUE`에 `aeth/holdout.py`·`aeth/paper.py` 추가; Task 7: 워크플로 목록에 `paper-aeth.yml`, `paper-aeth.yml`이 A를 건드리지 않는다는 CI 검사 1개 추가. `ci.yml`은 `tests/paper/test_fingerprint.py`와 이 파일만 실행하므로 A-ETH의 CI 검사는 여기에 둔다).
- 산출물 `aeth-stage1.json`, `holdout-aeth.lock`, 브랜치 `paper-aeth`, `data/cache/ETH-USD_1h.parquet`(gitignore)는 Task 9~11의 명령만 만든다. 구현 작업과 테스트는 이것들을 만들지도 쓰지도 않는다.
- A 지문은 `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`로 유지된다(`tests/paper/test_fingerprint.py`, `tests/aeth/test_isolation.py`, Task 8).
- **2024-01-01 00:00 UTC(`HOLDOUT_START = 1704067200`) 이후 ETH 봉은 유효한 `holdout-aeth.lock`이 생기기 전에 요청하지도 읽지도 않는다**: `aeth.data.fetch_eth`는 그런 구간을 요청하지 않고, `aeth.data.load_eth`는 parquet을 읽을 때 걸러내며, `unlock_2024=True`는 다이제스트가 맞는 잠금이 있어야 한다. 리터럴 `unlock_2024=True`는 `aeth/holdout.py`와 `aeth/paper.py`에서만 쓴다. A의 `update_cache`(지금까지 받는다)는 쓰지 않는다.
- 다른 전략의 정적 검사를 지킨다: 리터럴 `allow_holdout=True`는 `aeth/holdout.py`(`compare`)와 `aeth/paper.py`(`run_backtest`)에서만 쓴다(A의 `test_static.py`가 `src/ewbot` 전체를 검사한다). A-ETH 코드는 `allow_holdout`이라는 매개변수나 문자열, A의 `_init_worker`·`_map_tasks`·`check_before_holdout` 이름, B의 `include_holdout` 키워드(B의 `tests/stratb/test_isolation.py`가 검사한다)를 쓰지 않고, `ewbot.stratb`·`ewbot.paperb`를 import하지 않는다.
- 고정 입력(명세 5절): 파라미터는 `holdout.lock`에서 읽은 A의 값(K4 2.0, Q_MIN 0.7, S_MIN 0.5), 설정은 `holdout_config(BotConfig(), params)`에 `CostParams(contract_size=0.1)`(수수료 0.05%, 슬리피지 0.05%, 펀딩 연 10%, 최소 수수료 미적용), 무작위 대조군 100회, 단계 1 기간 [2017-01-01, 2024-01-01), 정보 기간 2019-01-01부터, 정보용 비용 배수 2. CLI에는 이것들을 바꾸는 옵션이 없다.
- 테스트는 네트워크, 실제 `data/cache`, 실제 `aeth-stage1.json`·`holdout-aeth.lock`을 쓰지 않는다(합성 ETH 가격 1시간봉, 임시 git 저장소, 가짜 수집기·발송기). 실제 `holdout.lock`은 `tests/aeth/test_config.py` 한 곳에서 **읽기만** 한다(A의 고정 파라미터 확인).
- 모든 커밋 메시지는 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 줄로 끝난다(`git commit -m "<제목>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"`). 푸시는 Task 9 이후의 수동 절차에서만 한다.

## 명세 해석 (이 계획이 정함, 모든 작업에 구속력)

1. **설정(명세 5.1).** `aeth_config(params) = dataclasses.replace(holdout_config(BotConfig(), params), costs=eth_costs())`. 그래서 `strategy.mode`는 `"A"`(A의 `compare`는 A·C·HYBRID를 직접 돌린다), 손실 한도는 켜진 A의 `BreakerParams()`, 위험은 `CONSERVATIVE`, 초기 자금 10,000, 시드 0이다. 파라미터는 `frozen_a_params()`가 매번 `holdout.lock`에서 읽고, 그 잠금이 A의 완료된 잠금(키, 그리드 조합, 홀드아웃 시작, `lock_digest(BotConfig(), params, random_runs, code_sha256)`, `first_run`)인지 확인한다.
2. **단계 1의 비교와 합격(명세 6).** A의 `compare(cfg, df, 2017-01-01, 2024-01-01, random_runs=100)`를 그대로 쓴다(A, C, HYBRID, BH, EMA, 무작위 100회: A의 실제 거래 수와 모양, 시드 0~99). 합격은 여섯 항목이 모두 참일 때다: A 순수익률 > 0, A 최대 낙폭 ≤ 0.15, A 샤프 > C 샤프, A Calmar > C Calmar, A 샤프 > 대조군 샤프 평균, A Calmar > 대조군 Calmar 평균. 값이 없거나(JSON의 null) 유한하지 않으면 그 항목은 거짓이다. BH·EMA·HYBRID는 보고만 한다(결정 사항이 C와 대조군만 기준으로 정했다). 판정은 기록에 쓰이는 JSON 값(`headline`)으로 계산하므로 잠금이 기록만으로 다시 계산할 수 있다.
3. **정보 실행(명세 6).** 2019~2023은 같은 봉(2017년부터 준비)으로 `compare(cfg, df, 2019-01-01, 2024-01-01, 100)`를 따로 돌리고, 같은 여섯 항목을 `checks_info_only`로 보여 준다. 비용 2배는 수수료율·최소 수수료·슬리피지를 2배로 하고 펀딩은 그대로 둔 A 1회(`run_backtest`)다. 둘 다 판정에 쓰지 않는다.
4. **단계 1 기록과 1회(명세 6).** `aeth-stage1.json`(저장소 루트, git에 커밋)에 한 번만 쓴다. 파일이 있으면 거부한다(결과와 상관없이 1회). 실행 전에 추적 파일의 커밋 안 된 변경, A 지문 ≠ `e0435223…`, 봉이 [2017-01-01, 2023-12-31 23:00]을 양 끝에서 하루 안으로 덮지 못함, 2024 봉을 거부한다. 기록의 `provenance`는 `holdout.lock`의 sha256, A 지문, A-ETH 지문, `code_sha256`(A의 `code_fingerprint`), 2024 이전 **원본**(캐시 그대로, 누락 봉을 채우기 전) 봉의 기록(`data_record`: 첫·끝 ts, 수, 누락 수, A의 `data_fingerprint`), 시각, git 상태다. 계산은 누락 봉을 A의 `fill_gaps`로 채운 봉으로 한다.
5. **잠금(명세 7.1).** 잠금은 커밋된 그대로의 단계 1 기록에서만 만든다(`committed_text`가 파일 내용과 같아야 한다). 기록은 합격이어야 하고 그 `criteria`가 `stage1_verdict(headline)`과 같아야 하며(손으로 바꾼 합격을 막는다), 기록의 파라미터·`holdout.lock` sha256·설정·A 지문·A-ETH 지문·2024 이전 봉 기록이 지금의 것과 같아야 한다. 기록의 `code_sha256`은 비교하지 않는다(단계 1과 잠금 사이에 `live/` 같은 무관한 파일이 바뀌어도 잠글 수 있게; 잠금 → 실행 사이는 8의 `code_sha256`이 막는다). `last_bar_ts`는 잠금 시각의 마지막 완성 시간(`now // 3600 * 3600 − 3600`)이고 2024-01-01 이후여야 한다. 잠금의 바인딩 필드(`LOCK_BOUND`)는 `kind, version, product, future, params, a_lock_sha256, config, holdout_start, last_bar_ts, random_runs, a_fingerprint, aeth_sha256, code_sha256, pre2024, stage1_sha256`이고 `sha256`은 그 다이제스트다. `locked_at`, `git_head`, `git_dirty`, `attempts`, `first_run`은 바인딩되지 않는다.
6. **"A의 잠금과 같은 코드 상태"(명세 7.1).** A의 잠금은 `code_sha256 = code_fingerprint()`(`src/ewbot` 아래 모든 `*.py`)를 묶었다. A-ETH 잠금도 같은 값을 묶고, 홀드아웃은 그 값이 다르면 거부한다. 거기에 A 지문과 A-ETH 지문을 따로 묶어, 어느 쪽이 바뀌었는지 거부 메시지가 말하게 한다.
7. **홀드아웃 실행(명세 7.2).** 확인, 유효한 잠금, 커밋된 잠금과 같은 `sha256`, 두 곳 모두 빈 `first_run`, 세 지문, `lock_mismatch`(잠금의 `kind, version, product, future, params, a_lock_sha256, config, holdout_start`가 지금 A의 파라미터에 대한 이 코드의 값과 같은지), 깨끗한 작업 트리(잠금 파일은 예외), 2024 이전 봉 기록, 마지막 봉까지의 데이터, 이전 시도의 홀드아웃 봉 해시를 이 순서로 확인한 뒤 시도를 기록하고 실행한다. 봉은 캐시 전체를 `fill_gaps`로 채운 뒤 `last_bar_ts`에서 자른다(마지막 봉이 원본에 없어도 모의매매와 같은 값; 해석 11). 실행은 `compare(cfg, df, HOLDOUT_START, last_bar_ts + 3600, random_runs=잠금 값, allow_holdout=True)`다.
8. **단계 2 합격(명세 7.3).** `evaluate_criteria(cmp)`(A의 함수, 바꾸지 않음)의 `winners`에 `"A"`가 있고(A의 샤프·Calmar > C, BH, 대조군 평균) A의 `max_dd ≤ 0.15`면 합격이다. HYBRID만 이긴 경우는 탈락이다(검증 대상은 A; A의 모의매매도 A만 센다). `first_run`에는 `evaluate_criteria`의 출력 전체, `headline`, `verdict`(`c1_a_beats_c_bh_random_mean`, `c2_max_dd_le_15pct`, `n_trades`, `passed`), 홀드아웃 구간 봉 해시가 들어간다.
9. **모의매매 런타임(명세 8).** A의 `run_paper`를 옮기되 다음만 다르다: 설정은 `aeth_config`, 잠금 검사는 `precheck_lock_aeth`(유효, 커밋된 그대로, 홀드아웃 합격, `lock_mismatch` 없음), 데이터는 `fetch_eth`·`load_eth(unlock_2024=True)`·`fill_gaps`, 구간 지문은 `epoch_fingerprint(A 지문, A-ETH 지문)`(기준값은 잠금의 두 값), 장부의 홀드아웃 요약은 `holdout_summary_aeth`(키 `lock_sha256, params, holdout_start, last_bar_ts, data_sha256, n_trades{"A"}, passed`), 메시지 접두어는 `[ewbot paper-A-ETH]`(A의 메시지의 `[ewbot paper]`를 바꿈), `report.html`과 매일의 9.6절 1·2번 재계산은 없다, 상태 줄은 `status_lines_aeth`. 카운트는 A의 `golive_count`(이 장부의 홀드아웃 A 거래 + 현재 구간의 모의 거래, 50건)이고 4번 기준은 A의 `criterion4`다.
10. **2024 가드의 세부(명세 5.3).** 잠금이 없을 때 캐시에 2024 이후 봉이 있으면(그 `ts` 열만 읽어 센다) `fetch_eth`와 `load_eth`가 `HoldoutGuardError`로 거부한다: A-ETH가 받은 것이 아니다(A의 `ewbot fetch --product ETH-USD`가 같은 파일을 지금까지 채운다). `fetch_eth`는 3,000시간 조각으로 A의 `fetch_1h(PRODUCT, start, stop)`를 부르고 조각마다 원자적으로 저장한다.
11. **데이터 해시.** 2024 이전 봉의 출처는 원본 봉(`data_record`)으로, 홀드아웃 구간은 누락 봉을 채운 봉의 `data_fingerprint(df, 2024-01-01, last_bar_ts)`로 잰다(A의 모의매매와 같은 방식). 홀드아웃과 모의매매가 모두 "캐시 전체를 채운 뒤 자른" 봉을 쓰므로 같은 데이터면 같은 해시다.
12. **CLI(명세 3).** `ewbot aeth fetch | stage1 | holdout lock | holdout run --confirm | paper run | paper status`. 처리 함수만 `ewbot.aeth`를 import하고 경로는 `ewbot.aeth.config`의 `LOCK_PATH`·`STAGE1_PATH`·`A_LOCK_PATH`를 호출할 때 읽는다(테스트가 바꿀 수 있게). `holdout run`은 `--confirm`과 유효한 잠금을 확인한 **뒤에만** 캐시를 지금까지 늘린다. 거부는 A·B의 CLI처럼 `refusing: …`과 종료 코드 2다.
13. **워크플로(명세 8).** `paper-b.yml`과 같은 모양: 저장소 변수 `PAPER_AETH_ENABLED`가 `true`일 때만 돌고(휴면), 1차 트리거는 두 번째 Cloudflare Worker의 크론 `42 * * * *`(코드는 `docs/aeth.md`), 예비 `schedule`은 `42 */3 * * *`, 동시성 그룹 `paper-aeth`, Actions 캐시 키 `eth-usd-1h-`, git 쪽은 `python -m ewbot.aeth.sync`(B의 `paperb.sync`와 같은 규칙, 브랜치 `paper-aeth`, 환경변수 `PAPER_AETH_DIR`·`PAPER_AETH_PUSH_BACKOFF`). B의 코드는 끝난 전략의 기록이므로 import하지 않고 같은 규칙을 다시 쓴다.
14. **테스트 데이터.** 합성 ETH 봉은 A의 모의매매 테스트 경로(`tests.paper.helpers.synth_frame`, 2023-11-15 ~ 2024-03-18)의 가격에 0.1을 곱한 것이다: 0.1 ETH 계약에서 A의 BTC 테스트(0.01 BTC 계약)와 같은 거래가 난다(홀드아웃 2건, 2024-02-10까지 모의 2건). 단계 1 테스트는 같은 경로를 마지막 봉이 2023-12-31 23:00이 되게 앞으로 옮긴 3,000개 봉을 쓴다. 파라미터는 A의 모의매매 테스트와 같은 (2.0, 0.5, 0.0)이고, 합성 A 잠금(`tests.paper.helpers.make_lock`)이 그 값을 담는다.

## File Structure

| 파일 | 작업 | 책임 |
|---|---|---|
| `src/ewbot/aeth/__init__.py` | 1 | 패키지 표시 |
| `src/ewbot/aeth/config.py` | 1 | 상품·계약·기간·경로 상수, ETH 비용, A의 잠금에서 읽는 고정 파라미터, 실행 설정 |
| `src/ewbot/aeth/fingerprint.py` | 1 | A-ETH 지문, 구간 지문 |
| `src/ewbot/aeth/lockfile.py` | 2 | 잠금 다이제스트·유효성·읽기, 원자적 JSON 쓰기, 커밋된 내용, 더러운 작업 트리 검사 |
| `src/ewbot/aeth/data.py` | 2 | ETH 1시간봉 수집(조각 저장)·로드, 2024 가드 |
| `src/ewbot/aeth/stage1.py` | 3 | 단계 1 비교·판정·정보 실행·1회 기록 |
| `src/ewbot/aeth/holdout.py` | 4 | 잠금 작성, 1회 ETH 홀드아웃, 단계 2 판정 |
| `src/ewbot/aeth/paper.py` | 5 | ETH 모의매매 1회 실행, 장부 바인딩, 상태 |
| `src/ewbot/cli.py` (수정) | 6 | `ewbot aeth …` |
| `src/ewbot/aeth/sync.py` | 7 | `paper-aeth` 브랜치 git 동기화 |
| `.github/workflows/paper-aeth.yml` | 7 | 휴면 모의매매 워크플로 |
| `tests/paper/test_static.py` (수정) | 1, 4, 5, 7 | A의 CI 정적 검사에 A-ETH 반영(최소 수정) |
| `docs/aeth.md`, `README.md` (수정) | 7 | 운영 문서 |
| `tests/aeth/*` | 1–7 | 테스트, 합성 데이터·기록 도우미(`helpers.py` Task 2, `records.py` Task 4) |

## 작업 개요

| 작업 | 구성 | 새 테스트 |
|---|---|---|
| 1 | 설정·고정 파라미터, 지문, A 보호 검사(+ CI 검사 1개) | 24 |
| 2 | 잠금 파일 기본 기능, ETH 데이터와 2024 가드 | 11 |
| 3 | 단계 1(비교, 판정, 정보 실행, 1회 기록) | 21 |
| 4 | 잠금 작성, 1회 ETH 홀드아웃, 단계 2 판정 | 27 |
| 5 | ETH 모의매매 런타임 | 15 |
| 6 | CLI `ewbot aeth …` | 12 |
| 7 | `paper-aeth` 동기화, 워크플로, CI 검사 1개, 문서 | 11 |
| 8 | 최종 검증(전체 테스트, A 지문, A 보호, 바뀐 파일) | — |
| 9 | 수동: PR·병합(사용자), **단계 1**(컨트롤러 전용; 네트워크: 2024 이전 ETH만) | — |
| 10 | 수동: **단계 2** — 잠금, **사용자 승인**, 홀드아웃 1회, 즉시 커밋 | — |
| 11 | 수동: **단계 3** — 모의매매 시작(사용자) | — |

Task 7 이후 전체: `1502 passed`(캐시가 없으면 `1501 passed, 1 skipped`) — 기준선보다 121개 많다(`tests/aeth` 119개, `tests/paper/test_static.py` 2개). 각 작업의 Step 4는 그 작업의 테스트만 돌린다.

---

### Task 1: 설정·고정 파라미터, 지문, A 보호 검사

**Files:**
- Create: `src/ewbot/aeth/__init__.py`
- Create: `src/ewbot/aeth/config.py`
- Create: `src/ewbot/aeth/fingerprint.py`
- Test: `tests/aeth/__init__.py`
- Test: `tests/aeth/test_config.py`
- Test: `tests/aeth/test_fingerprint.py`
- Test: `tests/aeth/test_isolation.py`
- Modify: `tests/paper/test_static.py` (CI 검사 1개 추가)

**Interfaces:**
- Consumes: A의 `ewbot.backtest.holdout`: `DEFAULT_LOCK_PATH`, `holdout_config(cfg, params) -> BotConfig`, `lock_digest(cfg, params, random_runs=100, code_sha256=None) -> str`; `ewbot.backtest.runner.utc_ts(date) -> int`; `ewbot.backtest.walkforward`: `GRID`, `HOLDOUT_START`, `WF_START`, `grid_combos() -> list[tuple]`; `ewbot.config`: `BotConfig`, `CostParams`; `ewbot.paper.fingerprint`: `PROJECT_ROOT`, `current_fingerprint(repo_root=None) -> str`, `fingerprint_of(files: Mapping[str, bytes]) -> str`, `is_fingerprinted(rel) -> bool`, `tree_files(repo_root=None) -> dict[str, bytes]`. 테스트: `tests.paper.helpers.make_lock(df, boundary=BOUNDARY, params=PARAMS, n_a=2, **overrides) -> dict`(유효한 다이제스트의 합성 A 잠금), `synth_frame() -> DataFrame`.
- Produces: `ewbot.aeth.config`: `PRODUCT = "ETH-USD"`, `FUTURE = "ETP-20DEC30-CDE"`, `CONTRACT_SIZE = 0.1`, `FROZEN_A_FINGERPRINT`(`e0435223…` 전체 64자), `DATA_START = 1483228800`(2017-01-01), `SUB_START = 1546300800`(2019-01-01), `RANDOM_RUNS = 100`, `COST_INFO_MULT = 2.0`, `LOCK_PATH = PROJECT_ROOT / "holdout-aeth.lock"`, `STAGE1_PATH = PROJECT_ROOT / "aeth-stage1.json"`, `A_LOCK_PATH`(A의 `DEFAULT_LOCK_PATH`), `Combo = tuple[float, ...]`, `class AethError(RuntimeError)`, `eth_costs() -> CostParams`, `scale_trading_costs(c: CostParams, mult: float) -> CostParams`(수수료율·최소 수수료·슬리피지만 배수), `frozen_a_params(a_lock_path: Path = A_LOCK_PATH) -> tuple[Combo, str]`(파라미터와 A 잠금의 `sha256`; 아니면 `AethError`), `aeth_config(params: Combo) -> BotConfig`, `config_json(params: Combo) -> dict[str, Any]`(`aeth_config`의 JSON).
  `ewbot.aeth.fingerprint`: `AETH_PREFIX = "src/ewbot/aeth/"`, `aeth_files(repo_root=None) -> dict[str, bytes]`, `aeth_fingerprint(repo_root=None) -> str`(파일이 없으면 `FileNotFoundError`), `epoch_fingerprint(a_fingerprint: str, aeth_sha256: str) -> str`, `current_epoch_fingerprint(repo_root=None) -> str`.
  `tests/aeth/test_isolation.py`: 이후 모든 작업에서 A·B 보호를 지키는 검사.

A-ETH의 모든 상수와 실행 설정을 A의 `config.py`와 별개인 `aeth/config.py`에 둔다. 설정은 A의 홀드아웃 설정에서 비용의 계약 크기만 0.1로 바꾼 것이고(해석 1), 파라미터는 매번 A의 커밋된 `holdout.lock`에서 읽는다(읽기만 한다). `frozen_a_params`는 A의 모의매매가 하는 것과 같은 다이제스트 검사로 그 파일이 A의 진짜 잠금인지 확인한다. A-ETH 지문은 A와 같은 형식으로 `aeth/` 아래 파일만 해시하고, 모의매매의 구간 지문은 A 지문과 A-ETH 지문의 쌍이다(어느 쪽이 바뀌어도 ETH 카운트가 새로 시작한다). `tests/aeth/test_isolation.py`는 이 계획 전체의 안전장치다: A 지문 고정값, `aeth/`가 A 지문 대상 밖인지, `master`와의 merge-base 이후 A-ETH 경로를 바꾼 브랜치가 A·B의 경로(지문 대상, `data/`, `backtest/`, `paper/`, `live/`, `stratb/`, `paperb/`, `holdout.lock`, 세 워크플로, `scripts/`, `ops/`, `deploy/`, `uv.lock`, `pyproject.toml`)를 하나도 바꾸지 않았는지(추적되지 않은 새 파일 포함; `master`가 없는 얕은 클론에서는 건너뜀), 리터럴 `unlock_2024=True`를 쓰는 모듈이 `aeth/holdout.py`·`aeth/paper.py`뿐이고 변수로 넘기는 곳이 없는지, A-ETH 코드가 A·B의 산출물 이름을 문자열로 쓰지 않는지, `stratb`·`paperb`를 import하지 않는지 확인한다. 집합 검사는 부분집합(`<=`)이라 아직 없는 모듈이 있어도 통과한다. CI에서 도는 부분(`tests/paper/test_static.py`)에는 B의 같은 검사 옆에 A-ETH 검사 1개를 더한다.

- [ ] **Step 1: 실패하는 테스트 작성**

테스트 패키지와 테스트 파일 3개를 만들고 `tests/paper/test_static.py`에 CI 검사 1개를 더한다. 설정 테스트는 상수, ETH 비용, 비용 배수, 실제 `holdout.lock`의 파라미터(읽기만), 실행 설정, A의 잠금이 아닌 파일의 거부(이유별)를 확인한다. 지문 테스트는 임시 사본에서 A-ETH 편집·A 편집·둘 밖의 편집이 각 지문에 주는 영향을 확인한다. `src/ewbot/aeth/` 폴더는 아직 만들지 않는다(만들면 네임스페이스 패키지로 읽혀 Step 2의 메시지가 달라진다).

```bash
mkdir -p tests/aeth
```

**파일 `tests/aeth/__init__.py`** (새 파일, 전체 내용)

```python
"""Tests for ewbot.aeth (Plan 5a, A-ETH)."""
```

**파일 `tests/aeth/test_config.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import dataclasses
import json

import pytest

from ewbot.aeth.config import (
    A_LOCK_PATH,
    CONTRACT_SIZE,
    DATA_START,
    FROZEN_A_FINGERPRINT,
    FUTURE,
    LOCK_PATH,
    PRODUCT,
    RANDOM_RUNS,
    STAGE1_PATH,
    SUB_START,
    AethError,
    aeth_config,
    config_json,
    eth_costs,
    frozen_a_params,
    scale_trading_costs,
)
from ewbot.backtest.holdout import holdout_config
from ewbot.config import CONSERVATIVE, BotConfig, BreakerParams, CostParams
from ewbot.paper.fingerprint import PROJECT_ROOT
from tests.paper.helpers import make_lock, synth_frame


def test_product_contract_windows_and_paths():
    assert (PRODUCT, FUTURE, CONTRACT_SIZE) == ("ETH-USD", "ETP-20DEC30-CDE", 0.1)
    assert (DATA_START, SUB_START, RANDOM_RUNS) == (1_483_228_800, 1_546_300_800, 100)  # 2017-01-01, 2019-01-01
    assert FROZEN_A_FINGERPRINT.startswith("e0435223")
    assert (LOCK_PATH, STAGE1_PATH) == (PROJECT_ROOT / "holdout-aeth.lock", PROJECT_ROOT / "aeth-stage1.json")
    assert A_LOCK_PATH == PROJECT_ROOT / "holdout.lock"


def test_eth_costs_are_a_s_costs_with_the_etp_contract_size():
    assert eth_costs() == dataclasses.replace(CostParams(), contract_size=0.1)
    assert (eth_costs().fee_rate, eth_costs().slippage, eth_costs().funding_apr_long) == (0.0005, 0.0005, 0.10)
    assert eth_costs().apply_min_fee is False and eth_costs().fee_min_per_contract == 0.15


def test_scale_trading_costs_doubles_fees_and_slippage_only():
    c = scale_trading_costs(eth_costs(), 2.0)
    assert (c.fee_rate, c.fee_min_per_contract, c.slippage) == (0.001, 0.3, 0.001)
    assert (c.funding_apr_long, c.contract_size, c.apply_min_fee) == (0.10, 0.1, False)


def test_the_frozen_params_are_those_of_strategy_a_s_committed_holdout_lock():
    """Reads (never writes) the real holdout.lock: A's walk-forward pick K4=2.0, Q_MIN=0.7, S_MIN=0.5."""
    params, sha = frozen_a_params()
    assert params == (2.0, 0.7, 0.5)
    assert sha == json.loads(A_LOCK_PATH.read_text(encoding="utf-8"))["sha256"]


def test_aeth_config_is_a_s_holdout_config_with_eth_costs():
    params = (2.0, 0.7, 0.5)
    cfg = aeth_config(params)
    assert cfg == dataclasses.replace(holdout_config(BotConfig(), params), costs=eth_costs())
    assert (cfg.wave.K4, cfg.strategy.Q_MIN, cfg.strategy.S_MIN, cfg.strategy.mode) == (2.0, 0.7, 0.5, "A")
    assert cfg.risk == CONSERVATIVE and cfg.breakers == BreakerParams() and cfg.breakers.enabled
    assert cfg.initial_equity == 10_000.0 and cfg.costs.contract_size == 0.1
    assert config_json(params)["costs"]["contract_size"] == 0.1
    assert config_json(params)["strategy"]["setups_enabled"] == ["W3", "W5", "W5_REV", "C_END"]


@pytest.mark.parametrize("case, why", [
    ("missing", "not strategy A's completed"), ("digest", "not the digest"), ("params", "are not strategy A's"),
    ("keys", "are not strategy A's"), ("start", "are not strategy A's"), ("not_run", "not strategy A's completed"),
])
def test_frozen_params_refuse_anything_but_a_s_completed_lock(tmp_path, case, why):
    lock = make_lock(synth_frame())
    if case == "digest":
        lock["sha256"] = "0" * 64
    if case == "params":
        lock = make_lock(synth_frame(), params=(2.2, 0.5, 0.0))
    if case == "keys":
        lock["keys"] = ["wave.K4", "strategy.Q_MIN"]
    if case == "start":
        lock["holdout_start"] = 0
    if case == "not_run":
        lock.pop("first_run")
    path = tmp_path / "holdout.lock"
    if case != "missing":
        path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(AethError, match=why):
        frozen_a_params(path)
```

**파일 `tests/aeth/test_fingerprint.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import shutil

import pytest

from ewbot.aeth.fingerprint import (
    AETH_PREFIX,
    aeth_files,
    aeth_fingerprint,
    current_epoch_fingerprint,
    epoch_fingerprint,
)
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of


def _copy_src(tmp_path):
    shutil.copytree(PROJECT_ROOT / "src" / "ewbot", tmp_path / "src" / "ewbot")
    return tmp_path


def test_aeth_files_are_every_py_file_below_aeth():
    files = aeth_files()
    pkg = PROJECT_ROOT / "src" / "ewbot" / "aeth"
    assert set(files) == {AETH_PREFIX + f.relative_to(pkg).as_posix() for f in pkg.rglob("*.py")}
    assert aeth_fingerprint() == fingerprint_of(files)


def test_an_aeth_edit_changes_the_aeth_and_epoch_fingerprints_but_not_strategy_a_s(tmp_path):
    root = _copy_src(tmp_path)
    a0, e0, ep0 = current_fingerprint(root), aeth_fingerprint(root), current_epoch_fingerprint(root)
    cfg = root / "src/ewbot/aeth/config.py"
    cfg.write_bytes(cfg.read_bytes() + b"# one comment line\n")
    assert current_fingerprint(root) == a0
    assert aeth_fingerprint(root) != e0 and current_epoch_fingerprint(root) != ep0


def test_a_strategy_a_edit_changes_the_epoch_fingerprint_but_not_aeth_s(tmp_path):
    root = _copy_src(tmp_path)
    e0, ep0 = aeth_fingerprint(root), current_epoch_fingerprint(root)
    loop = root / "src/ewbot/engine/loop.py"
    loop.write_bytes(loop.read_bytes() + b"# one comment line\n")
    assert aeth_fingerprint(root) == e0 and current_epoch_fingerprint(root) != ep0


def test_files_outside_both_change_neither(tmp_path):
    root = _copy_src(tmp_path)
    ep0 = current_epoch_fingerprint(root)
    for rel in ("cli.py", "live/app.py", "backtest/runner.py", "paper/runner.py"):
        (root / "src/ewbot" / rel).write_text("# tweak\n", encoding="utf-8")
    assert current_epoch_fingerprint(root) == ep0


def test_epoch_fingerprint_is_the_ordered_pair():
    assert epoch_fingerprint("a", "b") != epoch_fingerprint("b", "a")
    assert current_epoch_fingerprint() == epoch_fingerprint(current_fingerprint(), aeth_fingerprint())


def test_no_aeth_files_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        aeth_fingerprint(tmp_path)
```

**파일 `tests/aeth/test_isolation.py`** (새 파일, 전체 내용)

```python
"""Strategy-A protection (Plan 5a spec 3, 11): A-ETH must not change anything strategy A's paper, live or go-live
count depends on, nor strategy B's ended code. These run in the full suite; tests/paper/test_static.py (run by
ci.yml) carries the CI part."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import ewbot
from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, is_fingerprinted, tree_files

SRC = Path(ewbot.__file__).resolve().parent
FROZEN_A = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
# Strategy A's files (fingerprinted code, research code, runtimes, lock, workflows, scripts, lockfiles) and strategy
# B's (ended, kept as a record).
PROTECTED = (
    "src/ewbot/types.py", "src/ewbot/config.py", "src/ewbot/indicators.py", "src/ewbot/data/",
    "src/ewbot/waves/", "src/ewbot/strategy/", "src/ewbot/engine/", "src/ewbot/execution/", "src/ewbot/risk/",
    "src/ewbot/backtest/", "src/ewbot/paper/", "src/ewbot/live/", "src/ewbot/stratb/", "src/ewbot/paperb/",
    "holdout.lock", ".github/workflows/paper.yml", ".github/workflows/ci.yml", ".github/workflows/paper-b.yml",
    "scripts/", "ops/", "deploy/", "uv.lock", "pyproject.toml",
)
AETH_PATHS = ("src/ewbot/aeth/", "tests/aeth/", ".github/workflows/paper-aeth.yml", "docs/aeth.md",
              "holdout-aeth.lock", "aeth-stage1.json")
UNLOCK_TRUE = {"aeth/holdout.py", "aeth/paper.py"}  # the only callers that may read 2024+ ETH candles
# Runtime artifacts of strategy A and strategy B that A-ETH code must never name.
FOREIGN = {"paper.yml", "paper-b.yml", "holdout.lock", "holdout-b.lock", "refs/heads/paper", "origin/paper",
           "paper-b", "BTC-USD_1h.parquet", "ETH-USD_15m.parquet", "btc-usd-1h-", "stratb-15m-"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], capture_output=True, text=True, timeout=60)


def _merge_base() -> str | None:
    for ref in ("master", "origin/master"):
        r = _git("merge-base", "HEAD", ref)
        if r.returncode == 0:
            return r.stdout.strip()
    return None


def _modules(folder: str | None = None):
    root = SRC if folder is None else SRC / folder
    for f in sorted(root.rglob("*.py")) if root.is_dir() else []:
        yield f.relative_to(SRC).as_posix(), ast.parse(f.read_text(encoding="utf-8"), filename=str(f))


def test_strategy_a_fingerprint_is_unchanged():
    assert current_fingerprint() == FROZEN_A


def test_aeth_code_is_outside_strategy_a_fingerprinted_folders():
    assert not is_fingerprinted("aeth/x.py") and not is_fingerprinted("aeth/sub/y.py")
    assert [rel for rel, _ in _modules("aeth")]
    assert not [p for p in tree_files() if p.startswith("src/ewbot/aeth/")]


def test_aeth_work_changes_no_protected_path():
    """Since the merge-base with master (tracked changes and new untracked files): a branch that touches A-ETH
    touches no strategy-A or strategy-B path. A branch that does not touch A-ETH is not concerned."""
    base = _merge_base()
    if base is None:
        pytest.skip("no master branch in this clone (shallow CI checkout)")
    diff = _git("diff", "--name-only", base)
    new = _git("ls-files", "--others", "--exclude-standard")
    assert diff.returncode == 0 and new.returncode == 0
    changed = set(diff.stdout.split()) | set(new.stdout.split())
    if any(p.startswith(AETH_PATHS) for p in changed):
        assert sorted(p for p in changed if p.startswith(PROTECTED)) == []


def test_only_the_aeth_holdout_and_paper_runtime_unlock_2024_candles():
    literal_true, other = set(), set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "unlock_2024":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            literal_true.add(rel)
                        elif not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                            other.add(rel)
    assert literal_true <= UNLOCK_TRUE
    assert other == set()


def test_aeth_code_never_names_strategy_a_or_b_runtime_artifacts():
    for rel, tree in _modules("aeth"):
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not names & FOREIGN, rel


def test_aeth_never_imports_strategy_b():
    for rel, tree in _modules("aeth"):
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [m for m in mods if m.startswith(("ewbot.stratb", "ewbot.paperb"))], rel
```

**수정 `tests/paper/test_static.py`** (1/1) — 다음 부분을 찾아:

```python
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratb/", "src/ewbot/paperb/"))]
```

바꿀 내용:

```python
    assert not [p for p in tree_files() if p.startswith(("src/ewbot/stratb/", "src/ewbot/paperb/"))]


def test_aeth_code_is_outside_the_strategy_a_fingerprint():
    """Plan 5a spec 3, 11 (A protection, run by ci.yml): A-ETH lives in aeth/ only, and none of its files is in
    A's fingerprinted set (A's fingerprint itself is pinned in test_fingerprint.py)."""
    from ewbot.paper.fingerprint import is_fingerprinted, tree_files

    files = [f.relative_to(SRC).as_posix() for f in (SRC / "aeth").rglob("*.py")]
    assert files and not [r for r in files if is_fingerprinted(r)]
    assert not [p for p in tree_files() if p.startswith("src/ewbot/aeth/")]
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/aeth tests/paper/test_static.py -q
```

예상: `No module named 'ewbot.aeth'`
예상: `2 errors`

`ewbot.aeth`가 아직 없어 두 파일이 수집 단계에서 실패하고 실행이 중단된다(`test_isolation.py`와 `test_static.py`는 수집되지만 실행되지 않는다).

- [ ] **Step 3: 최소 구현**

```bash
mkdir -p src/ewbot/aeth
```

**파일 `src/ewbot/aeth/__init__.py`** (새 파일, 전체 내용)

```python
"""A-ETH (Plan 5a): strategy A, frozen, on ETH-USD - stage 1 (2017-2023), a one-shot holdout, paper trading.

Every *.py file below this folder is hashed by ewbot.aeth.fingerprint.aeth_fingerprint. None of it is part of
strategy A's fingerprinted set; strategy A's modules are imported, never changed."""
```

**파일 `src/ewbot/aeth/config.py`** (새 파일, 전체 내용)

```python
"""A-ETH run configuration (Plan 5a spec 5): strategy A exactly as frozen, on ETH-USD. Nothing here is tuned.

The params come from strategy A's committed holdout.lock (K4, Q_MIN, S_MIN), which must be A's real, completed
lock: its sha256 is lock_digest(BotConfig(), params, random_runs, code_sha256), the check A's own paper runtime
makes. The rest is holdout_config(BotConfig(), params) - A's conservative profile, A's circuit breakers, A's wave
and strategy defaults - with A's CostParams whose contract size is that of ETP-20DEC30-CDE (0.1 ETH). Fee 0.05%,
slippage 0.05% and long-pays funding of 10% a year stay A's; the 0.15 USD minimum fee per contract is not applied,
as in A (apply_min_fee=False)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from ewbot.backtest.holdout import DEFAULT_LOCK_PATH as A_LOCK_PATH
from ewbot.backtest.holdout import holdout_config, lock_digest
from ewbot.backtest.runner import utc_ts
from ewbot.backtest.walkforward import GRID, HOLDOUT_START, WF_START, grid_combos
from ewbot.config import BotConfig, CostParams
from ewbot.paper.fingerprint import PROJECT_ROOT

PRODUCT = "ETH-USD"  # Coinbase spot candles for the signals, like A's BTC-USD
FUTURE = "ETP-20DEC30-CDE"  # the modelled contract (only Plan 5c would trade it)
CONTRACT_SIZE = 0.1  # ETH per ETP contract
FROZEN_A_FINGERPRINT = "e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1"
DATA_START = utc_ts(WF_START)  # 2017-01-01: the first ETH bar fetched and the start of stage 1
SUB_START = utc_ts("2019-01-01")  # stage 1, information only: 2019-01-01..2023-12-31
RANDOM_RUNS = 100  # strategy A's random-entry control (compare() default, holdout.lock random_runs)
COST_INFO_MULT = 2.0  # stage 1, information only: A with fees and slippage x 2
LOCK_PATH = PROJECT_ROOT / "holdout-aeth.lock"
STAGE1_PATH = PROJECT_ROOT / "aeth-stage1.json"

Combo = tuple[float, ...]


class AethError(RuntimeError):
    """An A-ETH step is refused: the lock, the stage-1 record, the data or the code state is not as required."""


def eth_costs() -> CostParams:
    """Strategy A's CostParams with the ETP contract size; every other field is A's default."""
    return dataclasses.replace(CostParams(), contract_size=CONTRACT_SIZE)


def scale_trading_costs(c: CostParams, mult: float) -> CostParams:
    """Fee rate, minimum fee and slippage times `mult`; funding and contract size unchanged."""
    return dataclasses.replace(
        c, fee_rate=c.fee_rate * mult, fee_min_per_contract=c.fee_min_per_contract * mult, slippage=c.slippage * mult
    )


def frozen_a_params(a_lock_path: Path = A_LOCK_PATH) -> tuple[Combo, str]:
    """(params, sha256) of strategy A's completed holdout lock - read only. AethError unless its keys are A's grid
    keys, its params a grid combo, its holdout start A's and its sha256 lock_digest(BotConfig(), params,
    random_runs, code_sha256)."""
    p = Path(a_lock_path)
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
        params = tuple(float(x) for x in lock["params"])
        keys = list(lock["keys"])
        sha = str(lock["sha256"])
        holdout_start = int(lock["holdout_start"])
        digest = lock_digest(BotConfig(), params, int(lock["random_runs"]), str(lock["code_sha256"]))
        int(lock["first_run"]["criteria"]["n_trades"]["A"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AethError(f"{p} is not strategy A's completed holdout lock: {exc!r}") from exc
    if keys != list(GRID) or params not in grid_combos() or holdout_start != HOLDOUT_START:
        raise AethError(f"{p}: keys {keys}, params {list(params)} or holdout start {holdout_start} are not "
                        "strategy A's")
    if digest != sha:
        raise AethError(f"{p}: sha256 {sha[:12]} is not the digest of its config, params and code ({digest[:12]})")
    return params, sha


def aeth_config(params: Combo) -> BotConfig:
    """The full A-ETH run config: strategy A's holdout config for `params`, with the ETH costs."""
    return dataclasses.replace(holdout_config(BotConfig(), tuple(float(p) for p in params)), costs=eth_costs())


def config_json(params: Combo) -> dict[str, Any]:
    """aeth_config(params) as JSON (what the stage-1 record and the lock store and compare)."""
    return json.loads(json.dumps(dataclasses.asdict(aeth_config(params))))
```

**파일 `src/ewbot/aeth/fingerprint.py`** (새 파일, 전체 내용)

```python
"""A-ETH fingerprints (Plan 5a spec 3, 8), in strategy A's format (ewbot.paper.fingerprint.fingerprint_of: sorted
repo-relative paths, each path NUL bytes NUL).

aeth_fingerprint: every *.py file below src/ewbot/aeth - A-ETH's own code (ETH costs, data, stages, runtime). It
is outside strategy A's fingerprinted folders and no A file is in it, so either can change without the other.
epoch_fingerprint: what a paper-A-ETH epoch is bound to - strategy A's fingerprint (the strategy files) and the
A-ETH fingerprint together. A change of either opens a new epoch and restarts the ETH count."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ewbot.paper.fingerprint import PROJECT_ROOT, current_fingerprint, fingerprint_of

AETH_PREFIX = "src/ewbot/aeth/"


def aeth_files(repo_root: Path | None = None) -> dict[str, bytes]:
    """{"src/ewbot/aeth/<rel>": bytes} for every *.py file below src/ewbot/aeth."""
    pkg = (PROJECT_ROOT if repo_root is None else Path(repo_root)) / "src" / "ewbot" / "aeth"
    return {AETH_PREFIX + f.relative_to(pkg).as_posix(): f.read_bytes() for f in pkg.rglob("*.py") if f.is_file()}


def aeth_fingerprint(repo_root: Path | None = None) -> str:
    files = aeth_files(repo_root)
    if not files:
        raise FileNotFoundError(f"no A-ETH source files below {repo_root or PROJECT_ROOT}/src/ewbot/aeth")
    return fingerprint_of(files)


def epoch_fingerprint(a_fingerprint: str, aeth_sha256: str) -> str:
    """sha256 of the pair (strategy A's fingerprint, the A-ETH fingerprint)."""
    return hashlib.sha256(f"strategy-a:{a_fingerprint}\naeth:{aeth_sha256}\n".encode("utf-8")).hexdigest()


def current_epoch_fingerprint(repo_root: Path | None = None) -> str:
    return epoch_fingerprint(current_fingerprint(repo_root), aeth_fingerprint(repo_root))
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/aeth tests/paper/test_static.py -q
```

예상: `51 passed`

`tests/aeth` 23개와 `tests/paper/test_static.py` 28개(기존 27개 + 새 CI 검사 1개)다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/aeth/__init__.py src/ewbot/aeth/config.py src/ewbot/aeth/fingerprint.py tests/aeth/__init__.py tests/aeth/test_config.py tests/aeth/test_fingerprint.py tests/aeth/test_isolation.py tests/paper/test_static.py
git commit -m "feat(aeth): A-ETH config with strategy A's frozen params and ETH costs, fingerprints and the strategy-A protection guard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: 잠금 파일 기본 기능, ETH 데이터와 2024 가드

**Files:**
- Create: `src/ewbot/aeth/lockfile.py`
- Create: `src/ewbot/aeth/data.py`
- Test: `tests/aeth/helpers.py`
- Test: `tests/aeth/test_data.py`

**Interfaces:**
- Consumes: Task 1: `ewbot.aeth.config`: `AethError`, `DATA_START`, `LOCK_PATH`, `PRODUCT`. A: `ewbot.backtest.report.to_jsonable(o) -> Any`; `ewbot.data.coinbase_history`: `COLUMNS = ["ts", "open", "high", "low", "close", "volume"]`, `fetch_1h(product, start_ts, end_ts, session=None, sleep_s=0.15) -> DataFrame`(열린 시각이 [start_ts, end_ts)인 마감 봉); `ewbot.backtest.walkforward.HOLDOUT_START`; `ewbot.types.H1`. 테스트: `tests.paper.helpers`: `BOUNDARY = 1704844800`(2024-01-10 00:00), `git(repo, *args)`, `make_lock`, `synth_frame`.
- Produces: `ewbot.aeth.lockfile`: `LOCK_KIND = "ewbot-aeth-holdout"`, `LOCK_VERSION = 1`, `LOCK_BOUND`(해석 5의 15개 필드 이름 튜플), `lock_digest(lock: Mapping) -> str`, `lock_exists(lock_path: Path) -> bool`, `read_lock(lock_path: Path) -> dict`(유효하지 않으면 `AethError`), `write_json(path: Path, obj) -> None`(엄격한 JSON, 원자적), `committed_text(path: Path) -> str | None`(그 작업 트리의 git HEAD 내용), `dirty_files(anchor: Path, *ignore: str) -> list[str]`, `refuse_dirty(anchor: Path, *ignore: str) -> None`(`AethError`).
  `ewbot.aeth.data`: `CHUNK_HOURS = 3000`, `Fetch = Callable[[str, int, int], DataFrame]`, `class HoldoutGuardError(AethError)`, `cache_path(cache_dir: Path) -> Path`(`<cache_dir>/ETH-USD_1h.parquet`), `fetch_eth(cache_dir: Path, *, lock_path: Path = LOCK_PATH, now_ts: int | None = None, fetch: Fetch = fetch_1h) -> Path`, `load_eth(path: Path, *, unlock_2024: bool = False, lock_path: Path = LOCK_PATH) -> DataFrame`(표준 열, 정렬, ts 중복 없음).
  `tests/aeth/helpers.py`: `HS = 1704067200`, `ETH_SCALE = 0.1`, `HOLDOUT_TRADES = 2`, `LOCK_NOW = BOUNDARY + 3600 + 7`, `eth_frame() -> DataFrame`, `pre2024_frame() -> DataFrame`(마지막 봉 2023-12-31 23:00, 3,000개), `save_cache(cache_dir, df) -> Path`, `init_repo(repo) -> Path`(커밋 하나가 있는 git 저장소), `commit_file(repo, name, text) -> Path`(바뀌었을 때만 커밋), `write_a_lock(directory, **overrides) -> Path`(합성 A `holdout.lock`, 파라미터 (2.0, 0.5, 0.0)), `a_lock_sha(a_lock) -> str`, `class FakeFetch1h(source)`(`fetch_1h` 대역, `calls`에 `(product, start, end)` 기록), `class FakeCacheFetch(source, clock, fail=0)`(모의매매 `default_fetch` 대역, Task 5).

잠금 파일의 기본 기능(다이제스트, 유효성, 커밋 상태, 작업 트리 검사)은 데이터 가드·홀드아웃·모의매매가 함께 쓰므로 순환 import 없이 `lockfile.py`에 따로 둔다(해석 5). `lock_exists`는 종류와 다이제스트가 맞는 파일만 잠금으로 본다: 이름이나 모양만 같은 파일은 2024를 열지 못한다. `fetch_eth`는 A의 `fetch_1h`를 3,000시간 조각으로 부르고 조각마다 원자적으로 저장해 중단되어도 이어 받으며, 잠금이 없으면 끝을 2024-01-01로 자른다(그 너머 구간은 요청 전에 `HoldoutGuardError`). `load_eth`는 parquet을 읽을 때 2024 이후를 걸러내고, `unlock_2024=True`는 유효한 잠금이 있어야 한다. 잠금 없이 캐시에 2024 이후 봉이 있으면 둘 다 거부한다(해석 10). 테스트 도우미는 합성 ETH 봉(해석 14), 임시 git 저장소, 합성 A 잠금, 가짜 수집기를 준다. `FakeCacheFetch`는 Task 5가 쓴다.

- [ ] **Step 1: 실패하는 테스트 작성**

데이터 테스트는 잠금 판정(없음, JSON 아님, 다른 종류, 다이제스트 불일치), 다이제스트가 정확히 바인딩 필드만 덮는지, 커밋된 내용과 더러운 작업 트리, 잠금 전 수집의 조각과 2024 경계, 중단 뒤 이어 받기, 잠금 뒤 지금까지의 수집(진행 중인 시간 제외), 로더의 필터와 열기 조건, 잠금 없는 2024 봉의 거부를 확인한다.

**파일 `tests/aeth/helpers.py`** (새 파일, 전체 내용)

```python
"""Shared helpers for tests/aeth: synthetic ETH-priced 1h bars (strategy A's paper-test path,
tests.paper.helpers.synth_frame, with prices x 0.1: the same A trades with 0.1-ETH contracts), a synthetic
strategy-A holdout.lock with the paper tests' params, git repos and fake candle fetchers.

Everything is synthetic: no test uses the network or the real data/cache, and no test writes the real holdout.lock,
aeth-stage1.json or holdout-aeth.lock (tests/aeth/test_config.py only reads the real holdout.lock)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.aeth.config import PRODUCT
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.types import H1
from tests.paper.helpers import BOUNDARY, git, make_lock, synth_frame

HS = HOLDOUT_START
ETH_SCALE = 0.1
HOLDOUT_TRADES = 2  # synthetic A trades entering in [HOLDOUT_START, BOUNDARY] (as in tests/paper)
LOCK_NOW = BOUNDARY + H1 + 7  # a lock written now has last bar BOUNDARY (2024-01-10 00:00 UTC)


def eth_frame() -> pd.DataFrame:
    """tests.paper.helpers.synth_frame (2023-11-15 .. 2024-03-18 UTC) with prices x 0.1."""
    df = synth_frame()
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * ETH_SCALE
    return df


def pre2024_frame() -> pd.DataFrame:
    """eth_frame shifted back so that its last bar is 2023-12-31 23:00 UTC (3000 bars, all before 2024)."""
    df = eth_frame()
    df["ts"] = df["ts"] - (int(df["ts"].iloc[-1]) - (HS - H1))
    return df


def save_cache(cache_dir: Path, df: pd.DataFrame) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{PRODUCT}_1h.parquet"
    df.reset_index(drop=True).to_parquet(path, index=False)
    return path


def init_repo(repo: Path) -> Path:
    """A git repo with one committed file, so that HEAD exists."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q")
        (repo / "README.md").write_text("synthetic\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-q", "-m", "init")
    return repo


def commit_file(repo: Path, name: str, text: str) -> Path:
    """Write <repo>/<name> and commit it (when it changed)."""
    init_repo(repo)
    p = repo / name
    p.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode != 0:
        git(repo, "commit", "-q", "-m", name)
    return p


def write_a_lock(directory: Path, **overrides: Any) -> Path:
    """A synthetic strategy-A holdout.lock with the paper tests' params (2.0, 0.5, 0.0) and a valid digest
    (tests.paper.helpers.make_lock; `overrides` change its fields before the digest)."""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / "holdout.lock"
    p.write_text(json.dumps(make_lock(synth_frame(), **overrides), indent=2), encoding="utf-8")
    return p


def a_lock_sha(a_lock: Path) -> str:
    return json.loads(a_lock.read_text(encoding="utf-8"))["sha256"]


class FakeFetch1h:
    """Stands in for A's fetch_1h(product, start_ts, end_ts): the bars of `source` with open time in [start, end).
    Records every call."""

    def __init__(self, source: pd.DataFrame) -> None:
        self.source, self.calls = source, []

    def __call__(self, product: str, start_ts: int, end_ts: int) -> pd.DataFrame:
        self.calls.append((product, int(start_ts), int(end_ts)))
        s = self.source
        return s[(s["ts"] >= start_ts) & (s["ts"] < end_ts)].reset_index(drop=True)


class FakeCacheFetch:
    """Stands in for paper.default_fetch(cache_dir, lock_path): writes the bars of `source` closed by clock() to the
    cache; raises for the next `fail` calls."""

    def __init__(self, source: pd.DataFrame | Callable[[], pd.DataFrame], clock: Callable[[], int], fail: int = 0):
        self.source, self.clock, self.fail, self.calls = source, clock, fail, 0

    def __call__(self, cache_dir: Path, lock_path: Path) -> Path:
        self.calls += 1
        if self.fail > 0:
            self.fail -= 1
            raise ConnectionError("coinbase down")
        df = self.source() if callable(self.source) else self.source
        return save_cache(Path(cache_dir), df[df["ts"] < int(self.clock()) // H1 * H1])
```

**파일 `tests/aeth/test_data.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json

import pandas as pd
import pytest

from ewbot.aeth.config import DATA_START, AethError
from ewbot.aeth.data import CHUNK_HOURS, HoldoutGuardError, cache_path, fetch_eth, load_eth
from ewbot.aeth.lockfile import (
    LOCK_BOUND,
    LOCK_KIND,
    committed_text,
    dirty_files,
    lock_digest,
    lock_exists,
    read_lock,
    refuse_dirty,
)
from ewbot.types import H1
from tests.aeth.helpers import HS, FakeFetch1h, commit_file, eth_frame, init_repo, pre2024_frame, save_cache

NOW = HS + 40 * 86_400 + 1_800  # 2024-02-10 00:30 UTC: the last closed hour opened at 2024-02-09 23:00


def _valid_lock(path):
    lock = {"kind": LOCK_KIND, "version": 1, "params": [2.0, 0.5, 0.0]}
    lock["sha256"] = lock_digest(lock)
    path.write_text(json.dumps(lock), encoding="utf-8")
    return path


@pytest.mark.parametrize("case", ["missing", "not_json", "other_kind", "digest"])
def test_only_a_lock_whose_sha256_is_its_digest_exists(tmp_path, case):
    p = tmp_path / "holdout-aeth.lock"
    lock = {"kind": LOCK_KIND, "version": 1}
    lock["sha256"] = lock_digest(lock)
    if case == "not_json":
        p.write_text("{", encoding="utf-8")
    if case == "other_kind":
        p.write_text(json.dumps({**lock, "kind": "ewbot-stratb-holdout"}), encoding="utf-8")
    if case == "digest":
        p.write_text(json.dumps({**lock, "version": 2}), encoding="utf-8")
    assert lock_exists(p) is False
    with pytest.raises(AethError):
        read_lock(p)
    assert lock_exists(_valid_lock(p)) is True and read_lock(p)["kind"] == LOCK_KIND


def test_the_digest_binds_exactly_the_bound_fields():
    lock = {k: f"v-{k}" for k in LOCK_BOUND}
    d = lock_digest(lock)
    assert lock_digest({**lock, "first_run": {"x": 1}, "attempts": [1], "locked_at": "now"}) == d
    assert all(lock_digest({**lock, k: "other"}) != d for k in LOCK_BOUND)


def test_committed_text_and_dirty_files(tmp_path):
    repo = init_repo(tmp_path / "repo")
    p = commit_file(repo, "holdout-aeth.lock", "one\n")
    assert committed_text(p) == "one\n" and committed_text(repo / "nope.json") is None
    assert dirty_files(p) == []
    p.write_text("two\n", encoding="utf-8")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    assert dirty_files(p) == ["README.md", "holdout-aeth.lock"]
    assert dirty_files(p, p.name) == ["README.md"]
    with pytest.raises(AethError, match="uncommitted changes"):
        refuse_dirty(p, p.name)
    with pytest.raises(AethError, match="not a git working tree"):
        dirty_files(tmp_path / "elsewhere" / "x.json")


def test_fetch_before_the_lock_stops_at_2024_in_saved_chunks(tmp_path):
    src = eth_frame()
    fake = FakeFetch1h(src)
    path = fetch_eth(tmp_path / "cache", lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=fake)
    assert path == cache_path(tmp_path / "cache") and path.name == "ETH-USD_1h.parquet"
    assert fake.calls[0][:2] == ("ETH-USD", DATA_START)
    assert all(end - start <= CHUNK_HOURS * H1 for _, start, end in fake.calls)
    assert max(end for _, _, end in fake.calls) == HS  # no window reaches 2024-01-01
    got = pd.read_parquet(path)
    assert int(got["ts"].max()) == HS - H1 and got.equals(src[src["ts"] < HS].reset_index(drop=True))
    fake.calls.clear()
    fetch_eth(tmp_path / "cache", lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=fake)
    assert fake.calls == []  # nothing new before 2024


def test_an_interrupted_fetch_resumes_from_the_last_saved_chunk(tmp_path):
    src = pre2024_frame()  # 2023-08-29 .. 2023-12-31: two chunks hold bars
    fake = FakeFetch1h(src)

    def flaky(product, start, end):
        if start > int(src["ts"].iloc[0]):  # the chunk after the first one with bars fails
            raise ConnectionError("coinbase down")
        return fake(product, start, end)

    with pytest.raises(ConnectionError):
        fetch_eth(tmp_path, lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=flaky)
    saved = int(pd.read_parquet(cache_path(tmp_path))["ts"].max())
    assert int(src["ts"].iloc[0]) <= saved < HS - H1
    fake.calls.clear()
    fetch_eth(tmp_path, lock_path=tmp_path / "missing.lock", now_ts=NOW, fetch=fake)
    assert fake.calls[0][1] == saved + H1 and int(pd.read_parquet(cache_path(tmp_path))["ts"].max()) == HS - H1


def test_with_the_lock_fetch_extends_to_the_last_closed_hour(tmp_path):
    src = eth_frame()
    lock = _valid_lock(tmp_path / "holdout-aeth.lock")
    save_cache(tmp_path / "cache", src[src["ts"] < HS])
    fake = FakeFetch1h(src)
    path = fetch_eth(tmp_path / "cache", lock_path=lock, now_ts=NOW, fetch=fake)
    assert fake.calls[0][1] == HS and max(end for _, _, end in fake.calls) == NOW // H1 * H1
    assert int(pd.read_parquet(path)["ts"].max()) == NOW // H1 * H1 - H1  # the hour in progress is not stored


def test_load_filters_2024_out_until_the_lock_unlocks_it(tmp_path):
    src = eth_frame()
    lock_path = tmp_path / "holdout-aeth.lock"
    path = save_cache(tmp_path / "cache", src[src["ts"] < HS])
    assert int(load_eth(path, lock_path=lock_path)["ts"].max()) == HS - H1
    with pytest.raises(HoldoutGuardError):
        load_eth(path, unlock_2024=True, lock_path=lock_path)
    _valid_lock(lock_path)
    save_cache(tmp_path / "cache", src)
    assert int(load_eth(path, lock_path=lock_path)["ts"].max()) == HS - H1  # still filtered without unlock_2024
    full = load_eth(path, unlock_2024=True, lock_path=lock_path)
    assert full.equals(src.astype({"ts": "int64"}).reset_index(drop=True))


def test_2024_bars_without_a_lock_did_not_come_from_aeth_and_are_refused(tmp_path):
    """E.g. strategy A's `ewbot fetch --product ETH-USD` writes the same file up to now."""
    path = save_cache(tmp_path / "cache", eth_frame())
    lock_path = tmp_path / "missing.lock"
    with pytest.raises(HoldoutGuardError, match="did not come from A-ETH"):
        load_eth(path, lock_path=lock_path)
    with pytest.raises(HoldoutGuardError, match="did not come from A-ETH"):
        fetch_eth(tmp_path / "cache", lock_path=lock_path, now_ts=NOW, fetch=FakeFetch1h(eth_frame()))
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/aeth/test_data.py -q
```

예상: `No module named 'ewbot.aeth.data'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/aeth/lockfile.py`** (새 파일, 전체 내용)

```python
"""holdout-aeth.lock primitives (Plan 5a spec 7.1) and the git checks of A-ETH's records.

The lock's sha256 is the sha256 of the sorted compact JSON of its LOCK_BOUND fields. lock_exists is True only for
a JSON object of kind LOCK_KIND whose sha256 is that digest - a file that merely has the name or the shape does
not unlock 2024 (data.py). committed_text is a file's content in git HEAD of its own working tree; dirty_files
lists the tracked files of that working tree with uncommitted changes (the stage-1 record, the lock and the
holdout are made on a clean tree)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

from ewbot.aeth.config import AethError
from ewbot.backtest.report import to_jsonable

LOCK_KIND = "ewbot-aeth-holdout"
LOCK_VERSION = 1
LOCK_BOUND = ("kind", "version", "product", "future", "params", "a_lock_sha256", "config", "holdout_start",
              "last_bar_ts", "random_runs", "a_fingerprint", "aeth_sha256", "code_sha256", "pre2024", "stage1_sha256")


def lock_digest(lock: Mapping[str, Any]) -> str:
    """sha256 of the sorted compact JSON of the lock's LOCK_BOUND fields."""
    blob = json.dumps({k: lock.get(k) for k in LOCK_BOUND}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def lock_exists(lock_path: Path) -> bool:
    p = Path(lock_path)
    if not p.is_file():
        return False
    try:
        lock = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(lock, dict) and lock.get("kind") == LOCK_KIND and lock.get("sha256") == lock_digest(lock)


def read_lock(lock_path: Path) -> dict[str, Any]:
    if not lock_exists(lock_path):
        raise AethError(f"{lock_path} is missing or not a valid A-ETH lock (its sha256 must be the digest of its "
                        "fields)")
    return json.loads(Path(lock_path).read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    """Strict JSON, atomically (a temp file next to `path`, then os.replace)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(to_jsonable(obj), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def committed_text(path: Path) -> str | None:
    """`path`'s content in git HEAD of the working tree it is in; None when it is not committed there."""
    p = Path(path)
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "show", f"HEAD:./{p.name}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AethError(f"git is not available to check {p}: {exc}") from exc
    return r.stdout.decode("utf-8") if r.returncode == 0 else None


def dirty_files(anchor: Path, *ignore: str) -> list[str]:
    """Tracked files with uncommitted changes in the git working tree whose root holds `anchor`, the files named in
    `ignore` (next to `anchor`) excepted."""
    p = Path(anchor)
    cmd = ["git", "-C", str(p.parent), "status", "--porcelain", "--untracked-files=no", "--", ":/",
           *[f":!{name}" for name in ignore]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AethError(f"git is not available to check the working tree: {exc}") from exc
    if r.returncode != 0:
        raise AethError(f"{p.parent} is not a git working tree: {r.stderr.strip()}")
    return [line[3:] for line in r.stdout.splitlines() if line.strip()]


def refuse_dirty(anchor: Path, *ignore: str) -> None:
    dirty = dirty_files(anchor, *ignore)
    if dirty:
        raise AethError(f"uncommitted changes to tracked files {dirty[:5]}{' ...' if len(dirty) > 5 else ''}: "
                        "A-ETH's records are made on a clean tree (commit or discard them)")
```

**파일 `src/ewbot/aeth/data.py`** (새 파일, 전체 내용)

```python
"""ETH-USD 1h candles for A-ETH (Plan 5a spec 5.3): fetch, cache, guarded load.

One cache file, <cache_dir>/ETH-USD_1h.parquet, filled through strategy A's Coinbase fetcher
(ewbot.data.coinbase_history.fetch_1h: the same public endpoint; imported, never changed) in chunks of CHUNK_HOURS,
each saved as it arrives (temp file, then rename), so an interrupted download resumes from the last saved bar. The
hour in progress is never stored and missing bars are not filled in the cache (stage 1, the holdout and the paper
runtime fill them after loading, like A, with fill_gaps). A's update_cache is not used: it always fetches up to now.

2024 guard (spec 5.3): until a valid holdout-aeth.lock exists (lockfile.lock_exists: its kind and digest),
fetch_eth never requests a window that reaches HOLDOUT_START (2024-01-01) and load_eth never returns a bar at or
after it - the parquet read itself filters them out. load_eth(unlock_2024=True) raises HoldoutGuardError until the
lock exists. A cache that holds bars from 2024-01-01 on while no lock exists did not come from A-ETH (strategy A's
`ewbot fetch --product ETH-USD` writes the same file up to now): fetch_eth and load_eth refuse it."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from ewbot.aeth.config import DATA_START, LOCK_PATH, PRODUCT, AethError
from ewbot.aeth.lockfile import lock_exists
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.coinbase_history import COLUMNS, fetch_1h
from ewbot.types import H1

CHUNK_HOURS = 3000  # one saved chunk = 10 requests of 300 bars

Fetch = Callable[[str, int, int], pd.DataFrame]


class HoldoutGuardError(AethError):
    """ETH candles at/after 2024-01-01 were requested, read or found before holdout-aeth.lock exists."""


def cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{PRODUCT}_1h.parquet"


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.Series(dtype="int64"), **{c: pd.Series(dtype="float64") for c in COLUMNS[1:]}})


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    df = df[COLUMNS].astype({"ts": "int64", **{c: "float64" for c in COLUMNS[1:]}})
    return df.drop_duplicates("ts").sort_values("ts", kind="stable").reset_index(drop=True)


def _read(path: Path, before: int | None) -> pd.DataFrame:
    """The cached bars (standard schema); with `before`, the parquet read keeps only ts < before."""
    return _standard(pd.read_parquet(path, filters=None if before is None else [("ts", "<", before)]))


def _holdout_rows(path: Path) -> int:
    """How many cached bars are at/after 2024-01-01 (only their ts column is read)."""
    return len(pd.read_parquet(path, columns=["ts"], filters=[("ts", ">=", HOLDOUT_START)]))


def _refuse_foreign_2024(path: Path) -> None:
    if path.exists() and _holdout_rows(path):
        raise HoldoutGuardError(f"{path} holds ETH bars from 2024-01-01 on but no holdout-aeth.lock exists: they did "
                                "not come from A-ETH - delete the file and run `ewbot aeth fetch` again")


def _save(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def fetch_eth(
    cache_dir: Path, *, lock_path: Path = LOCK_PATH, now_ts: int | None = None, fetch: Fetch = fetch_1h
) -> Path:
    """Create (from 2017-01-01) or extend the ETH 1h cache up to the last CLOSED hour - and, while no valid
    holdout-aeth.lock exists, never past 2024-01-01. `fetch(product, start_ts, end_ts)` returns the bars with open
    time in [start_ts, end_ts) (A's fetch_1h). Network: only here."""
    path = cache_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    unlocked = lock_exists(lock_path)
    if not unlocked:
        _refuse_foreign_2024(path)
    cur = _read(path, None if unlocked else HOLDOUT_START) if path.exists() else _empty()
    now = int(time.time()) if now_ts is None else int(now_ts)
    end = now // H1 * H1  # exclusive: the hour in progress is never stored
    if not unlocked:
        end = min(end, HOLDOUT_START)
    begin = int(cur["ts"].max()) + H1 if len(cur) else DATA_START
    if not path.exists():
        _save(cur, path)
    while begin < end:
        stop = min(begin + CHUNK_HOURS * H1, end)
        if stop > HOLDOUT_START and not lock_exists(lock_path):
            raise HoldoutGuardError(f"{PRODUCT}: bars from 2024-01-01 on cannot be fetched before holdout-aeth.lock")
        page = fetch(PRODUCT, begin, stop)
        page = page[(page["ts"] >= begin) & (page["ts"] < stop)]
        if len(page):
            cur = _standard(pd.concat([cur, page], ignore_index=True))
            _save(cur, path)
        begin = stop
    return path


def load_eth(path: Path, *, unlock_2024: bool = False, lock_path: Path = LOCK_PATH) -> pd.DataFrame:
    """The cached ETH bars (standard schema, sorted, unique ts). Without unlock_2024 the parquet read filters out
    every bar at/after HOLDOUT_START; unlock_2024=True needs a valid holdout-aeth.lock (HoldoutGuardError)."""
    p = Path(path)
    unlocked = lock_exists(lock_path)
    if unlock_2024 and not unlocked:
        raise HoldoutGuardError(f"{lock_path} is not a valid A-ETH lock: ETH bars from 2024-01-01 on cannot be read")
    if not unlocked:
        _refuse_foreign_2024(p)
    return _read(p, None if unlock_2024 else HOLDOUT_START)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/aeth/test_data.py -q
```

예상: `11 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/aeth/lockfile.py src/ewbot/aeth/data.py tests/aeth/helpers.py tests/aeth/test_data.py
git commit -m "feat(aeth): ETH 1h cache through A's fetch_1h that cannot reach 2024 before holdout-aeth.lock; lock primitives" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: 단계 1 — 비교, 판정, 정보 실행, 1회 기록

**Files:**
- Create: `src/ewbot/aeth/stage1.py`
- Test: `tests/aeth/test_stage1.py`

**Interfaces:**
- Consumes: Task 1: `A_LOCK_PATH`, `COST_INFO_MULT`, `DATA_START`, `FROZEN_A_FINGERPRINT`, `FUTURE`, `PRODUCT`, `RANDOM_RUNS`, `STAGE1_PATH`, `SUB_START`, `AethError`, `Combo`, `aeth_config`, `config_json`, `frozen_a_params`, `scale_trading_costs`, `aeth_fingerprint()`. Task 2: `refuse_dirty`, `write_json`; 도우미 `HS`, `a_lock_sha`, `init_repo`, `pre2024_frame`, `write_a_lock`. A: `ewbot.backtest.runner.compare(cfg, df1h, start_ts, end_ts, random_runs=100, workers=1, *, allow_holdout=False) -> dict`(`comparison`, `random`(`runs`, `n_signals`, `dist`), `breakdown`, …), `run_backtest(cfg, df1h, start_ts, end_ts) -> EngineResult`; `ewbot.backtest.metrics.compute_metrics(equity, trades, start_ts, end_ts) -> Metrics`; `ewbot.backtest.holdout`: `code_fingerprint() -> str`, `data_fingerprint(df, start_ts, last_ts) -> str`, `git_state() -> dict`; `ewbot.data.candles`: `check_integrity(df) -> IntegrityReport`, `fill_gaps(df) -> DataFrame`.
- Produces: `ewbot.aeth.stage1`: `STAGE1_KIND = "ewbot-aeth-stage1"`, `MAX_DD = 0.15`, `COVER_SLACK_S = 86400`, `headline(cmp: dict) -> dict`(전략 `A, HYBRID, C, BH, EMA`마다 `total_return, cagr, max_dd, sharpe, calmar, win_rate, avg_r, n_trades`와 `RANDOM`(`runs`, `n_signals`, `sharpe`·`calmar`·`total_return` 분포); 엄격한 JSON), `stage1_verdict(head: dict) -> dict`(`{"checks": {"total_return_gt_0", "max_dd_le_15pct", "sharpe_gt_c", "calmar_gt_c", "sharpe_gt_random_mean", "calmar_gt_random_mean"}: bool, "passed": bool}`), `run_stage1(df1h, params, *, random_runs=RANDOM_RUNS, workers=1, start_ts=DATA_START, sub_start_ts=SUB_START, end_ts=HOLDOUT_START) -> dict`(`kind, version, product, future, params, config, window, random_runs, headline, criteria, per_year_A, info{sub_period{window, headline, checks_info_only}, cost_x2{fee_rate, slippage, A}}`), `data_record(raw: DataFrame) -> dict`(`first_ts, last_ts, n_bars, missing, sha256`; 2024 이전 원본 봉), `write_stage1(raw, *, out_path=STAGE1_PATH, a_lock_path=A_LOCK_PATH, random_runs=RANDOM_RUNS, workers=1, start_ts=DATA_START, sub_start_ts=SUB_START, run=run_stage1, a_fingerprint=current_fingerprint) -> dict`(기록에 `provenance{a_lock_sha256, a_fingerprint, aeth_sha256, code_sha256, data, started, completed, git_head, git_dirty}`를 더해 `out_path`에 씀).

단계 1은 A의 `compare()`를 ETH 설정으로 그대로 부르는 얇은 층이다(해석 2~4). 판정은 기록에 쓰는 JSON 값으로 계산해 잠금이 기록만 보고 다시 확인할 수 있게 한다. `write_stage1`은 1회 규칙과 출처를 맡는다: 기록 파일이 있으면, 작업 트리가 더러우면, A 지문이 고정값이 아니면, 봉이 2017~2023을 덮지 못하거나 2024에 닿으면 아무것도 계산하지 않고 거부한다. `run`과 `a_fingerprint`는 테스트용 주입점이다(CLI는 기본값만 쓴다). 합성 데이터에서 실제 `run_stage1`은 몇 초 걸린다(무작위 대조군 2회).

- [ ] **Step 1: 실패하는 테스트 작성**

판정 테스트는 항목마다의 탈락(낙폭은 정확히 15%까지 합격), 값이 없을 때의 탈락, 대조군이 없을 때의 탈락을 확인한다. 실행 테스트는 합성 2023년 봉으로 실제 `run_stage1`을 한 번 돌려 ETH 비용, 비교 대상, 판정이 결과와 일치함, 연도별 지표, 정보 실행(2019~2023 대신 합성 기간의 뒷부분, 비용 2배)을 확인한다. 기록 테스트는 가짜 실행으로 출처와 1회·거부 조건을 확인한다.

**파일 `tests/aeth/test_stage1.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import copy
import json

import pytest

from ewbot.aeth.config import FROZEN_A_FINGERPRINT, AethError
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.stage1 import STAGE1_KIND, data_record, headline, run_stage1, stage1_verdict, write_stage1
from ewbot.backtest.holdout import code_fingerprint
from ewbot.data.candles import fill_gaps
from ewbot.types import H1
from tests.aeth.helpers import HS, a_lock_sha, init_repo, pre2024_frame, write_a_lock
from tests.paper.helpers import PARAMS

HEAD = {
    "A": {"total_return": 0.2, "max_dd": 0.08, "sharpe": 0.9, "calmar": 0.4},
    "C": {"total_return": 0.05, "max_dd": 0.1, "sharpe": 0.2, "calmar": 0.1},
    "RANDOM": {"runs": 100, "sharpe": {"mean": -0.1}, "calmar": {"mean": 0.0}},
}


def _with(path: tuple[str, ...], value):
    head = copy.deepcopy(HEAD)
    node = head
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    return head


def test_the_verdict_passes_only_when_every_check_holds():
    assert stage1_verdict(HEAD) == {"checks": dict.fromkeys(
        ("total_return_gt_0", "max_dd_le_15pct", "sharpe_gt_c", "calmar_gt_c", "sharpe_gt_random_mean",
         "calmar_gt_random_mean"), True), "passed": True}
    assert stage1_verdict(_with(("A", "max_dd"), 0.15))["passed"] is True  # at most 15%


@pytest.mark.parametrize("path, value, check", [
    (("A", "total_return"), 0.0, "total_return_gt_0"),
    (("A", "max_dd"), 0.1501, "max_dd_le_15pct"),
    (("C", "sharpe"), 0.9, "sharpe_gt_c"),
    (("C", "calmar"), 0.5, "calmar_gt_c"),
    (("RANDOM", "sharpe", "mean"), 0.95, "sharpe_gt_random_mean"),
    (("RANDOM", "calmar", "mean"), 0.4, "calmar_gt_random_mean"),
    (("A", "sharpe"), None, "sharpe_gt_c"),  # a non-finite metric becomes null in JSON: never a pass
])
def test_each_failed_check_fails_stage_1(path, value, check):
    v = stage1_verdict(_with(path, value))
    assert v["checks"][check] is False and v["passed"] is False


def test_without_a_random_distribution_stage_1_fails():
    head = copy.deepcopy(HEAD)
    head["RANDOM"] = {"runs": 0, "n_signals": 0}
    v = stage1_verdict(head)
    assert not v["checks"]["sharpe_gt_random_mean"] and not v["checks"]["calmar_gt_random_mean"] and not v["passed"]


@pytest.fixture(scope="module")
def raw():
    return pre2024_frame()


@pytest.fixture(scope="module")
def rec(raw):
    start = int(raw["ts"].iloc[0])
    return run_stage1(fill_gaps(raw), PARAMS, random_runs=2, start_ts=start, sub_start_ts=start + 1500 * H1)


def test_run_stage1_compares_a_with_c_bh_ema_and_random_on_eth_costs(raw, rec):
    start = int(raw["ts"].iloc[0])
    assert rec["kind"] == STAGE1_KIND and rec["product"] == "ETH-USD" and rec["future"] == "ETP-20DEC30-CDE"
    assert rec["params"] == list(PARAMS) and rec["window"] == [start, HS] and rec["random_runs"] == 2
    assert rec["config"]["costs"]["contract_size"] == 0.1 and rec["config"]["breakers"]["enabled"] is True
    head = rec["headline"]
    assert sorted(head) == ["A", "BH", "C", "EMA", "HYBRID", "RANDOM"]
    assert head["A"]["n_trades"] == 7 and head["RANDOM"]["runs"] == 2 and head["RANDOM"]["n_signals"] == 7
    assert rec["criteria"] == stage1_verdict(head)
    assert rec["criteria"]["checks"]["total_return_gt_0"] and not rec["criteria"]["checks"]["sharpe_gt_c"]
    assert rec["per_year_A"]["2023"]["n_trades"] == 7
    json.dumps(rec, allow_nan=False)  # strict JSON as written


def test_the_information_runs_are_the_sub_period_and_double_costs(raw, rec):
    sub = rec["info"]["sub_period"]
    assert sub["window"] == [int(raw["ts"].iloc[0]) + 1500 * H1, HS]
    assert sub["checks_info_only"] == stage1_verdict(sub["headline"])
    cost = rec["info"]["cost_x2"]
    assert (cost["fee_rate"], cost["slippage"]) == (0.001, 0.001)
    assert cost["A"]["n_trades"] == rec["headline"]["A"]["n_trades"]
    assert cost["A"]["total_return"] < rec["headline"]["A"]["total_return"]


def test_headline_keeps_the_metrics_and_the_random_distribution():
    cmp = {"comparison": {"A": {k: 1.0 for k in ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate",
                                                     "avg_r")} | {"n_trades": 3, "sortino": 9.0}},
           "random": {"runs": 2, "n_signals": 3, "dist": {"sharpe": {"mean": 0.1}, "avg_r": {"mean": 0.2}}}}
    head = headline(cmp)
    assert "sortino" not in head["A"] and head["A"]["n_trades"] == 3
    assert head["RANDOM"] == {"runs": 2, "n_signals": 3, "sharpe": {"mean": 0.1}}


def test_stage_1_refuses_2024_bars(raw):
    with pytest.raises(AethError, match="before 2024-01-01"):
        run_stage1(fill_gaps(raw), PARAMS, end_ts=HS + H1)
    late = raw.copy()
    late.loc[len(late) - 1, "ts"] = HS
    with pytest.raises(AethError, match="before 2024-01-01"):
        run_stage1(late, PARAMS)


def test_data_record_hashes_the_cached_bars_before_2024(raw):
    rec = data_record(raw)
    assert rec["n_bars"] == 3000 and rec["first_ts"] == int(raw["ts"].iloc[0]) and rec["last_ts"] == HS - H1
    assert rec["missing"] == 0 and len(rec["sha256"]) == 64
    gap = raw.drop(index=[10, 11]).reset_index(drop=True)
    assert data_record(gap)["missing"] == 2 and data_record(gap)["sha256"] != rec["sha256"]
    edited = raw.copy()
    edited.loc[5, "close"] += 1.0
    assert data_record(edited)["sha256"] != rec["sha256"]


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, df, params, **kw):
        self.calls.append((df, params, kw))
        return {"kind": STAGE1_KIND, "params": list(params), "criteria": {"passed": False}}


@pytest.fixture
def world(tmp_path, raw):
    repo = init_repo(tmp_path / "repo")
    return {"repo": repo, "out": repo / "aeth-stage1.json", "a_lock": write_a_lock(tmp_path / "a"),
            "start": int(raw["ts"].iloc[0])}


def test_write_stage1_runs_once_on_gap_filled_bars_and_records_its_provenance(world, raw):
    run = FakeRun()
    rec = write_stage1(raw, out_path=world["out"], a_lock_path=world["a_lock"], start_ts=world["start"],
                       sub_start_ts=world["start"] + H1, random_runs=2, run=run)
    ((df, params, kw),) = run.calls
    assert params == PARAMS and df.equals(fill_gaps(raw)) and kw["random_runs"] == 2
    prov = rec["provenance"]
    assert prov["a_lock_sha256"] == a_lock_sha(world["a_lock"]) and prov["a_fingerprint"] == FROZEN_A_FINGERPRINT
    assert prov["aeth_sha256"] == aeth_fingerprint() and prov["code_sha256"] == code_fingerprint()
    assert prov["data"] == data_record(raw) and {"git_head", "git_dirty", "started", "completed"} <= set(prov)
    assert json.loads(world["out"].read_text(encoding="utf-8")) == rec
    with pytest.raises(AethError, match="runs once"):
        write_stage1(raw, out_path=world["out"], a_lock_path=world["a_lock"], start_ts=world["start"], run=run)
    assert len(run.calls) == 1


@pytest.mark.parametrize("case, why", [
    ("dirty", "uncommitted changes"), ("fingerprint", "not the frozen"), ("late_start", "to completion"),
    ("early_end", "to completion"), ("2024", "before 2024-01-01"), ("a_lock", "not strategy A's completed"),
])
def test_write_stage1_refuses_before_running(world, raw, case, why):
    kw = {"out_path": world["out"], "a_lock_path": world["a_lock"], "start_ts": world["start"]}
    bars = raw
    if case == "dirty":
        (world["repo"] / "README.md").write_text("edited\n", encoding="utf-8")
    if case == "fingerprint":
        kw["a_fingerprint"] = lambda: "0" * 64
    if case == "late_start":
        bars = raw.iloc[48:].reset_index(drop=True)
    if case == "early_end":
        bars = raw.iloc[:-48].reset_index(drop=True)
    if case == "2024":
        bars = raw.copy()
        bars.loc[len(bars) - 1, "ts"] = HS
    if case == "a_lock":
        kw["a_lock_path"] = world["repo"] / "missing.lock"
    run = FakeRun()
    with pytest.raises(AethError, match=why):
        write_stage1(bars, run=run, **kw)
    assert run.calls == [] and not world["out"].exists()
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/aeth/test_stage1.py -q
```

예상: `No module named 'ewbot.aeth.stage1'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/aeth/stage1.py`** (새 파일, 전체 내용)

```python
"""Stage 1 of A-ETH (Plan 5a spec 6): strategy A, frozen, on ETH-USD 2017-01-01..2023-12-31 - one attempt, no search.

run_stage1 runs strategy A's compare() - A, C, HYBRID, buy & hold, EMA 50/200 and the 100-run random-entry control
with A's trade geometry, all with the ETH costs - over [2017-01-01, 2024-01-01), and, for information only, the same
comparison over 2019-01-01..2023-12-31 and A alone with fees and slippage x 2. stage1_verdict is the pass rule:
A's net total return > 0, A's max drawdown <= 15%, and A's Sharpe and Calmar each above C's and above the random
control's mean. write_stage1 runs it ONCE and writes aeth-stage1.json; it refuses when that record exists, when
tracked files have uncommitted changes, when strategy A's fingerprint is not the frozen e0435223..., and when the
bars do not cover 2017..2023 or reach 2024. The record carries what it ran on - the params and the sha256 of
A's holdout.lock, the config, the bars (data_record), the fingerprints, the git state - so the lock can check it
(holdout.create_lock)."""

from __future__ import annotations

import dataclasses
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.aeth.config import (
    A_LOCK_PATH,
    COST_INFO_MULT,
    DATA_START,
    FROZEN_A_FINGERPRINT,
    FUTURE,
    PRODUCT,
    RANDOM_RUNS,
    STAGE1_PATH,
    SUB_START,
    AethError,
    Combo,
    aeth_config,
    config_json,
    frozen_a_params,
    scale_trading_costs,
)
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.lockfile import refuse_dirty, write_json
from ewbot.backtest.holdout import code_fingerprint, data_fingerprint, git_state
from ewbot.backtest.metrics import compute_metrics
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.runner import compare, run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.candles import check_integrity, fill_gaps
from ewbot.paper.fingerprint import current_fingerprint
from ewbot.types import H1

STAGE1_KIND = "ewbot-aeth-stage1"
MAX_DD = 0.15
COVER_SLACK_S = 86_400  # the bars must start within a day of the window start and end within a day of 2024-01-01
_STRATEGIES = ("A", "HYBRID", "C", "BH", "EMA")
_KEYS = ("total_return", "cagr", "max_dd", "sharpe", "calmar", "win_rate", "avg_r", "n_trades")


def headline(cmp: dict[str, Any]) -> dict[str, Any]:
    """The comparison's metrics per strategy and the random control's distribution, as strict JSON."""
    comp = cmp["comparison"]
    head: dict[str, Any] = {n: {k: comp[n][k] for k in _KEYS} for n in _STRATEGIES if n in comp}
    rnd = cmp.get("random") or {}
    dist = rnd.get("dist") or {}
    head["RANDOM"] = {"runs": int(rnd.get("runs", 0)), "n_signals": int(rnd.get("n_signals", 0)),
                      **{k: dist[k] for k in ("sharpe", "calmar", "total_return") if k in dist}}
    return to_jsonable(head)


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _gt(x: Any, y: Any) -> bool:
    return _num(x) and _num(y) and x > y


def stage1_verdict(head: dict[str, Any]) -> dict[str, Any]:
    """Spec 6: every check must hold. A missing or non-finite number fails its check."""
    a, c = head["A"], head["C"]
    rnd = head.get("RANDOM") or {}
    r_sharpe = (rnd.get("sharpe") or {}).get("mean")
    r_calmar = (rnd.get("calmar") or {}).get("mean")
    checks = {
        "total_return_gt_0": _gt(a["total_return"], 0.0),
        "max_dd_le_15pct": _num(a["max_dd"]) and a["max_dd"] <= MAX_DD,
        "sharpe_gt_c": _gt(a["sharpe"], c["sharpe"]),
        "calmar_gt_c": _gt(a["calmar"], c["calmar"]),
        "sharpe_gt_random_mean": _gt(a["sharpe"], r_sharpe),
        "calmar_gt_random_mean": _gt(a["calmar"], r_calmar),
    }
    return {"checks": checks, "passed": all(checks.values())}


def run_stage1(
    df1h: pd.DataFrame,
    params: Combo,
    *,
    random_runs: int = RANDOM_RUNS,
    workers: int = 1,
    start_ts: int = DATA_START,
    sub_start_ts: int = SUB_START,
    end_ts: int = HOLDOUT_START,
) -> dict[str, Any]:
    """The stage-1 comparisons on gap-filled bars that all end before 2024-01-01."""
    if end_ts > HOLDOUT_START or (len(df1h) and int(df1h["ts"].max()) >= HOLDOUT_START):
        raise AethError("stage 1 reads ETH bars before 2024-01-01 only")
    cfg = aeth_config(params)
    full = compare(cfg, df1h, start_ts, end_ts, random_runs=random_runs, workers=workers)
    sub = compare(cfg, df1h, sub_start_ts, end_ts, random_runs=random_runs, workers=workers)
    dear = dataclasses.replace(cfg, costs=scale_trading_costs(cfg.costs, COST_INFO_MULT))
    res = run_backtest(dear, df1h, start_ts, end_ts)
    dear_m = compute_metrics(res.equity, res.trades, start_ts, end_ts)
    head, sub_head = headline(full), headline(sub)
    return {
        "kind": STAGE1_KIND,
        "version": 1,
        "product": PRODUCT,
        "future": FUTURE,
        "params": [float(p) for p in params],
        "config": config_json(params),
        "window": [int(start_ts), int(end_ts)],
        "random_runs": int(random_runs),
        "headline": head,
        "criteria": stage1_verdict(head),
        "per_year_A": to_jsonable(full["breakdown"]["A"]["per_year"]),
        "info": {
            "sub_period": {"window": [int(sub_start_ts), int(end_ts)], "headline": sub_head,
                           "checks_info_only": stage1_verdict(sub_head)},
            "cost_x2": {"fee_rate": dear.costs.fee_rate, "slippage": dear.costs.slippage,
                        "A": to_jsonable(dataclasses.asdict(dear_m))},
        },
    }


def data_record(raw: pd.DataFrame) -> dict[str, Any]:
    """The cached (not gap-filled) ETH bars before 2024-01-01: first and last ts, count, missing hours, sha256
    (A's data_fingerprint)."""
    w = raw[raw["ts"] < HOLDOUT_START].reset_index(drop=True)
    return {
        "first_ts": int(w["ts"].iloc[0]) if len(w) else None,
        "last_ts": int(w["ts"].iloc[-1]) if len(w) else None,
        "n_bars": int(len(w)),
        "missing": len(check_integrity(w).missing_ts),
        "sha256": data_fingerprint(w, 0, HOLDOUT_START - 1),
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_stage1(
    raw: pd.DataFrame,
    *,
    out_path: Path = STAGE1_PATH,
    a_lock_path: Path = A_LOCK_PATH,
    random_runs: int = RANDOM_RUNS,
    workers: int = 1,
    start_ts: int = DATA_START,
    sub_start_ts: int = SUB_START,
    run: Callable[..., dict[str, Any]] = run_stage1,
    a_fingerprint: Callable[[], str] = current_fingerprint,
) -> dict[str, Any]:
    """Stage 1, once: refuse, or run on the cached bars `raw` (before 2024) and write the record to out_path."""
    out = Path(out_path)
    if out.exists():
        raise AethError(f"{out} exists: stage 1 runs once (spec 6) - no rerun, whatever its result")
    a_fp = a_fingerprint()
    if a_fp != FROZEN_A_FINGERPRINT:
        raise AethError(f"strategy A's fingerprint is {a_fp[:12]}, not the frozen {FROZEN_A_FINGERPRINT[:12]}: "
                        "refusing")
    refuse_dirty(out)
    params, a_sha = frozen_a_params(a_lock_path)
    if raw.empty or int(raw["ts"].max()) >= HOLDOUT_START:
        raise AethError("stage 1 needs the cached ETH bars before 2024-01-01 (and none after)")
    first, last = int(raw["ts"].min()), int(raw["ts"].max())
    if first > start_ts + COVER_SLACK_S or last < HOLDOUT_START - H1 - COVER_SLACK_S:
        raise AethError(f"the ETH bars span {first}..{last}, not {start_ts}..{HOLDOUT_START - H1}: "
                        "run `ewbot aeth fetch` to completion first")
    started = _now()
    rec = run(fill_gaps(raw), params, random_runs=random_runs, workers=workers, start_ts=start_ts,
              sub_start_ts=sub_start_ts)
    rec["provenance"] = {
        "a_lock_sha256": a_sha,
        "a_fingerprint": a_fp,
        "aeth_sha256": aeth_fingerprint(),
        "code_sha256": code_fingerprint(),
        "data": data_record(raw),
        "started": started,
        "completed": _now(),
        **git_state(),
    }
    write_json(out, rec)
    return rec
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/aeth/test_stage1.py -q
```

예상: `21 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/aeth/stage1.py tests/aeth/test_stage1.py
git commit -m "feat(aeth): stage 1 - A's compare on ETH 2017-2023, the pre-registered pass rule, information runs and the one-shot record" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: 잠금 작성, 1회 ETH 홀드아웃, 단계 2 판정

**Files:**
- Create: `src/ewbot/aeth/holdout.py`
- Test: `tests/aeth/records.py`
- Test: `tests/aeth/test_holdout.py`
- Modify: `tests/paper/test_static.py` (`ALLOWED_TRUE`에 `aeth/holdout.py`)

**Interfaces:**
- Consumes: Task 1~3 전부(특히 `frozen_a_params`, `config_json`, `aeth_config`, `aeth_fingerprint`, `lock_digest`, `read_lock`, `committed_text`, `refuse_dirty`, `write_json`, `LOCK_KIND`, `LOCK_VERSION`, `cache_path`, `load_eth(..., unlock_2024=True, ...)`, `STAGE1_KIND`, `MAX_DD`, `data_record`, `headline`, `stage1_verdict`). A: `ewbot.backtest.holdout.evaluate_criteria(cmp) -> dict`(`winners`, `max_dd_ok`, `n_trades_ok`, `n_trades`, `trade_count`, `random_control_evaluated`, `passing`, `paper_4_weeks`, `automatic_criteria_met`), `code_fingerprint()`, `data_fingerprint`, `git_state()`; `compare(..., allow_holdout=True)`; `fill_gaps`; `current_fingerprint()`.
- Produces: `ewbot.aeth.holdout`: `stage2_verdict(criteria: Mapping, comparison: Mapping) -> dict`(`c1_a_beats_c_bh_random_mean`, `c2_max_dd_le_15pct`, `n_trades`, `passed`), `lock_mismatch(lock: Mapping, a_lock_path: Path = A_LOCK_PATH) -> list[str]`, `create_lock(raw_pre2024, *, now_ts: int, lock_path=LOCK_PATH, stage1_path=STAGE1_PATH, a_lock_path=A_LOCK_PATH, random_runs=RANDOM_RUNS) -> dict`, `run_holdout_aeth(*, confirm: bool, cache_dir: Path, lock_path=LOCK_PATH, a_lock_path=A_LOCK_PATH, workers=1, a_fingerprint=current_fingerprint, aeth_fp=aeth_fingerprint, code_fp=code_fingerprint) -> dict`(`{"lock_path", "compare", "first_run"}`; `first_run`: `started, completed, start_ts, end_ts, last_bar_ts, data{start_ts, last_bar_ts, n_bars, sha256}, headline, criteria, verdict`).
  `tests/aeth/records.py`: `PASSING_HEADLINE`, `stage1_record(raw_pre2024, a_lock, *, headline=None, **provenance) -> dict`, `aeth_lock(df, a_lock, *, passed=True, n_trades=HOLDOUT_TRADES, last_bar=BOUNDARY, **overrides) -> dict`(Task 5·6이 쓰는, 홀드아웃이 끝난 잠금).

잠금은 커밋된 단계 1 기록에서만, 2024 이후 봉을 보기 전에 만든다(해석 5·6). 홀드아웃은 해석 7의 순서로 모든 조건을 확인한 뒤 시도를 잠금에 먼저 기록하고 A의 `compare`를 `allow_holdout=True`로 1회 돌리며, 판정은 A의 `evaluate_criteria`를 바꾸지 않고 쓴다(해석 8). 리터럴 `allow_holdout=True`를 쓰는 새 모듈이므로 A의 정적 검사(`tests/paper/test_static.py`의 `ALLOWED_TRUE`, 등호 비교)에 `aeth/holdout.py`를 더한다. `a_fingerprint`·`aeth_fp`·`code_fp`는 테스트용 주입점이다. 테스트 기록 도우미 `records.py`는 `write_stage1`이 쓰는 모양의 단계 1 기록(손으로 정한 결과)과 홀드아웃이 끝난 잠금(Task 5·6용)을 만든다.

- [ ] **Step 1: 실패하는 테스트 작성**

판정 테스트(HYBRID만 이기면 탈락, 낙폭 초과 탈락), 잠금이 묶는 필드, 잠금 거부(이유별: 미커밋, 커밋 뒤 수정, 탈락, 손으로 바꾼 합격, 출처의 파라미터·A-ETH 지문·데이터·A 지문 불일치, 더러운 트리, 2024 전 시각, 2024 봉), 합성 데이터의 실제 1회 실행(A의 `evaluate_criteria` 그대로, 시도 기록), 마지막 봉 누락, 재실행 거부, 실행 거부(이유별 11가지; 거부되면 잠금 파일은 그대로)를 확인한다. `ALLOWED_TRUE`를 먼저 고치므로 `test_static.py`의 해당 테스트도 이 단계에서 실패한다.

**파일 `tests/aeth/records.py`** (새 파일, 전체 내용)

```python
"""A-ETH records for tests: a stage-1 record as write_stage1 writes it (the fields create_lock checks) and a
completed holdout-aeth.lock as run_holdout_aeth leaves it (the fields the paper runtime reads), both for the
synthetic bars of tests.aeth.helpers and the paper tests' params."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from ewbot.aeth.config import FUTURE, PRODUCT, config_json
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.lockfile import LOCK_KIND, LOCK_VERSION, lock_digest
from ewbot.aeth.stage1 import STAGE1_KIND, data_record, stage1_verdict
from ewbot.backtest.holdout import data_fingerprint
from ewbot.data.candles import fill_gaps
from ewbot.paper.fingerprint import current_fingerprint
from ewbot.types import H1
from tests.aeth.helpers import HOLDOUT_TRADES, HS, a_lock_sha
from tests.paper.helpers import BOUNDARY, PARAMS

PASSING_HEADLINE = {
    "A": {"total_return": 0.2, "cagr": 0.03, "max_dd": 0.08, "sharpe": 0.9, "calmar": 0.4, "win_rate": 0.4,
          "avg_r": 0.3, "n_trades": 70},
    "C": {"total_return": 0.05, "cagr": 0.01, "max_dd": 0.1, "sharpe": 0.2, "calmar": 0.1, "win_rate": 0.4,
          "avg_r": 0.1, "n_trades": 80},
    "RANDOM": {"runs": 100, "n_signals": 70, "sharpe": {"mean": -0.1, "p05": -1.0, "p50": -0.1, "p95": 0.6},
               "calmar": {"mean": 0.0, "p05": -0.3, "p50": 0.0, "p95": 0.5}},
}


def stage1_record(raw_pre2024: pd.DataFrame, a_lock: Path, *, headline: dict[str, Any] | None = None,
                  **provenance: Any) -> dict[str, Any]:
    """A stage-1 record with a hand-set headline (default: one that passes, criteria computed from it) and
    provenance computed from the bars and this code (each field overridable)."""
    head = json.loads(json.dumps(PASSING_HEADLINE if headline is None else headline))
    prov = {"a_lock_sha256": a_lock_sha(a_lock), "a_fingerprint": current_fingerprint(),
            "aeth_sha256": aeth_fingerprint(), "code_sha256": "c0de" * 16, "data": data_record(raw_pre2024)}
    prov.update(provenance)
    return {"kind": STAGE1_KIND, "version": 1, "product": PRODUCT, "future": FUTURE, "params": list(PARAMS),
            "config": config_json(PARAMS), "headline": head, "criteria": stage1_verdict(head), "provenance": prov}


def aeth_lock(df: pd.DataFrame, a_lock: Path, *, passed: bool = True, n_trades: int = HOLDOUT_TRADES,
              last_bar: int = BOUNDARY, **overrides: Any) -> dict[str, Any]:
    """A completed A-ETH lock for the bars `df` (crossing 2024): first_run says the holdout ran with `n_trades` A
    trades and passed (or not). sha256 is the real digest of the bound fields after `overrides`."""
    filled = fill_gaps(df)
    filled = filled[filled["ts"] <= last_bar].reset_index(drop=True)
    lock: dict[str, Any] = {
        "kind": LOCK_KIND, "version": LOCK_VERSION, "product": PRODUCT, "future": FUTURE, "params": list(PARAMS),
        "a_lock_sha256": a_lock_sha(a_lock), "config": config_json(PARAMS), "holdout_start": HS,
        "last_bar_ts": last_bar, "random_runs": 2, "a_fingerprint": current_fingerprint(),
        "aeth_sha256": aeth_fingerprint(), "code_sha256": "c0de" * 16, "pre2024": data_record(df),
        "stage1_sha256": "5" * 64,
    }
    lock.update(overrides)
    lock.update(sha256=lock_digest(lock), git_head="f" * 40, git_dirty=False, attempts=[], first_run={
        "start_ts": HS, "end_ts": last_bar + H1, "last_bar_ts": last_bar,
        "data": {"start_ts": HS, "last_bar_ts": last_bar, "sha256": data_fingerprint(filled, HS, last_bar)},
        "verdict": {"c1_a_beats_c_bh_random_mean": passed, "c2_max_dd_le_15pct": True, "n_trades": n_trades,
                    "passed": passed},
    })
    return lock
```

**파일 `tests/aeth/test_holdout.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import hashlib
import json

import pytest

from ewbot.aeth.config import FROZEN_A_FINGERPRINT, AethError, config_json
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.holdout import create_lock, lock_mismatch, run_holdout_aeth, stage2_verdict
from ewbot.aeth.lockfile import LOCK_KIND, lock_digest, lock_exists
from ewbot.aeth.stage1 import data_record, headline
from ewbot.backtest.holdout import code_fingerprint, data_fingerprint, evaluate_criteria
from ewbot.backtest.report import to_jsonable
from ewbot.data.candles import fill_gaps
from ewbot.types import H1
from tests.aeth.helpers import (
    HOLDOUT_TRADES,
    HS,
    LOCK_NOW,
    a_lock_sha,
    commit_file,
    eth_frame,
    init_repo,
    save_cache,
    write_a_lock,
)
from tests.aeth.records import stage1_record
from tests.paper.helpers import BOUNDARY, PARAMS, git


def test_stage2_passes_when_a_wins_a_s_criteria_and_keeps_its_drawdown():
    comp = {"A": {"max_dd": 0.05, "n_trades": 30}}
    assert stage2_verdict({"winners": ["A", "HYBRID"]}, comp) == {
        "c1_a_beats_c_bh_random_mean": True, "c2_max_dd_le_15pct": True, "n_trades": 30, "passed": True}
    assert stage2_verdict({"winners": ["HYBRID"]}, comp)["passed"] is False  # HYBRID alone is not A on ETH
    assert stage2_verdict({"winners": ["A"]}, {"A": {"max_dd": 0.16, "n_trades": 30}})["passed"] is False


def _world(tmp_path, *, stage1=None, commit_stage1=True):
    df = eth_frame()
    a_lock = write_a_lock(tmp_path / "a")
    repo = init_repo(tmp_path / "repo")
    cache = tmp_path / "cache"
    save_cache(cache, df)
    raw = df[df["ts"] < HS].reset_index(drop=True)
    rec = stage1_record(raw, a_lock) if stage1 is None else stage1
    text = json.dumps(rec, indent=2) + "\n"
    s1 = commit_file(repo, "aeth-stage1.json", text) if commit_stage1 else repo / "aeth-stage1.json"
    if not commit_stage1:
        s1.write_text(text, encoding="utf-8")
    return {"df": df, "raw": raw, "a_lock": a_lock, "repo": repo, "cache": cache, "s1": s1, "text": text,
            "lock": repo / "holdout-aeth.lock"}


def _lock(w, **kw):
    return create_lock(w["raw"], now_ts=kw.pop("now_ts", LOCK_NOW), lock_path=w["lock"], stage1_path=w["s1"],
                       a_lock_path=w["a_lock"], random_runs=2, **kw)


def test_the_lock_binds_the_passed_stage_1_record_and_what_it_ran_on(tmp_path):
    w = _world(tmp_path)
    lock = _lock(w)
    assert lock_exists(w["lock"]) and lock == json.loads(w["lock"].read_text(encoding="utf-8"))
    assert (lock["kind"], lock["product"], lock["future"]) == (LOCK_KIND, "ETH-USD", "ETP-20DEC30-CDE")
    assert lock["params"] == list(PARAMS) and lock["a_lock_sha256"] == a_lock_sha(w["a_lock"])
    assert lock["config"] == config_json(PARAMS) and lock["holdout_start"] == HS
    assert lock["last_bar_ts"] == BOUNDARY and lock["random_runs"] == 2
    assert lock["a_fingerprint"] == FROZEN_A_FINGERPRINT and lock["aeth_sha256"] == aeth_fingerprint()
    assert lock["code_sha256"] == code_fingerprint() and lock["pre2024"] == data_record(w["raw"])
    assert lock["stage1_sha256"] == hashlib.sha256(w["text"].encode("utf-8")).hexdigest()
    assert lock["sha256"] == lock_digest(lock) and lock["first_run"] is None and lock["attempts"] == []
    assert lock_mismatch(lock, w["a_lock"]) == []
    with pytest.raises(AethError, match="written once"):
        _lock(w)


@pytest.mark.parametrize("case, why", [
    ("uncommitted", "committed unchanged"), ("edited", "committed unchanged"), ("failed", "did not pass"),
    ("hand_passed", "did not pass"), ("a_lock", r"other \['params'\]"), ("aeth", r"other \['aeth_sha256'\]"),
    ("data", r"other \['data'\]"), ("a_fingerprint", r"other \['a_fingerprint'\]"), ("dirty", "uncommitted changes"),
    ("before_2024", "after 2024-01-01"), ("bars_2024", "before 2024-01-01 only"),
])
def test_the_lock_is_refused(tmp_path, case, why):
    w = _world(tmp_path, commit_stage1=case != "uncommitted")
    raw = w["df"][w["df"]["ts"] < HS].reset_index(drop=True)
    kw = {}
    if case == "edited":
        w["s1"].write_text(w["text"].replace('"version": 1', '"version": 2'), encoding="utf-8")
    if case in ("failed", "hand_passed", "a_lock", "aeth", "data", "a_fingerprint"):
        head = {**json.loads(w["text"])["headline"]}
        if case in ("failed", "hand_passed"):
            head["C"] = {**head["C"], "sharpe": 5.0}
        prov = {"a_lock": {"a_lock_sha256": "0" * 64}, "aeth": {"aeth_sha256": "0" * 64},
                "data": {"data": data_record(raw.iloc[:-1])}, "a_fingerprint": {"a_fingerprint": "0" * 64}}
        rec = stage1_record(raw, w["a_lock"], headline=head, **prov.get(case, {}))
        if case == "hand_passed":
            rec["criteria"]["passed"] = True
        w = _world(tmp_path / "again", stage1=rec)
    if case == "dirty":
        (w["repo"] / "README.md").write_text("edited\n", encoding="utf-8")
    if case == "before_2024":
        kw["now_ts"] = HS + 600
    if case == "bars_2024":
        w["raw"] = w["df"]
    with pytest.raises(AethError, match=why):
        _lock(w, **kw)
    assert not w["lock"].exists()


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    """One real (synthetic) ETH holdout run: the lock committed, then run_holdout_aeth once."""
    w = _world(tmp_path_factory.mktemp("holdout"))
    _lock(w)
    git(w["repo"], "add", w["lock"].name)
    git(w["repo"], "commit", "-q", "-m", "lock")
    out = run_holdout_aeth(confirm=True, cache_dir=w["cache"], lock_path=w["lock"], a_lock_path=w["a_lock"])
    return w, out


def test_the_holdout_runs_a_s_comparison_and_criteria_on_the_window(ran):
    w, out = ran
    fr = out["first_run"]
    assert (fr["start_ts"], fr["end_ts"], fr["last_bar_ts"]) == (HS, BOUNDARY + H1, BOUNDARY)
    assert fr["data"]["last_bar_ts"] == BOUNDARY and fr["data"]["n_bars"] == (BOUNDARY - HS) // H1 + 1
    assert fr["headline"] == headline(out["compare"])
    assert fr["criteria"] == to_jsonable(evaluate_criteria(out["compare"]))  # strategy A's, unchanged
    assert fr["verdict"] == stage2_verdict(fr["criteria"], out["compare"]["comparison"])
    assert fr["verdict"]["n_trades"] == HOLDOUT_TRADES == fr["headline"]["A"]["n_trades"]
    assert out["compare"]["random"]["runs"] == 2
    lock = json.loads(w["lock"].read_text(encoding="utf-8"))
    assert lock["first_run"] == fr and lock_exists(w["lock"])
    assert len(lock["attempts"]) == 1 and lock["attempts"][0]["data_sha256"] == fr["data"]["sha256"]


def test_a_missing_last_bar_is_filled_from_the_later_bars_like_the_paper_runtime_does(tmp_path):
    w = _world(tmp_path)
    gap = w["df"][w["df"]["ts"] != BOUNDARY].reset_index(drop=True)
    save_cache(w["cache"], gap)
    _lock(w)
    git(w["repo"], "add", w["lock"].name)
    git(w["repo"], "commit", "-q", "-m", "lock")
    fr = run_holdout_aeth(confirm=True, cache_dir=w["cache"], lock_path=w["lock"], a_lock_path=w["a_lock"])["first_run"]
    assert fr["data"]["sha256"] == data_fingerprint(fill_gaps(gap), HS, BOUNDARY)
    assert fr["data"]["n_bars"] == (BOUNDARY - HS) // H1 + 1


def test_the_holdout_runs_once(ran):
    w, _ = ran
    with pytest.raises(AethError, match="already run once"):
        run_holdout_aeth(confirm=True, cache_dir=w["cache"], lock_path=w["lock"], a_lock_path=w["a_lock"])


@pytest.mark.parametrize("case, why", [
    ("confirm", "single-use"), ("uncommitted", "differs from the committed lock"),
    ("tampered", "not a valid A-ETH lock"),
    ("a_fp", "strategy A fingerprint"), ("aeth_fp", "A-ETH fingerprint"), ("code_fp", "code fingerprint"),
    ("a_params", "a_lock_sha256"), ("dirty", "uncommitted changes"), ("pre2024", "differ from the ones stage 1"),
    ("short", "ends before the lock's last bar"), ("attempt", "unfinished attempt"),
])
def test_the_holdout_is_refused(tmp_path, case, why):
    w = _world(tmp_path)
    _lock(w)
    if case != "uncommitted":
        git(w["repo"], "add", w["lock"].name)
        git(w["repo"], "commit", "-q", "-m", "lock")
    kw = {"confirm": case != "confirm", "cache_dir": w["cache"], "lock_path": w["lock"], "a_lock_path": w["a_lock"]}
    if case == "tampered":
        lock = json.loads(w["lock"].read_text(encoding="utf-8"))
        lock["random_runs"] = 1
        w["lock"].write_text(json.dumps(lock), encoding="utf-8")
    if case in ("a_fp", "aeth_fp", "code_fp"):
        kw[{"a_fp": "a_fingerprint", "aeth_fp": "aeth_fp", "code_fp": "code_fp"}[case]] = lambda: "0" * 64
    if case == "a_params":
        kw["a_lock_path"] = write_a_lock(tmp_path / "other", random_runs=3)  # same params, another A lock sha256
    if case == "dirty":
        (w["repo"] / "README.md").write_text("edited\n", encoding="utf-8")
    if case == "pre2024":
        df = w["df"].copy()
        df.loc[5, "close"] += 1.0
        save_cache(w["cache"], df)
    if case == "short":
        save_cache(w["cache"], w["df"][w["df"]["ts"] < BOUNDARY])
    if case == "attempt":
        lock = json.loads(w["lock"].read_text(encoding="utf-8"))
        lock["attempts"] = [{"started": "earlier", "data_sha256": "0" * 64}]
        w["lock"].write_text(json.dumps(lock), encoding="utf-8")
    before = w["lock"].read_text(encoding="utf-8")
    with pytest.raises(AethError, match=why):
        run_holdout_aeth(**kw)
    assert w["lock"].read_text(encoding="utf-8") == before  # nothing recorded
```

**수정 `tests/paper/test_static.py`** (1/1) — 다음 부분을 찾아:

```python
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py", "live/target.py"}  # Plan 2 spec 7.1, Plan 3 spec 3.1
```

바꿀 내용:

```python
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py", "live/target.py",  # Plan 2 spec 7.1, Plan 3 spec 3.1
                "aeth/holdout.py"}  # Plan 5a spec 7.2: the one-shot ETH holdout
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/aeth/test_holdout.py tests/paper/test_static.py -q
```

예상: `No module named 'ewbot.aeth.holdout'`
예상: `1 error`

`tests/paper/test_static.py`만 따로 돌리면 `test_only_the_holdout_and_the_paper_runner_pass_allow_holdout_true` 1개가 실패한다(`aeth/holdout.py`가 아직 없다).

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/aeth/holdout.py`** (새 파일, 전체 내용)

```python
"""Stage 2 of A-ETH (Plan 5a spec 7): holdout-aeth.lock and the one-shot ETH holdout.

create_lock writes <project root>/holdout-aeth.lock BEFORE any ETH bar from 2024-01-01 on can be fetched or read
(data.py unlocks them only for a valid lock), from the committed stage-1 record (aeth-stage1.json): only a record
that passed (its criteria are stage1_verdict of its own headline) and was made with the params and sha256 of
strategy A's holdout.lock, this A-ETH code (aeth_sha256), strategy A's frozen fingerprint and the same pre-2024 ETH
bars. The lock fixes the product and contract, the params and A's lock sha256, the full config (ETH costs), the
holdout start and its last bar (the last complete hour at lock time), the 100-run random control, A's
fingerprint, the A-ETH fingerprint, the code state like A's lock (code_sha256: A's code_fingerprint over every
src/ewbot *.py), the pre-2024 bars (data_record) and the stage-1 record's sha256, and their digest. It refuses a
dirty tracked tree. The user commits it at once.

run_holdout_aeth runs the holdout ONCE, after the user's approval (confirm=True), over [2024-01-01, last_bar_ts]
from the first cached bar, like strategy A's holdout. It refuses unless the lock is valid and equals the committed
one, first_run is empty there and in the file, strategy A's fingerprint, the A-ETH fingerprint and the code
fingerprint are the lock's, the lock's fields are what this code writes for A's params now (lock_mismatch), no
tracked file but the lock has uncommitted changes, the pre-2024 bars are the lock's, the data reaches last_bar_ts
and an unfinished earlier attempt saw the same holdout bars. Each attempt is recorded in the lock before it runs.

Verdict (spec 7.3): strategy A's evaluate_criteria, unchanged, on the holdout's compare(). A-ETH passes when "A" is
one of its winners (A's Sharpe and Calmar above C's, buy & hold's and the random control's mean; the random control
evaluated) and A's max drawdown is at most 15% - strategy A's go-live items 1 and 2. The trade count is recorded."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from ewbot.aeth.config import (
    A_LOCK_PATH,
    FROZEN_A_FINGERPRINT,
    FUTURE,
    LOCK_PATH,
    PRODUCT,
    RANDOM_RUNS,
    STAGE1_PATH,
    AethError,
    aeth_config,
    config_json,
    frozen_a_params,
)
from ewbot.aeth.data import cache_path, load_eth
from ewbot.aeth.fingerprint import aeth_fingerprint
from ewbot.aeth.lockfile import (
    LOCK_KIND,
    LOCK_VERSION,
    committed_text,
    lock_digest,
    read_lock,
    refuse_dirty,
    write_json,
)
from ewbot.aeth.stage1 import MAX_DD, STAGE1_KIND, data_record, headline, stage1_verdict
from ewbot.backtest.holdout import code_fingerprint, data_fingerprint, evaluate_criteria, git_state
from ewbot.backtest.report import to_jsonable
from ewbot.backtest.runner import compare
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.candles import fill_gaps
from ewbot.paper.fingerprint import current_fingerprint
from ewbot.types import H1


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def stage2_verdict(criteria: Mapping[str, Any], comparison: Mapping[str, Any]) -> dict[str, Any]:
    """Spec 7.3 from strategy A's evaluate_criteria output and the comparison it was made from."""
    c1 = "A" in criteria["winners"]
    c2 = float(comparison["A"]["max_dd"]) <= MAX_DD
    return {"c1_a_beats_c_bh_random_mean": bool(c1), "c2_max_dd_le_15pct": bool(c2),
            "n_trades": int(comparison["A"]["n_trades"]), "passed": bool(c1 and c2)}


def lock_mismatch(lock: Mapping[str, Any], a_lock_path: Path = A_LOCK_PATH) -> list[str]:
    """The lock fields that are not what this code writes for strategy A's params now (AethError when A's
    holdout.lock itself is not valid)."""
    params, a_sha = frozen_a_params(a_lock_path)
    want = {"kind": LOCK_KIND, "version": LOCK_VERSION, "product": PRODUCT, "future": FUTURE,
            "params": list(params), "a_lock_sha256": a_sha, "config": config_json(params),
            "holdout_start": HOLDOUT_START}
    return [k for k, v in want.items() if lock.get(k) != v]


def create_lock(
    raw_pre2024: pd.DataFrame,
    *,
    now_ts: int,
    lock_path: Path = LOCK_PATH,
    stage1_path: Path = STAGE1_PATH,
    a_lock_path: Path = A_LOCK_PATH,
    random_runs: int = RANDOM_RUNS,
) -> dict[str, Any]:
    """Write the lock (once) from the committed, passed stage-1 record. raw_pre2024: the cached ETH bars, all
    before 2024-01-01 (the record must have been made on them)."""
    path, s1 = Path(lock_path), Path(stage1_path)
    if path.exists():
        raise AethError(f"{path} exists: the A-ETH lock is written once")
    text = committed_text(s1)
    if text is None or not s1.is_file() or s1.read_text(encoding="utf-8") != text:
        raise AethError(f"{s1} must be committed unchanged before the lock (it is the permanent stage-1 record)")
    try:
        rec = json.loads(text)
        head, crit, prov = rec["headline"], rec["criteria"], rec["provenance"]
        ok = rec.get("kind") == STAGE1_KIND and crit == stage1_verdict(head) and bool(crit["passed"])
    except (ValueError, KeyError, TypeError) as exc:
        raise AethError(f"{s1} is not an A-ETH stage-1 record: {exc!r}") from exc
    if not ok:
        raise AethError(f"{s1}: stage 1 did not pass (spec 6): A-ETH ends without a holdout")
    if len(raw_pre2024) and int(raw_pre2024["ts"].max()) >= HOLDOUT_START:
        raise AethError("the lock is made from the ETH bars before 2024-01-01 only")
    params, a_sha = frozen_a_params(a_lock_path)
    a_fp, aeth_sha = current_fingerprint(), aeth_fingerprint()
    pre = data_record(raw_pre2024)
    stale = [name for name, same in (
        ("params", rec.get("params") == list(params) and prov.get("a_lock_sha256") == a_sha),
        ("config", rec.get("config") == config_json(params)),
        ("a_fingerprint", a_fp == FROZEN_A_FINGERPRINT and prov.get("a_fingerprint") == a_fp),
        ("aeth_sha256", prov.get("aeth_sha256") == aeth_sha),
        ("data", prov.get("data") == pre),
    ) if not same]
    if stale:
        raise AethError(f"{s1} was made with other {stale} than now: the lock must bind what stage 1 ran on")
    last_bar = int(now_ts) // H1 * H1 - H1  # the last complete hour
    if last_bar < HOLDOUT_START:
        raise AethError("the lock must be written after 2024-01-01")
    if int(random_runs) < 1:
        raise AethError("random_runs must be >= 1 (strategy A's criteria compare with the random control)")
    refuse_dirty(path, path.name)
    lock: dict[str, Any] = {
        "kind": LOCK_KIND,
        "version": LOCK_VERSION,
        "product": PRODUCT,
        "future": FUTURE,
        "params": list(params),
        "a_lock_sha256": a_sha,
        "config": config_json(params),
        "holdout_start": HOLDOUT_START,
        "last_bar_ts": last_bar,
        "random_runs": int(random_runs),
        "a_fingerprint": a_fp,
        "aeth_sha256": aeth_sha,
        "code_sha256": code_fingerprint(),
        "pre2024": pre,
        "stage1_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    lock.update(sha256=lock_digest(lock), locked_at=_now(), **git_state(), attempts=[], first_run=None)
    write_json(path, lock)
    return lock


def run_holdout_aeth(
    *,
    confirm: bool,
    cache_dir: Path,
    lock_path: Path = LOCK_PATH,
    a_lock_path: Path = A_LOCK_PATH,
    workers: int = 1,
    a_fingerprint: Callable[[], str] = current_fingerprint,
    aeth_fp: Callable[[], str] = aeth_fingerprint,
    code_fp: Callable[[], str] = code_fingerprint,
) -> dict[str, Any]:
    """The one-shot ETH holdout over [2024-01-01, last_bar_ts]; the result goes to the lock's first_run."""
    if not confirm:
        raise AethError("the ETH holdout is single-use: pass confirm=True (CLI: --confirm) after the user's approval")
    path = Path(lock_path)
    lock = read_lock(path)
    text = committed_text(path)
    head = json.loads(text) if text else None
    if not isinstance(head, dict) or head.get("sha256") != lock["sha256"]:
        raise AethError(f"{path} differs from the committed lock: commit the lock as written by `aeth holdout lock`")
    if lock.get("first_run") is not None or head.get("first_run") is not None:
        raise AethError("the ETH holdout has already run once: no rerun (spec 7.2)")
    changed = [name for name, now, locked in (
        ("strategy A fingerprint", a_fingerprint(), lock["a_fingerprint"]),
        ("A-ETH fingerprint", aeth_fp(), lock["aeth_sha256"]),
        ("code fingerprint", code_fp(), lock["code_sha256"]),
    ) if now != locked]
    if changed:
        raise AethError(f"the {changed} changed after the lock: refusing")
    stale = lock_mismatch(lock, a_lock_path)
    if stale:
        raise AethError(f"the lock's {stale} are not this code's for strategy A's params: refusing")
    refuse_dirty(path, path.name)
    raw = load_eth(cache_path(cache_dir), unlock_2024=True, lock_path=path)
    if data_record(raw) != lock["pre2024"]:
        raise AethError("the cached ETH bars before 2024-01-01 differ from the ones stage 1 and the lock used")
    last = int(lock["last_bar_ts"])
    if raw.empty or int(raw["ts"].iloc[-1]) < last:
        raise AethError(f"the ETH cache ends before the lock's last bar {last}: run `ewbot aeth fetch` first")
    filled = fill_gaps(raw)  # like the paper runtime: a missing last bar is filled from the bars after it
    df = filled[filled["ts"] <= last].reset_index(drop=True)
    data = {"start_ts": HOLDOUT_START, "last_bar_ts": last,
            "n_bars": int(((df["ts"] >= HOLDOUT_START) & (df["ts"] <= last)).sum()),
            "sha256": data_fingerprint(df, HOLDOUT_START, last)}
    if any(a.get("data_sha256") != data["sha256"] for a in lock["attempts"]):
        raise AethError("an earlier unfinished attempt saw other holdout bars: refusing (inspect by hand)")
    attempt: dict[str, Any] = {"started": _now(), "data_sha256": data["sha256"], **git_state()}
    lock["attempts"].append(attempt)
    write_json(path, lock)

    cfg = aeth_config(tuple(lock["params"]))
    end = last + H1
    cmp = compare(cfg, df, HOLDOUT_START, end, random_runs=int(lock["random_runs"]), workers=workers,
                  allow_holdout=True)
    criteria = evaluate_criteria(cmp)
    verdict = stage2_verdict(criteria, cmp["comparison"])
    attempt["completed"] = _now()
    lock["first_run"] = to_jsonable({
        "started": attempt["started"], "completed": attempt["completed"], "start_ts": HOLDOUT_START, "end_ts": end,
        "last_bar_ts": last, "data": data, "headline": headline(cmp), "criteria": criteria, "verdict": verdict,
    })
    write_json(path, lock)
    return {"lock_path": str(path), "compare": cmp, "first_run": lock["first_run"]}
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/aeth/test_holdout.py tests/paper/test_static.py -q
```

예상: `55 passed`

`tests/aeth/test_holdout.py` 27개와 `tests/paper/test_static.py` 28개다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/aeth/holdout.py tests/aeth/records.py tests/aeth/test_holdout.py tests/paper/test_static.py
git commit -m "feat(aeth): holdout-aeth.lock from the committed stage-1 record and the one-shot ETH holdout judged by A's evaluate_criteria" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: ETH 모의매매 런타임

**Files:**
- Create: `src/ewbot/aeth/paper.py`
- Test: `tests/aeth/test_paper.py`
- Modify: `tests/paper/test_static.py` (`ALLOWED_TRUE`에 `aeth/paper.py`)

**Interfaces:**
- Consumes: Task 1~4: `A_LOCK_PATH`, `LOCK_PATH`, `AethError`, `aeth_config`, `fetch_eth`, `load_eth`, `current_epoch_fingerprint`, `epoch_fingerprint`, `lock_mismatch`, `committed_text`, `read_lock`; 도우미 `FakeCacheFetch`, `commit_file`, `eth_frame`, `write_a_lock`, `records.aeth_lock`. A의 `ewbot.paper`: `notify`(`HEAD = "[ewbot paper]"`, `epoch_text`, `discrepancy_text`, `data_hash_text`, `holdout_trades_text`, `gap_text`, `GAP_WARN_S`, `NY`, `Sender`, `daily_summary_due`, `decision_events(fresh, trades, open_key, epoch_start, count, cur_closed, tp1_frac)`, `deliver`, `gap_seconds`, `last_ok_run_ts`, `make_event`, `position_line`, `queue_events`), `criteria`(`PAPER_MIN_DAYS = 28`, `criterion4(ledger, now_ts)`), `decisions`(`build_decisions`, `decisions_to_record`, `known_at`, `open_position_record`, `paper_trades`, `split_result`, `verify_decisions`), `environment`(`environment_digest`, `runtime_environment`), `ledger`(`RUNS_LOG`, `Ledger`, `LedgerError`, `append_log_line`, `current_epoch`, `ensure_epoch`, `golive_count`(50건), `load_ledger`, `replace_epoch_trades`, `save_ledger`), `trace.traced_engine`; `run_backtest(..., allow_holdout=True)`; `data_fingerprint`; `fill_gaps`. 테스트: `tests.paper.helpers`: `Clock`, `FakeSender`, `git`, `ts`.
- Produces: `ewbot.aeth.paper`: `HEAD_AETH = "[ewbot paper-A-ETH]"`, `FETCH_ATTEMPTS = 3`, `class AethPaperError(RuntimeError)`(`stage`, `message`), `eth_head(text: str) -> str`, `default_fetch(cache_dir: Path, lock_path: Path) -> Path`, `@dataclass AethPaperEnv(ledger_dir, lock_path=LOCK_PATH, a_lock_path=A_LOCK_PATH, cache_dir=Path("data/cache"), fetch=default_fetch, sender=None, clock=_wall_clock, fingerprint=current_epoch_fingerprint, sleep=time.sleep, fetch_backoff_s=10.0, environment=runtime_environment)`, `@dataclass(frozen=True) AethRunOutcome(run_id, last_bar_ts, epoch_id, new_decisions, new_discrepancies, events, sent, pending, notes, status)`, `precheck_lock_aeth(lock_path: Path, a_lock_path: Path = A_LOCK_PATH) -> dict`, `holdout_summary_aeth(lock: dict) -> dict`, `new_ledger_aeth(lock: dict) -> Ledger`, `status_aeth(ledger: Ledger, now_ts: int) -> dict`(`epoch_id, eligible, count, holdout_passed, count_met, c4, auto_met`), `status_lines_aeth(st: dict) -> list[str]`, `run_paper_aeth(env: AethPaperEnv) -> AethRunOutcome`.

A의 `paper/runner.py`의 `run_paper`를 해석 9대로 옮긴다: 단계 이름(`precheck`, `ledger`, `data`, `fingerprint`, `recompute`, `events`, `write`, `notify`)과 순서, 판단 대조, 이벤트, 원자적 쓰기는 A와 같고, 설정·잠금·데이터·구간 지문·접두어만 A-ETH의 것이다. 잠금의 `first_run`이 합격이 아니면 실행하지 않는다(A-ETH가 끝났다). 리터럴 `allow_holdout=True`(`run_backtest`)를 쓰므로 `ALLOWED_TRUE`에 `aeth/paper.py`를 더한다.

- [ ] **Step 1: 실패하는 테스트 작성**

합성 ETH 경로(해석 14)와 홀드아웃이 끝난 합성 잠금으로 첫 실행(홀드아웃 재현 2건, 모의 2건, 4/50, 장부와 `runs.log`만, 접두어), 같은 시간 재실행과 새 봉(진입, 실행 공백, 일일 요약과 상태 줄), 구간 변경(카운트 리셋, 판정 대상 아님), 사전 점검 거부(이유별 7가지; 아무것도 쓰지 않음), 홀드아웃 데이터·거래 수 경고(한 번씩, 실행은 계속), 수집 재시도(10초, 20초)와 3회 실패, 다른 잠금의 장부 거부, 상태 줄, 접두어 바꾸기를 확인한다.

**파일 `tests/aeth/test_paper.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json
import os

import pytest

from ewbot.aeth.fingerprint import current_epoch_fingerprint
from ewbot.aeth.paper import (
    HEAD_AETH,
    AethPaperEnv,
    AethPaperError,
    eth_head,
    holdout_summary_aeth,
    run_paper_aeth,
    status_aeth,
    status_lines_aeth,
)
from ewbot.paper.environment import environment_digest
from ewbot.paper.ledger import LEDGER_FILE, RUNS_LOG, load_ledger
from ewbot.types import H1
from tests.aeth.helpers import FakeCacheFetch, commit_file, eth_frame, write_a_lock
from tests.aeth.records import aeth_lock
from tests.paper.helpers import BOUNDARY, Clock, FakeSender, git, ts

FIRST = ts("2024-02-10 12:07")  # 07:07 New York: no daily summary
ENV = {"python": "3.12.13", "numpy": "2.1.0", "pandas": "2.2.3", "pyarrow": "18.0.0", "uv_lock_sha256": "a" * 64,
       "image_os": "ubuntu24", "image_version": "20261001.1.0"}


@pytest.fixture(scope="module")
def df():
    return eth_frame()


@pytest.fixture
def make_env(tmp_path, df):
    a_lock = write_a_lock(tmp_path / "a")

    def make(lock: dict | None = None, now: int = FIRST, **kw) -> AethPaperEnv:
        text = json.dumps(aeth_lock(df, a_lock) if lock is None else lock, indent=2)
        lock_path = commit_file(tmp_path / "repo", "holdout-aeth.lock", text)
        clock = Clock(now)
        defaults = dict(ledger_dir=tmp_path / "paper-aeth", lock_path=lock_path, a_lock_path=a_lock,
                        cache_dir=tmp_path / "cache", fetch=FakeCacheFetch(df, clock), sender=FakeSender(), clock=clock,
                        sleep=lambda s: None, environment=lambda: dict(ENV))
        defaults.update(kw)
        return AethPaperEnv(**defaults)

    make.a_lock = a_lock
    return make


def test_first_run_reproduces_the_holdout_and_records_the_eth_paper_trades(make_env):
    env = make_env()
    out = run_paper_aeth(env)
    led = load_ledger(env.ledger_dir)
    assert sorted(os.listdir(env.ledger_dir)) == [LEDGER_FILE, RUNS_LOG]  # no report.html
    assert [e["id"] for e in led.epochs] == [1] and led.epochs[0]["started_ts"] == BOUNDARY
    assert led.baseline["fingerprint"] == current_epoch_fingerprint()  # A's + A-ETH's at the lock = now
    assert [t["entry_ts"] for t in led.trades] == [ts("2024-01-12 15:00"), ts("2024-02-07 21:00")]
    run = led.runs[0]
    assert run["holdout_trades_recomputed"] == 2 and run["holdout_data_ok"] is True and run["warnings"] == []
    assert run["count_total"] == 4 and out.status["count"]["required"] == 50  # 2 holdout + 2 paper, of 50
    assert run["env_sha256"] == environment_digest(ENV) and all(d["epoch_id"] == 1 for d in led.decisions)
    texts = env.sender.texts
    assert texts and all(t.startswith(HEAD_AETH) for t in texts)
    assert not any(t.startswith("[ewbot paper]") for t in texts)
    assert sum("신규 진입" in t for t in texts) == 2 and sum("청산" in t for t in texts) == 2
    assert led.holdout == holdout_summary_aeth(json.loads(env.lock_path.read_text(encoding="utf-8")))
    assert (env.ledger_dir / RUNS_LOG).read_text().count("\n") == 1


def test_a_rerun_in_the_same_hour_adds_nothing_and_new_bars_add_only_their_decisions(make_env):
    env = make_env()
    run_paper_aeth(env)
    before = load_ledger(env.ledger_dir)
    env.clock.now += 60
    out = run_paper_aeth(env)
    assert out.new_decisions == 0 and out.events == [] and load_ledger(env.ledger_dir).decisions == before.decisions
    n = len(env.sender.texts)
    env.clock.now = ts("2024-02-16 03:07")  # the 02-16 02:00 short has filled; 22:07 New York
    out = run_paper_aeth(env)
    new = env.sender.texts[n:]
    assert out.new_decisions == 2 and "신규 진입" in new[0] and "W3 숏" in new[0]
    assert "실행 공백" in new[1]
    assert "일일 요약 2024-02-15 (뉴욕)" in new[2] and "ETH 표본 외 누적 4/50" in new[2]
    assert "A-ETH 실거래 검토 조건" in new[2] and "A-BTC CANARY" in new[2]


def test_a_changed_strategy_a_or_aeth_fingerprint_opens_a_new_epoch_and_restarts_the_count(make_env):
    env = make_env()
    run_paper_aeth(env)
    env.fingerprint = lambda: "edited"
    env.clock.now += H1
    out = run_paper_aeth(env)
    led = load_ledger(env.ledger_dir)
    assert [e["id"] for e in led.epochs] == [1, 2]
    assert out.status["count"] == {"epoch_id": 2, "eligible": False, "holdout": 0, "paper": 0, "total": 0,
                                   "required": 50}
    assert out.status["auto_met"] is False and "판정 대상 아님" in status_lines_aeth(out.status)[1]
    assert env.sender.texts[-1].startswith(HEAD_AETH) and "구간 2" in env.sender.texts[-1]


@pytest.mark.parametrize("case, why", [
    ("missing", "not a valid A-ETH lock"), ("uncommitted", "differs from the committed"),
    ("modified", "differs from the committed"), ("not_run", "has not run"), ("failed", "did not pass"),
    ("a_params", "a_lock_sha256"), ("config", "config"),
])
def test_the_precheck_refuses_and_writes_nothing(make_env, df, tmp_path, case, why):
    lock = aeth_lock(df, make_env.a_lock, passed=case != "failed")
    if case == "not_run":
        lock["first_run"] = None
    if case == "config":
        lock = aeth_lock(df, make_env.a_lock, config={"costs": {"contract_size": 0.01}})
    kw = {"a_lock_path": write_a_lock(tmp_path / "other", random_runs=3)} if case == "a_params" else {}
    env = make_env(lock=lock, **kw)
    if case == "missing":
        git(env.lock_path.parent, "rm", "-q", env.lock_path.name)
    if case == "uncommitted":
        git(env.lock_path.parent, "rm", "-q", "--cached", env.lock_path.name)
        git(env.lock_path.parent, "commit", "-q", "-m", "untrack")
    if case == "modified":
        data = json.loads(env.lock_path.read_text(encoding="utf-8"))
        data["attempts"].append({"note": "edited"})
        env.lock_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AethPaperError, match=why) as exc:
        run_paper_aeth(env)
    assert exc.value.stage == "precheck"
    assert not env.ledger_dir.exists() and env.fetch.calls == 0 and env.sender.texts == []


def test_holdout_drift_warns_once_and_never_stops_the_run(make_env, df):
    lock = aeth_lock(df, make_env.a_lock, n_trades=3)
    lock["first_run"]["data"]["sha256"] = "0" * 64  # first_run is not bound by the digest
    env = make_env(lock=lock)
    out = run_paper_aeth(env)
    run = load_ledger(env.ledger_dir).runs[-1]
    assert run["status"] == "ok" and run["holdout_data_ok"] is False and run["holdout_trades_recomputed"] == 2
    assert out.notes[0].endswith("!= lock 000000000000") and out.notes[1] == "holdout trades recomputed 2 != lock 3"
    env.clock.now += H1
    run_paper_aeth(env)
    texts = env.sender.texts
    assert sum("데이터 해시 불일치" in t for t in texts) == 1 and sum("홀드아웃 재현 불일치" in t for t in texts) == 1


def test_fetch_is_retried_three_times_then_the_run_fails_at_data(make_env):
    sleeps: list[float] = []
    env = make_env(sleep=sleeps.append)
    env.fetch.fail = 2
    run_paper_aeth(env)
    assert env.fetch.calls == 3 and sleeps == [10.0, 20.0]
    env2 = make_env(ledger_dir=env.ledger_dir.parent / "other-ledger")
    env2.fetch.fail = 3
    with pytest.raises(AethPaperError) as exc:
        run_paper_aeth(env2)
    assert exc.value.stage == "data" and not (env2.ledger_dir / LEDGER_FILE).exists()


def test_a_ledger_of_another_lock_is_refused(make_env, df):
    env = make_env()
    run_paper_aeth(env)
    other = make_env(lock=aeth_lock(df, make_env.a_lock, n_trades=3))
    with pytest.raises(AethPaperError, match="another holdout-aeth.lock .*n_trades") as exc:
        run_paper_aeth(other)
    assert exc.value.stage == "precheck"


def test_status_needs_the_passed_holdout_fifty_trades_and_criterion_4(make_env):
    env = make_env()
    run_paper_aeth(env)
    led = load_ledger(env.ledger_dir)
    st = status_aeth(led, env.clock.now)
    assert st["holdout_passed"] and not st["count_met"] and not st["c4"]["met"] and not st["auto_met"]
    lines = status_lines_aeth(st)
    assert lines[1] == "1. 단계 2 홀드아웃 합격 (A의 9.6절 1·2번): 충족"
    assert lines[2] == "2. ETH 표본 외 거래 4/50 (홀드아웃 2 + 모의 2): 미충족"
    assert lines[-1] == "=> Plan 5c 검토 불가"


def test_eth_head_replaces_only_strategy_a_s_paper_prefix():
    assert eth_head("[ewbot paper] 신규 진입") == f"{HEAD_AETH} 신규 진입"
    with pytest.raises(ValueError):
        eth_head("[ewbot paper-B] 신규 진입")
```

**수정 `tests/paper/test_static.py`** (1/1) — 다음 부분을 찾아:

```python
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py", "live/target.py",  # Plan 2 spec 7.1, Plan 3 spec 3.1
                "aeth/holdout.py"}  # Plan 5a spec 7.2: the one-shot ETH holdout
```

바꿀 내용:

```python
ALLOWED_TRUE = {"backtest/holdout.py", "paper/runner.py", "live/target.py",  # Plan 2 spec 7.1, Plan 3 spec 3.1
                "aeth/holdout.py", "aeth/paper.py"}  # Plan 5a spec 7.2, 8: the one-shot ETH holdout, paper-A-ETH
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/aeth/test_paper.py tests/paper/test_static.py -q
```

예상: `No module named 'ewbot.aeth.paper'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/aeth/paper.py`** (새 파일, 전체 내용)

```python
"""One `ewbot aeth paper run` (Plan 5a spec 8): strategy A's paper runtime (ewbot.paper.runner) adapted to ETH - the
product (ETH-USD), the costs (ETP, 0.1 ETH per contract) and the lock (holdout-aeth.lock) - with strategy A's paper
components imported unchanged: the traced engine run, the decisions and the replay check, the ledger schema, epochs
and the go-live count, criterion 4, the runtime environment, Telegram delivery and message formats (with the prefix
`[ewbot paper-A-ETH]`).

precheck -> data -> epoch -> full recompute -> out-of-sample extraction -> replay check -> ledger and runs.log ->
Telegram. The precheck refuses a lock that is not valid (digest) or not committed unchanged, whose holdout has not
run or did not pass (A-ETH ended), or whose fields are not this code's for strategy A's params now (lock_mismatch).
Each run recomputes from the first cached bar (2017-01-01) and trades from 2024-01-01, like the holdout, so it
reproduces the holdout and continues it; closed trades entering after the lock's last bar are paper trades, and the
count to 50 is the holdout's A trades plus the paper trades of the baseline epoch (strategy A's golive_count, on
this ledger only: ETH trades are never pooled with strategy A's BTC count). An epoch is bound to strategy A's
fingerprint and the A-ETH fingerprint together (fingerprint.epoch_fingerprint): a change of either opens a new epoch
and restarts the count. The holdout window's bars no longer hashing to first_run.data.sha256 is warned once per
hash, never refused; decisions that change because of it are DATA_REVISION. No report.html and no daily criteria
1-2 recompute (spec 8): the go-live block is status_aeth / status_lines_aeth. Every step runs inside a named stage:
any failure raises AethPaperError(stage); nothing is written before the write stage."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ewbot.aeth.config import A_LOCK_PATH, LOCK_PATH, AethError, aeth_config
from ewbot.aeth.data import fetch_eth, load_eth
from ewbot.aeth.fingerprint import current_epoch_fingerprint, epoch_fingerprint
from ewbot.aeth.holdout import lock_mismatch
from ewbot.aeth.lockfile import committed_text, read_lock
from ewbot.backtest.holdout import data_fingerprint
from ewbot.backtest.runner import run_backtest
from ewbot.backtest.walkforward import HOLDOUT_START
from ewbot.data.candles import fill_gaps
from ewbot.paper import notify as a_notify
from ewbot.paper.criteria import PAPER_MIN_DAYS, criterion4
from ewbot.paper.decisions import (
    build_decisions,
    decisions_to_record,
    known_at,
    open_position_record,
    paper_trades,
    split_result,
    verify_decisions,
)
from ewbot.paper.environment import environment_digest, runtime_environment
from ewbot.paper.ledger import (
    RUNS_LOG,
    Ledger,
    LedgerError,
    append_log_line,
    current_epoch,
    ensure_epoch,
    golive_count,
    load_ledger,
    replace_epoch_trades,
    save_ledger,
)
from ewbot.paper.notify import (
    GAP_WARN_S,
    NY,
    Sender,
    daily_summary_due,
    decision_events,
    deliver,
    gap_seconds,
    last_ok_run_ts,
    make_event,
    position_line,
    queue_events,
)
from ewbot.paper.trace import traced_engine
from ewbot.types import H1

HEAD_AETH = "[ewbot paper-A-ETH]"
FETCH_ATTEMPTS = 3


class AethPaperError(RuntimeError):
    """A run failed at `stage` (named in the failure alert); the workflow commits nothing after it."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


def eth_head(text: str) -> str:
    """Strategy A's message with the A-ETH prefix in place of `[ewbot paper]`."""
    if not text.startswith(a_notify.HEAD):
        raise ValueError(f"not a paper message: {text[:40]!r}")
    return HEAD_AETH + text[len(a_notify.HEAD):]


def default_fetch(cache_dir: Path, lock_path: Path) -> Path:
    return fetch_eth(Path(cache_dir), lock_path=Path(lock_path))


def _wall_clock() -> int:
    return int(time.time())


@dataclass
class AethPaperEnv:
    """Everything a run touches. Production uses the defaults; tests inject fakes (no network, temp git repos)."""

    ledger_dir: Path
    lock_path: Path = LOCK_PATH
    a_lock_path: Path = A_LOCK_PATH
    cache_dir: Path = Path("data/cache")
    fetch: Callable[[Path, Path], Path] = default_fetch
    sender: Sender | None = None
    clock: Callable[[], int] = _wall_clock
    fingerprint: Callable[[], str] = current_epoch_fingerprint
    sleep: Callable[[float], None] = time.sleep
    fetch_backoff_s: float = 10.0
    environment: Callable[[], dict[str, Any]] = runtime_environment


@dataclass(frozen=True)
class AethRunOutcome:
    run_id: str
    last_bar_ts: int
    epoch_id: int
    new_decisions: int
    new_discrepancies: int
    events: list[str]
    sent: list[str]
    pending: int
    notes: list[str]
    status: dict[str, Any]


@contextmanager
def _stage(name: str, *, detail: bool = True) -> Iterator[None]:
    try:
        yield
    except AethPaperError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its stage
        if detail:
            raise AethPaperError(name, f"{type(exc).__name__}: {exc}") from exc
        raise AethPaperError(name, f"{type(exc).__name__} (details withheld: may contain the Telegram token)") from None


def precheck_lock_aeth(lock_path: Path, a_lock_path: Path = A_LOCK_PATH) -> dict[str, Any]:
    """The committed, passed A-ETH lock of this code for strategy A's params now."""
    try:
        lock = read_lock(lock_path)
        text = committed_text(lock_path)
        if text is None or json.loads(text) != lock:
            raise AethPaperError("precheck", f"{lock_path} differs from the committed version (or is not committed)")
        fr = lock.get("first_run")
        if not isinstance(fr, dict) or "verdict" not in fr or "data" not in fr:
            raise AethPaperError("precheck", "the ETH holdout has not run: paper trading starts after it passes")
        if not fr["verdict"].get("passed"):
            raise AethPaperError("precheck", "the ETH holdout did not pass (spec 7.3): A-ETH ended, no paper trading")
        stale = lock_mismatch(lock, a_lock_path)
    except AethError as exc:
        raise AethPaperError("precheck", str(exc)) from exc
    if stale:
        raise AethPaperError("precheck", f"the lock's {stale} are not this code's for strategy A's params")
    return lock


def holdout_summary_aeth(lock: dict[str, Any]) -> dict[str, Any]:
    """The part of holdout-aeth.lock the ledger is bound to (the precheck validated it)."""
    fr = lock["first_run"]
    return {
        "lock_sha256": lock["sha256"],
        "params": [float(p) for p in lock["params"]],
        "holdout_start": int(lock["holdout_start"]),
        "last_bar_ts": int(lock["last_bar_ts"]),
        "data_sha256": fr["data"]["sha256"],
        "n_trades": {"A": int(fr["verdict"]["n_trades"])},
        "passed": bool(fr["verdict"]["passed"]),
    }


def new_ledger_aeth(lock: dict[str, Any]) -> Ledger:
    """Epoch 1 starts at the lock's last bar with the lock's epoch fingerprint (A's and A-ETH's at the lock)."""
    hs = holdout_summary_aeth(lock)
    fp = epoch_fingerprint(lock["a_fingerprint"], lock["aeth_sha256"])
    start = hs["last_bar_ts"]
    epoch = {"id": 1, "fingerprint": fp, "started_ts": start, "reason": "baseline: holdout-aeth.lock",
             "through_ts": start}
    return Ledger(baseline={"fingerprint": fp, "commit": str(lock.get("git_head") or "holdout-aeth.lock")}, holdout=hs,
                  epochs=[epoch])


def status_aeth(ledger: Ledger, now_ts: int) -> dict[str, Any]:
    """Spec 8.1, the automatic part: holdout passed, 50 ETH out-of-sample trades, criterion 4 (strategy A's)."""
    count = golive_count(ledger)
    c4 = criterion4(ledger, now_ts)
    passed = bool(ledger.holdout.get("passed"))
    enough = bool(count["eligible"] and count["total"] >= count["required"])
    return {"epoch_id": int(current_epoch(ledger)["id"]), "eligible": bool(count["eligible"]), "count": count,
            "holdout_passed": passed, "count_met": enough, "c4": c4,
            "auto_met": bool(count["eligible"] and passed and enough and c4["met"])}


def _mark(ok: bool) -> str:
    return "충족" if ok else "미충족"


def status_lines_aeth(st: dict[str, Any]) -> list[str]:
    """Korean go-live block for the daily summary and `ewbot aeth paper status`."""
    count, c4 = st["count"], st["c4"]
    lines = ["A-ETH 실거래 검토 조건 (Plan 5a 명세 8.1)"]
    if not st["eligible"]:
        lines.append(f"현재 구간 {st['epoch_id']}의 지문(A 지문 + A-ETH 지문)이 잠금 파일과 다름: 판정 대상 아님")
    lines.append(f"1. 단계 2 홀드아웃 합격 (A의 9.6절 1·2번): {_mark(st['holdout_passed'])}")
    lines.append(f"2. ETH 표본 외 거래 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 "
                 f"{count['paper']}): {_mark(st['count_met'])}")
    env = f", 실행 환경 변경 불일치 {c4['environment']}건(집계 제외)" if c4.get("environment") else ""
    lines.append(f"3. 모의매매 재현: {_mark(c4['met'])} (운영 {c4['days']:.1f}/{PAPER_MIN_DAYS}일, 커버리지 "
                 f"{c4['coverage'] * 100:.1f}%, 원인 불명 불일치 {c4['unexplained']}건{env})")
    lines.append("4. A-BTC CANARY 통과: 수동 확인")
    lines.append("5. 전체 표본 외 구간의 9.6절 1·2번 재판정, 사용자 승인: Plan 5c에서 수동")
    lines.append("=> 자동 조건 충족: Plan 5c 검토 가능 (자동 전환하지 않음)" if st["auto_met"] else "=> Plan 5c 검토 불가")
    return lines


def _open_ledger(ledger_dir: Path, lock: dict[str, Any]) -> Ledger:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    try:
        ledger = load_ledger(ledger_dir)
    except LedgerError as exc:
        raise AethPaperError("ledger", str(exc)) from exc
    if ledger is None:
        return new_ledger_aeth(lock)
    bound = holdout_summary_aeth(lock)
    diff = sorted(k for k in bound.keys() | ledger.holdout.keys() if ledger.holdout.get(k) != bound.get(k))
    if diff:
        raise AethPaperError("precheck", f"ledger.json was started from another holdout-aeth.lock (differs in: {diff})")
    return ledger


def _load_data(env: AethPaperEnv) -> pd.DataFrame:
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
        raise AethPaperError("data", f"candle update failed {FETCH_ATTEMPTS} times: {type(last).__name__}: {last}")
    with _stage("data"):
        raw = load_eth(path, unlock_2024=True, lock_path=env.lock_path)
        if raw.empty:
            raise AethPaperError("data", f"{path} has no bars")
        return fill_gaps(raw)


def _recording_floor(ledger: Ledger) -> int:
    if len(ledger.epochs) == 1:
        return int(ledger.epochs[0]["started_ts"])
    return int(ledger.epochs[-2]["through_ts"])


def _run_id(now: int) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")


def _ny9_ts(ny_date: str) -> int:
    y, m, d = (int(x) for x in ny_date.split("-"))
    return int(datetime(y, m, d, 9, tzinfo=NY).timestamp())


def run_paper_aeth(env: AethPaperEnv) -> AethRunOutcome:
    with _stage("precheck"):
        now = int(env.clock())
        run_id = _run_id(now)
        lock = precheck_lock_aeth(Path(env.lock_path), Path(env.a_lock_path))
        cfg = aeth_config(tuple(lock["params"]))
        ledger_dir = Path(env.ledger_dir)
        run_env = dict(env.environment())
        env_sha = environment_digest(run_env)
    with _stage("ledger"):
        ledger = _open_ledger(ledger_dir, lock)
        hs = ledger.holdout
        gap = gap_seconds(now, ledger.runs)
        prev_ok = last_ok_run_ts(ledger.runs)
    df = _load_data(env)
    with _stage("data"):
        last_bar = int(df["ts"].iloc[-1])
        if last_bar < hs["last_bar_ts"]:
            raise AethPaperError("data", f"data ends at {last_bar}, before the lock's last bar {hs['last_bar_ts']}")
    with _stage("fingerprint"):
        epoch, created = ensure_epoch(ledger, env.fingerprint(), last_bar)
        floor = _recording_floor(ledger)
        if created:
            epoch["through_ts"] = floor
    with _stage("recompute"):
        with traced_engine() as trace:
            res = run_backtest(cfg, df, HOLDOUT_START, None, allow_holdout=True)
        closed, open_trade = split_result(res, trace)
        all_trades = closed + ([open_trade] if open_trade is not None else [])
        by_entry = {t.entry_ts: t for t in all_trades}
        holdout_n = sum(1 for t in all_trades if HOLDOUT_START <= t.entry_ts <= hs["last_bar_ts"])
        cur_closed = paper_trades(closed, int(epoch["started_ts"]))
        replace_epoch_trades(ledger, epoch["id"], cur_closed)
        ledger.open_position = (
            None if open_trade is None else open_position_record(open_trade, trace, int(epoch["started_ts"]))
        )
        memo: dict[int, str] = {}

        def prefix(ts: int) -> str:
            if ts not in memo:
                memo[ts] = data_fingerprint(df, 0, int(ts))
            return memo[ts]

        data_sha = prefix(last_bar)
        holdout_sha = data_fingerprint(df, HOLDOUT_START, hs["last_bar_ts"])
        holdout_ok = holdout_sha == hs["data_sha256"]
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

        recomputed = build_decisions(res, trace, all_trades)
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
        count = golive_count(ledger)
        lock_n = int(hs["n_trades"]["A"])
        warnings: list[str] = []
        events: list[dict[str, Any]] = []
        if created:
            events.append(make_event("EPOCH", epoch["started_ts"], epoch["id"], eth_head(a_notify.epoch_text(epoch))))
        for ev in decision_events(fresh, by_entry, None if open_trade is None else open_trade.entry_ts,
                                  int(epoch["started_ts"]), count, cur_closed, cfg.strategy.tp1_frac):
            events.append({**ev, "text": eth_head(ev["text"])})
        if new_disc:
            events.append(make_event("DISCREPANCY", last_bar, len(ledger.discrepancies),
                                     eth_head(a_notify.discrepancy_text(new_disc))))
        if not holdout_ok:  # one message per distinct revised hash (the event id carries it)
            warnings.append(f"holdout data hash {holdout_sha[:12]} != lock {hs['data_sha256'][:12]}")
            events.append(make_event("DATA_HASH", hs["last_bar_ts"], holdout_sha[:12],
                                     eth_head(a_notify.data_hash_text(hs["data_sha256"], holdout_sha))))
        if holdout_n != lock_n:  # one message per distinct recomputed count
            warnings.append(f"holdout trades recomputed {holdout_n} != lock {lock_n}")
            events.append(make_event("HOLDOUT_TRADES", hs["last_bar_ts"], holdout_n,
                                     eth_head(a_notify.holdout_trades_text(lock_n, holdout_n))))
        if gap is not None and prev_ok is not None and gap > GAP_WARN_S:
            events.append(make_event("GAP", now // H1 * H1, 0, eth_head(a_notify.gap_text(gap, prev_ok))))
        run: dict[str, Any] = {
            "id": run_id, "ts": now, "status": "ok", "last_bar_ts": last_bar, "data_sha256": data_sha,
            "holdout_data_ok": holdout_ok, "holdout_trades_recomputed": holdout_n, "epoch_id": epoch["id"],
            "paper_trades": count["paper"], "count_total": count["total"], "new_decisions": len(fresh),
            "new_discrepancies": len(new_disc), "env": run_env, "env_sha256": env_sha, "warnings": warnings,
        }
        ledger.runs.append(run)
        status = status_aeth(ledger, now)
        due = daily_summary_due(now, ledger.daily_summary_date)
        if due is not None:
            eq = {p.ts: p.equity for p in res.equity}
            base_eq = eq.get(hs["last_bar_ts"], cfg.initial_equity)
            last_eq = res.equity[-1].equity if res.equity else cfg.initial_equity
            text = "\n".join([
                f"{HEAD_AETH} 일일 요약 {due} (뉴욕)",
                position_line(ledger.open_position),
                f"자산 {last_eq:,.2f} USD (홀드아웃 이후 {last_eq / base_eq - 1:+.2%})",
                f"ETH 표본 외 누적 {count['total']}/{count['required']} (홀드아웃 {count['holdout']} + 모의 "
                f"{count['paper']}) · 구간 {epoch['id']}",
                *status_lines_aeth(status),
            ])
            events.append(make_event("DAILY", _ny9_ts(due), 0, text))
            ledger.daily_summary_date = due
        queued = queue_events(ledger, events)
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
        append_log_line(
            ledger_dir / RUNS_LOG,
            f"{datetime.fromtimestamp(now, UTC).isoformat(timespec='seconds')}\tok\tlast_bar="
            f"{datetime.fromtimestamp(last_bar, UTC).isoformat(timespec='minutes')}\tdata={data_sha[:12]}\t"
            f"trades={count['total']}/{count['required']}\tepoch={epoch['id']}\tnew_decisions={len(fresh)}",
        )
    with _stage("notify", detail=False):
        remaining, sent, errors = deliver(ledger.pending_events, ledger.sent_event_ids, env.sender, sleep=env.sleep)
        ledger.pending_events = remaining
        ledger.sent_event_ids.extend(sent)
        run["notify"] = {"sent": len(sent), "pending": len(remaining), "errors": errors}
    with _stage("write"):
        save_ledger(ledger_dir, ledger)
    return AethRunOutcome(run_id, last_bar, int(epoch["id"]), len(fresh), len(new_disc), queued, sent, len(remaining),
                          warnings + errors, status)
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/aeth/test_paper.py tests/paper/test_static.py -q
```

예상: `43 passed`

`tests/aeth/test_paper.py` 15개와 `tests/paper/test_static.py` 28개다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/aeth/paper.py tests/aeth/test_paper.py tests/paper/test_static.py
git commit -m "feat(aeth): hourly paper-A-ETH run - A's paper runtime with the ETH product, costs and lock; its own 50-trade count" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: CLI `ewbot aeth …`

**Files:**
- Modify: `src/ewbot/cli.py` (모듈 설명, `build_parser`, A-ETH 처리 함수 절, `_COMMANDS`)
- Test: `tests/aeth/test_cli_aeth.py`

**Interfaces:**
- Consumes: Task 1~5: `ewbot.aeth.config`(모듈로: `LOCK_PATH`, `STAGE1_PATH`, `A_LOCK_PATH`, `AethError`), `ewbot.aeth.data`(`cache_path`, `fetch_eth`, `load_eth`), `ewbot.aeth.lockfile.lock_exists`, `ewbot.aeth.stage1.write_stage1`, `ewbot.aeth.holdout`(`create_lock`, `run_holdout_aeth`), `ewbot.aeth.paper`(`AethPaperEnv`, `AethPaperError`, `eth_head`, `run_paper_aeth`, `status_aeth`, `status_lines_aeth`). `cli.py`에 이미 있는 것: `_print_comparison`, `_refuse`, `RUN_URL_ENV`, `FAILURE_MARKER_ENV`, `check_integrity`, `WF_END`, `failure_text`, `deliver`, `make_event`, `sender_from_env`, `load_ledger`, `LedgerError`, `current_epoch`, `position_line`.
- Produces: `ewbot aeth fetch [--cache-dir]`, `ewbot aeth stage1 [--cache-dir] [--workers]`, `ewbot aeth holdout lock [--cache-dir]`, `ewbot aeth holdout run --confirm [--cache-dir] [--workers]`, `ewbot aeth paper run --ledger-dir D [--cache-dir]`, `ewbot aeth paper status --ledger-dir D`; 파서의 `e_cmd`·`eh_cmd`·`ep_cmd`; `cli.cmd_aeth(args) -> int`; `cli._e_paper_env(args) -> AethPaperEnv`(테스트가 바꾸는 배선).

A·B의 CLI와 같은 방식이다(해석 12). 처리 함수만 `ewbot.aeth`를 import하므로 `ewbot live …`·`ewbot paper …`가 불러오는 모듈은 그대로이고(테스트로 확인), 경로는 `ewbot.aeth.config` 모듈 속성을 호출 때 읽는다. 단계 1과 잠금·홀드아웃은 다음에 할 일(커밋 명령)을 출력한다. `holdout run`은 확인과 유효한 잠금을 먼저 확인하고 그 뒤에만 2024 이후 봉을 받는다.

- [ ] **Step 1: 실패하는 테스트 작성**

파서(하위 명령, 조정 옵션 없음), `ewbot.cli`가 `ewbot.aeth`를 불러오지 않음(새 프로세스), 실제 배선, `fetch` 출력, 단계 1이 전체 기간으로만 돌고 1회임, 잠금에 커밋된 단계 1 기록이 필요함, 홀드아웃이 확인과 잠금 전에는 아무것도 받지 않음, 모의매매 실행과 상태, 실패 알림(접두어, 표시 파일), 장부 없는 상태를 확인한다.

**파일 `tests/aeth/test_cli_aeth.py`** (새 파일, 전체 내용)

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import ewbot.aeth.config as ec
import ewbot.aeth.data as edata
from ewbot import cli
from ewbot.aeth.paper import HEAD_AETH, AethPaperEnv
from ewbot.cli import build_parser, main
from tests.aeth.helpers import (
    FakeCacheFetch,
    commit_file,
    eth_frame,
    init_repo,
    pre2024_frame,
    save_cache,
    write_a_lock,
)
from tests.aeth.records import aeth_lock
from tests.paper.helpers import Clock, FakeSender, ts


def _code(argv: list[str]) -> int:
    """main's exit code, also when a refusal exits through SystemExit (cli._refuse)."""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code)


def test_the_parser_has_the_spec_subcommands_and_no_tuning_options():
    p = build_parser()
    assert p.parse_args(["aeth", "fetch"]).e_cmd == "fetch"
    assert p.parse_args(["aeth", "stage1", "--workers", "2"]).workers == 2
    assert p.parse_args(["aeth", "holdout", "lock"]).eh_cmd == "lock"
    assert p.parse_args(["aeth", "holdout", "run", "--confirm"]).confirm is True
    assert p.parse_args(["aeth", "paper", "run", "--ledger-dir", "x"]).ep_cmd == "run"
    assert p.parse_args(["aeth", "paper", "status", "--ledger-dir", "x"]).ep_cmd == "status"
    for argv in (["aeth", "stage1", "--params", "2,0.7,0.5"], ["aeth", "stage1", "--product", "SOL-USD"],
                 ["aeth", "holdout", "run", "--random-runs", "5"], ["aeth", "paper", "run", "--ledger-dir", "x",
                                                                     "--lock", "y"]):
        with pytest.raises(SystemExit):
            p.parse_args(argv)


def test_the_cli_loads_aeth_only_for_aeth_commands():
    """`ewbot live …` and `ewbot paper …` run with the same modules as before A-ETH."""
    code = "import sys, ewbot.cli; print(sorted(m for m in sys.modules if m.startswith('ewbot.aeth')))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


def test_production_wiring_uses_the_repo_locks_and_env_secrets(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    env = cli._e_paper_env(build_parser().parse_args(["aeth", "paper", "run", "--ledger-dir", str(tmp_path)]))
    assert env.lock_path == ec.LOCK_PATH and env.a_lock_path == ec.A_LOCK_PATH and env.sender is None
    assert env.ledger_dir == tmp_path and env.cache_dir == Path("data/cache")


def test_fetch_reports_the_guarded_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ec, "LOCK_PATH", tmp_path / "none.lock")
    seen = []

    def fake_fetch(cache_dir, lock_path):
        seen.append(lock_path)
        return save_cache(Path(cache_dir), pre2024_frame())

    monkeypatch.setattr(edata, "fetch_eth", fake_fetch)
    assert main(["aeth", "fetch", "--cache-dir", str(tmp_path / "cache")]) == 0
    out = capsys.readouterr().out
    assert "ETH bars before 2024-01-01: 3000 (2023-08-29 00:00 .. 2023-12-31 23:00 UTC)" in out
    assert "stops at 2024-01-01 (no lock)" in out and seen == [tmp_path / "none.lock"]


def _stage1_world(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    monkeypatch.setattr(ec, "STAGE1_PATH", repo / "aeth-stage1.json")
    monkeypatch.setattr(ec, "LOCK_PATH", repo / "holdout-aeth.lock")
    monkeypatch.setattr(ec, "A_LOCK_PATH", write_a_lock(tmp_path / "a"))
    save_cache(tmp_path / "cache", pre2024_frame())
    return repo


def test_stage1_runs_on_the_whole_2017_2023_window_only(tmp_path, monkeypatch, capsys):
    _stage1_world(tmp_path, monkeypatch)  # synthetic bars from 2023-08-29: the CLI has no window option
    assert _code(["aeth", "stage1", "--cache-dir", str(tmp_path / "cache")]) == 2
    assert "run `ewbot aeth fetch` to completion first" in capsys.readouterr().err
    assert not ec.STAGE1_PATH.exists()


def test_stage1_runs_once(tmp_path, monkeypatch, capsys):
    repo = _stage1_world(tmp_path, monkeypatch)
    commit_file(repo, "aeth-stage1.json", "{}\n")
    assert _code(["aeth", "stage1", "--cache-dir", str(tmp_path / "cache")]) == 2
    assert "stage 1 runs once" in capsys.readouterr().err


def test_the_lock_needs_the_committed_stage1_record(tmp_path, monkeypatch, capsys):
    _stage1_world(tmp_path, monkeypatch)
    assert _code(["aeth", "holdout", "lock", "--cache-dir", str(tmp_path / "cache")]) == 2
    assert "must be committed unchanged" in capsys.readouterr().err and not ec.LOCK_PATH.exists()


@pytest.mark.parametrize("argv, why", [
    (["--confirm"], "not a valid A-ETH lock"),
    ([], "runs once, after the user's approval"),
])
def test_the_holdout_run_needs_confirm_and_a_lock_before_any_download(tmp_path, monkeypatch, capsys, argv, why):
    _stage1_world(tmp_path, monkeypatch)

    def no_network(*a, **k):
        raise AssertionError("no download before the checks")

    monkeypatch.setattr(edata, "fetch_eth", no_network)
    assert _code(["aeth", "holdout", "run", "--cache-dir", str(tmp_path / "cache"), *argv]) == 2
    assert why in capsys.readouterr().err


@pytest.fixture
def senders(tmp_path, monkeypatch):
    built = []
    df = eth_frame()
    a_lock = write_a_lock(tmp_path / "a")
    lock_path = commit_file(tmp_path / "repo", "holdout-aeth.lock", json.dumps(aeth_lock(df, a_lock), indent=2))

    def factory(args):
        clock = Clock(ts("2024-02-10 12:07"))
        env = AethPaperEnv(ledger_dir=args.ledger_dir, lock_path=lock_path, a_lock_path=a_lock,
                           cache_dir=tmp_path / "cache", fetch=FakeCacheFetch(df, clock), sender=FakeSender(),
                           clock=clock, sleep=lambda s: None, environment=lambda: {"python": "3.12.13"})
        built.append(env)
        return env

    monkeypatch.setattr(cli, "_e_paper_env", factory)
    return built


def test_paper_run_then_status(senders, tmp_path, capsys):
    ledger_dir = tmp_path / "paper-aeth"
    assert main(["aeth", "paper", "run", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("paper-A-ETH run 20240210T120700Z") and "ETH out-of-sample 4/50" in out
    assert main(["aeth", "paper", "status", "--ledger-dir", str(ledger_dir)]) == 0
    out = capsys.readouterr().out
    assert "A-ETH 실거래 검토 조건 (Plan 5a 명세 8.1)" in out and "1. 단계 2 홀드아웃 합격 (A의 9.6절 1·2번): 충족" in out
    assert "holdout data ok, holdout trades recomputed 2 (lock: 2)" in out and "discrepancies 0 (unexplained 0)" in out


def test_a_failed_paper_run_alerts_with_the_a_eth_prefix(senders, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "notified"
    monkeypatch.setenv("EWBOT_RUN_URL", "https://github.com/o/r/actions/runs/9")
    monkeypatch.setenv("EWBOT_FAILURE_MARKER", str(marker))
    real = cli._e_paper_env

    def failing(args):
        env = real(args)
        env.fetch.fail = 3
        return env

    monkeypatch.setattr(cli, "_e_paper_env", failing)
    assert main(["aeth", "paper", "run", "--ledger-dir", str(tmp_path / "pa")]) == 1
    assert "failed at stage data" in capsys.readouterr().err
    (env,) = senders
    assert env.sender.texts == [f"{HEAD_AETH} 실패 · 단계 data\ncandle update failed 3 times: ConnectionError: coinbase "
                                "down\n로그: https://github.com/o/r/actions/runs/9"]
    assert marker.read_text() == "data" and not (tmp_path / "pa" / "ledger.json").exists()


def test_status_without_a_ledger_fails(tmp_path, capsys):
    assert main(["aeth", "paper", "status", "--ledger-dir", str(tmp_path)]) == 1
    assert "no ledger.json" in capsys.readouterr().err
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/aeth/test_cli_aeth.py -q
```

예상: `invalid choice: 'aeth'`
예상: `9 failed, 1 passed, 2 errors`

`aeth` 하위 명령이 아직 없어 파서가 거부하고(`invalid choice: 'aeth'`), `cli._e_paper_env`가 없어 그것을 바꾸는 픽스처를 쓰는 테스트 2개는 준비 단계에서 오류가 난다. `ewbot.cli`가 `ewbot.aeth`를 불러오지 않는다는 테스트 1개는 지금도 통과한다.

- [ ] **Step 3: 최소 구현**

**수정 `src/ewbot/cli.py`** (1/4) — 다음 부분을 찾아:

```python
ledger (paper-b branch); its research commands never read a bar from 2024-01-01 on (data15 filters them out)."""
```

바꿀 내용:

```python
ledger (paper-b branch); its research commands never read a bar from 2024-01-01 on (data15 filters them out).
`aeth fetch | stage1 | holdout lock | holdout run | paper run | paper status` (Plan 5a) is A-ETH (ewbot.aeth):
strategy A, frozen, on ETH-USD with its own 1h cache, stage-1 record (aeth-stage1.json), lock (holdout-aeth.lock)
and ledger (paper-aeth branch); like `paper` it has no strategy or config options, and ewbot.aeth is imported only
inside its handlers, so `ewbot live …` and `ewbot paper …` never load it."""
```

**수정 `src/ewbot/cli.py`** (2/4) — 다음 부분을 찾아:

```python
    p_bps = bp_sub.add_parser("status", help="print the paper-B ledger's status (read-only, no network)")
    p_bps.add_argument("--ledger-dir", type=Path, required=True)
    return parser
```

바꿀 내용:

```python
    p_bps = bp_sub.add_parser("status", help="print the paper-B ledger's status (read-only, no network)")
    p_bps.add_argument("--ledger-dir", type=Path, required=True)

    p_e = sub.add_parser("aeth", help="A-ETH (Plan 5a): strategy A, frozen, on ETH - stage 1, one-shot holdout, paper")
    e_sub = p_e.add_subparsers(dest="e_cmd", required=True)
    e_cache = argparse.ArgumentParser(add_help=False)
    e_cache.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    e_sub.add_parser("fetch", parents=[e_cache], help="update the ETH-USD 1h cache (before 2024 until holdout-aeth.lock)")
    p_e1 = e_sub.add_parser("stage1", parents=[e_cache], help="stage 1, ONCE: A on ETH 2017-2023 -> aeth-stage1.json")
    p_e1.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_eh = e_sub.add_parser("holdout", help="holdout-aeth.lock and the ONE-SHOT ETH holdout")
    eh_sub = p_eh.add_subparsers(dest="eh_cmd", required=True)
    eh_sub.add_parser("lock", parents=[e_cache], help="write holdout-aeth.lock from aeth-stage1.json (commit it)")
    p_ehr = eh_sub.add_parser("run", parents=[e_cache], help="run the ETH holdout ONCE (the user's approval: --confirm)")
    p_ehr.add_argument("--confirm", action="store_true")
    p_ehr.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p_ep = e_sub.add_parser("paper", help="paper trading of A on ETH (ledger on the paper-aeth branch)")
    ep_sub = p_ep.add_subparsers(dest="ep_cmd", required=True)
    p_epr = ep_sub.add_parser("run", parents=[e_cache], help="one paper-A-ETH run; everything comes from holdout-aeth.lock")
    p_epr.add_argument("--ledger-dir", type=Path, required=True)
    p_eps = ep_sub.add_parser("status", help="print the paper-A-ETH ledger's status (read-only, no network)")
    p_eps.add_argument("--ledger-dir", type=Path, required=True)
    return parser
```

**수정 `src/ewbot/cli.py`** (3/4) — 다음 부분을 찾아:

```python
_COMMANDS = {
    "fetch": cmd_fetch,
```

바꿀 내용:

```python
# ----------------------------------------------------------------------------- A-ETH (Plan 5a)
# ewbot.aeth is imported inside these handlers only: `ewbot live …` and `ewbot paper …` never load it.


def _e_raw(args: argparse.Namespace) -> pd.DataFrame:
    """The cached ETH bars before 2024-01-01 (load_eth filters the rest out while reading)."""
    from ewbot.aeth import config as ec
    from ewbot.aeth.data import cache_path, load_eth

    path = cache_path(args.cache_dir)
    if not path.exists():
        raise SystemExit(f"ETH candle cache not found: {path} - run `ewbot aeth fetch` first")
    raw = load_eth(path, lock_path=ec.LOCK_PATH)
    rep = check_integrity(raw)
    print(f"[data] ETH-USD: {rep.n} 1h bars before {WF_END}, {len(rep.missing_ts)} missing", file=sys.stderr)
    return raw


def _e_fetch(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.data import fetch_eth
    from ewbot.aeth.lockfile import lock_exists

    path = fetch_eth(args.cache_dir, lock_path=ec.LOCK_PATH)
    raw = _e_raw(args)
    first = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(raw["ts"].iloc[0]))) if len(raw) else "-"
    last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(raw["ts"].iloc[-1]))) if len(raw) else "-"
    print(f"ETH cache: {path}\nETH bars before {WF_END}: {len(raw)} ({first} .. {last} UTC)")
    window = "2024+ included (holdout-aeth.lock exists)" if lock_exists(ec.LOCK_PATH) else f"stops at {WF_END} (no lock)"
    print(f"fetch window: {window}")
    return 0


def _e_head_lines(head: dict[str, Any]) -> None:
    _print_comparison({k: v for k, v in head.items() if k != "RANDOM"})
    rnd = head.get("RANDOM") or {}
    sharpe, calmar = (rnd.get("sharpe") or {}).get("mean"), (rnd.get("calmar") or {}).get("mean")
    print(f"random control: {rnd.get('runs')} runs, {rnd.get('n_signals')} entries each; mean Sharpe {sharpe}, "
          f"mean Calmar {calmar}")


def _e_stage1(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.stage1 import write_stage1

    raw = _e_raw(args)
    try:
        rec = write_stage1(raw, out_path=ec.STAGE1_PATH, a_lock_path=ec.A_LOCK_PATH, workers=args.workers)
    except ec.AethError as exc:
        _refuse(str(exc))
    _e_head_lines(rec["headline"])
    print(json.dumps(rec["criteria"], indent=1))
    sub, dear = rec["info"]["sub_period"]["headline"]["A"], rec["info"]["cost_x2"]["A"]
    print(f"information only - A 2019..2023: return {sub['total_return']:.3f}, max dd {sub['max_dd']:.3f}, Sharpe "
          f"{sub['sharpe']:.3f}; A with fees and slippage x 2: return {dear['total_return']:.3f}, Sharpe "
          f"{dear['sharpe']:.3f}")
    verdict = ("PASSED - next: `ewbot aeth holdout lock`" if rec["criteria"]["passed"]
               else "FAILED - A-ETH ends (spec 6): no lock, no 2024 data, no other asset")
    print(f"stage 1 {verdict}. Record: {ec.STAGE1_PATH}. Commit it NOW:\n  git add \"{ec.STAGE1_PATH}\"\n"
          '  git commit -m "chore(aeth): record stage 1"')
    return 0


def _e_holdout_lock(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.holdout import create_lock

    try:
        lock = create_lock(_e_raw(args), now_ts=int(time.time()), lock_path=ec.LOCK_PATH, stage1_path=ec.STAGE1_PATH,
                           a_lock_path=ec.A_LOCK_PATH)
    except ec.AethError as exc:
        _refuse(str(exc))
    print(f"holdout-aeth.lock written: {ec.LOCK_PATH} (sha256 {lock['sha256'][:12]}, params {lock['params']}, last bar "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(lock['last_bar_ts']))} UTC).\n"
          "Commit it NOW, before the holdout runs:\n"
          f'  git add "{ec.LOCK_PATH}"\n  git commit -m "chore(aeth): lock the ETH holdout {lock["sha256"][:12]}"')
    return 0


def _e_holdout_run(args: argparse.Namespace) -> int:
    from ewbot.aeth import config as ec
    from ewbot.aeth.data import fetch_eth
    from ewbot.aeth.holdout import run_holdout_aeth
    from ewbot.aeth.lockfile import lock_exists

    if not args.confirm:
        print("refusing: the ETH holdout runs once, after the user's approval: re-run with --confirm", file=sys.stderr)
        return 2
    if not lock_exists(ec.LOCK_PATH):
        _refuse(f"{ec.LOCK_PATH} is missing or not a valid A-ETH lock: `ewbot aeth holdout lock` first")
    fetch_eth(args.cache_dir, lock_path=ec.LOCK_PATH)
    try:
        out = run_holdout_aeth(confirm=True, cache_dir=args.cache_dir, lock_path=ec.LOCK_PATH,
                               a_lock_path=ec.A_LOCK_PATH, workers=args.workers)
    except ec.AethError as exc:
        _refuse(str(exc))
    fr = out["first_run"]
    _e_head_lines(fr["headline"])
    for line in fr["criteria"]["trade_count"]:
        print(line)
    print(json.dumps(fr["verdict"], indent=1))
    verdict = "PASSED" if fr["verdict"]["passed"] else "FAILED - A-ETH ends (spec 7.3): no paper trading"
    print(f"ETH holdout {verdict}; result in {ec.LOCK_PATH} first_run. Commit it NOW:\n  git add \"{ec.LOCK_PATH}\"\n"
          '  git commit -m "chore(aeth): record the ETH holdout run"')
    return 0


def _e_paper_env(args: argparse.Namespace) -> Any:
    """Production wiring (tests replace this function): real lock, Coinbase, git and Telegram."""
    from ewbot.aeth.paper import AethPaperEnv

    return AethPaperEnv(ledger_dir=args.ledger_dir, cache_dir=args.cache_dir, sender=sender_from_env())


def _e_alert_failure(env: Any, stage: str, message: str) -> None:
    from ewbot.aeth.paper import eth_head

    if env.sender is None:
        return
    text = eth_head(failure_text(stage, message, os.environ.get(RUN_URL_ENV)))
    _, sent, errors = deliver([make_event("FAILURE", env.clock(), stage, text)], [], env.sender, sleep=env.sleep)
    if not sent:
        print(f"failure alert not sent: {errors}", file=sys.stderr)
        return
    marker = os.environ.get(FAILURE_MARKER_ENV)
    if marker:
        Path(marker).write_text(stage, encoding="utf-8")


def _e_paper_run(args: argparse.Namespace) -> int:
    from ewbot.aeth.paper import AethPaperError, run_paper_aeth

    env = _e_paper_env(args)
    try:
        out = run_paper_aeth(env)
    except AethPaperError as exc:
        print(f"paper-A-ETH run failed at stage {exc.stage}: {exc.message}", file=sys.stderr)
        _e_alert_failure(env, exc.stage, exc.message)
        return 1
    except Exception as exc:  # noqa: BLE001 - alert on anything, then fail the step
        traceback.print_exc()
        _e_alert_failure(env, "unexpected", f"{type(exc).__name__}: {exc}")
        return 1
    count = out.status["count"]
    last = time.strftime("%Y-%m-%d %H:%M", time.gmtime(out.last_bar_ts))
    print(f"paper-A-ETH run {out.run_id}: last bar {last} UTC, "
          f"epoch {out.epoch_id}, ETH out-of-sample {count['total']}/{count['required']}, {out.new_decisions} new "
          f"decision(s), {out.new_discrepancies} new discrepancy(ies), {len(out.sent)} sent, {out.pending} pending")
    for note in out.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _e_paper_status(args: argparse.Namespace) -> int:
    from ewbot.aeth.paper import status_aeth, status_lines_aeth

    try:
        led = load_ledger(args.ledger_dir)
    except LedgerError as exc:
        print(f"ledger unreadable: {exc}", file=sys.stderr)
        return 1
    if led is None:
        print(f"no ledger.json in {args.ledger_dir}", file=sys.stderr)
        return 1
    for line in status_lines_aeth(status_aeth(led, int(time.time()))):
        print(line)
    print(position_line(led.open_position))
    cur = current_epoch(led)
    print(f"epoch {cur['id']} of {len(led.epochs)}: fingerprint {cur['fingerprint'][:12]} "
          f"(lock {led.baseline['fingerprint'][:12]})")
    if led.runs:
        r = led.runs[-1]
        print(f"last run {r['id']}: last bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['last_bar_ts']))} UTC, "
              f"holdout data {'ok' if r['holdout_data_ok'] else 'MISMATCH (warned, not refused)'}, "
              f"holdout trades recomputed {r['holdout_trades_recomputed']} (lock: {led.holdout['n_trades']['A']})")
    unexplained = sum(1 for d in led.discrepancies if d["cause"] == "UNEXPLAINED")
    print(f"decisions {len(led.decisions)}, discrepancies {len(led.discrepancies)} (unexplained {unexplained}), "
          f"pending events {len(led.pending_events)}")
    return 0


def cmd_aeth(args: argparse.Namespace) -> int:
    if args.e_cmd == "holdout":
        return _e_holdout_lock(args) if args.eh_cmd == "lock" else _e_holdout_run(args)
    if args.e_cmd == "paper":
        return _e_paper_status(args) if args.ep_cmd == "status" else _e_paper_run(args)
    return {"fetch": _e_fetch, "stage1": _e_stage1}[args.e_cmd](args)


_COMMANDS = {
    "fetch": cmd_fetch,
```

**수정 `src/ewbot/cli.py`** (4/4) — 다음 부분을 찾아:

```python
    "stratb": cmd_stratb,
}
```

바꿀 내용:

```python
    "stratb": cmd_stratb,
    "aeth": cmd_aeth,
}
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/aeth/test_cli_aeth.py -q
```

예상: `12 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/cli.py tests/aeth/test_cli_aeth.py
git commit -m "feat(cli): ewbot aeth fetch | stage1 | holdout lock | holdout run | paper run | paper status (ewbot.aeth loaded only there)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `paper-aeth` 동기화, 워크플로, CI 검사, 문서

**Files:**
- Create: `src/ewbot/aeth/sync.py`
- Create: `.github/workflows/paper-aeth.yml`
- Create: `docs/aeth.md`
- Modify: `README.md`
- Modify: `tests/paper/test_static.py` (워크플로 목록, CI 검사 1개)
- Test: `tests/aeth/test_sync.py`
- Test: `tests/aeth/test_workflow.py`

**Interfaces:**
- Consumes: Task 5·6: `uv run ewbot aeth paper run --ledger-dir "$PAPER_AETH_DIR"`, 접두어 `[ewbot paper-A-ETH]`, 환경변수 `EWBOT_RUN_URL`·`EWBOT_FAILURE_MARKER`(A·B와 같은 이름, A의 `cli.py`가 읽음).
- Produces: `ewbot.aeth.sync`: `BRANCH = "paper-aeth"`, `LEDGER_FILES = ("ledger.json", "runs.log")`, `ATTEMPTS = 3`, `class SyncError`, `origin_branch(cwd) -> str`, `checkout(repo, paper_dir) -> None`, `commit_ledger(paper_dir) -> bool`, `push(paper_dir, recompute, *, attempts=3, backoff=5.0, sleep=time.sleep) -> str`, `main(argv=None) -> int`(`python -m ewbot.aeth.sync checkout | push RECOMPUTE...`, 환경변수 `PAPER_AETH_DIR`, `PAPER_AETH_PUSH_BACKOFF`). `.github/workflows/paper-aeth.yml`(해석 13). `docs/aeth.md`(운영 문서, 두 번째 Worker 코드 포함). README의 A-ETH 줄.

git 동기화는 A의 `scripts/paper_sync.sh`·B의 `paperb/sync.py`와 같은 규칙이고(브랜치가 없을 때만 orphan으로 시작, 원격이 그대로면 같은 커밋을 다시 푸시, 원격이 움직였을 때만 다시 계산), 브랜치 이름과 환경변수만 A-ETH의 것이다. 워크플로는 `paper-b.yml`과 같은 모양으로 휴면이며(`PAPER_AETH_ENABLED`), 시크릿은 실행·푸시·실패 알림 단계에만 간다. `tests/paper/test_static.py`의 워크플로 목록 테스트에 `paper-aeth.yml`을 더하고(이름도 바꿈), `paper-aeth.yml`이 A를 건드리지 않는다는 CI 검사를 B의 같은 검사 옆에 둔다.

- [ ] **Step 1: 실패하는 테스트 작성**

동기화 테스트는 로컬 bare 저장소(원격에 A의 `paper`와 B의 `paper-b`가 있음)로 orphan 시작과 이후 추적, `paper`·`paper-b` 불변, 원격을 읽을 수 없을 때 새 브랜치를 만들지 않음, 바뀐 것이 없을 때, 원격이 움직였을 때 한 번 다시 계산, 푸시 실패 재시도, `main`의 환경변수와 명령을 확인한다(git 2.42 미만이면 건너뜀). 워크플로 테스트는 트리거·동시성·권한·실행기·휴면, 단계(고정 Python 3.12.13), 시크릿 범위와 접두어, A·B 이름이 없음을 확인한다.

**파일 `tests/aeth/test_sync.py`** (새 파일, 전체 내용)

```python
"""ewbot.aeth.sync against local bare repositories (no network): a bare origin that already has strategy A's
`paper` and strategy B's `paper-b` branches, the workflow's master checkout, the paper-aeth worktree and another
writer."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from ewbot.aeth import sync
from ewbot.aeth.sync import BRANCH, SyncError, checkout, commit_ledger, push


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
    for branch in ("paper", "paper-b"):  # strategy A's and strategy B's ledger branches exist on origin
        g(work, "switch", "-q", "--orphan", branch)
        (work / "ledger.json").write_text(f'{{"{branch}": 1}}\n')
        g(work, "add", "ledger.json")
        g(work, "commit", "-q", "-m", f"{branch} ledger")
        g(work, "push", "-q", "origin", branch)
    g(work, "switch", "-q", "master")
    g(tmp_path, "clone", "-q", str(origin), str(other))
    return {"origin": origin, "work": work, "other": other, "paper": tmp_path / "rt" / "paper-aeth", "tmp": tmp_path}


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


def test_checkout_starts_an_orphan_paper_aeth_then_tracks_it_and_leaves_paper_and_paper_b_alone(world):
    before = {b: g(world["origin"], "rev-parse", f"refs/heads/{b}") for b in ("paper", "paper-b")}
    assert BRANCH == "paper-aeth"
    checkout(world["work"], world["paper"])
    assert g(world["paper"], "branch", "--show-current") == BRANCH
    _write(world["paper"], 1)
    assert push(world["paper"], lambda: 0, backoff=0).startswith("pushed")
    files = g(world["origin"], "ls-tree", "--name-only", f"refs/heads/{BRANCH}").split()
    assert sorted(files) == ["ledger.json", "runs.log"]
    assert {b: g(world["origin"], "rev-parse", f"refs/heads/{b}") for b in ("paper", "paper-b")} == before
    g(world["work"], "worktree", "remove", "--force", str(world["paper"]))
    checkout(world["work"], world["paper"])
    assert (world["paper"] / "runs.log").read_text() == "run 1\n"


def test_an_unreadable_origin_never_starts_a_new_branch(world):
    g(world["work"], "remote", "set-url", "origin", str(world["tmp"] / "missing.git"))
    with pytest.raises(SyncError, match="not starting a new paper-aeth"):
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

    assert push(world["paper"], recompute, backoff=0).startswith("pushed") and len(calls) == 1
    assert g(world["origin"], "show", f"refs/heads/{BRANCH}:runs.log").split("\n") == ["run 1", "run 99", "run 3"]


def test_failed_pushes_retry_the_same_commit_and_give_up_after_three(world):
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


def test_main_needs_paper_aeth_dir_and_a_known_command(world, monkeypatch, capsys):
    monkeypatch.delenv("PAPER_AETH_DIR", raising=False)
    assert sync.main(["checkout"]) == 1 and "PAPER_AETH_DIR is not set" in capsys.readouterr().err
    monkeypatch.setenv("PAPER_AETH_DIR", str(world["paper"]))
    monkeypatch.chdir(world["work"])
    assert sync.main(["checkout"]) == 0
    _write(world["paper"], 1)
    monkeypatch.setenv("PAPER_AETH_PUSH_BACKOFF", "0")
    assert sync.main(["push", sys.executable, "-c", "pass"]) == 0
    assert sync.main(["status"]) == 1
```

**파일 `tests/aeth/test_workflow.py`** (새 파일, 전체 내용)

```python
"""Static checks of .github/workflows/paper-aeth.yml: the shape of strategy A's paper.yml, nothing of A's or B's."""

from __future__ import annotations

from pathlib import Path

import yaml

import ewbot

ROOT = Path(ewbot.__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "paper-aeth.yml"
SETUP_UV = "astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7"
SECRETS = {"TELEGRAM_BOT_TOKEN": "${{ secrets.TELEGRAM_BOT_TOKEN }}", "TELEGRAM_CHAT_ID": "${{ secrets.TELEGRAM_CHAT_ID }}"}
RUN = 'uv run ewbot aeth paper run --ledger-dir "$PAPER_AETH_DIR"'


def _wf():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return wf, wf["jobs"]["paper-aeth"]["steps"]


def _step(steps, step_id):
    return next(s for s in steps if s.get("id") == step_id)


def test_triggers_concurrency_permissions_runner_and_dormancy():
    wf, _ = _wf()
    assert wf[True] == {"schedule": [{"cron": "42 */3 * * *"}], "workflow_dispatch": None}
    assert wf["concurrency"] == {"group": "paper-aeth", "cancel-in-progress": False}
    job = wf["jobs"]["paper-aeth"]
    assert wf["permissions"] == {"contents": "write"} and job["runs-on"] == "ubuntu-24.04"
    assert job["if"] == "vars.PAPER_AETH_ENABLED == 'true'"  # dormant until a passed ETH holdout


def test_steps_run_the_aeth_runtime_and_its_sync_on_the_pinned_python():
    wf, steps = _wf()
    assert [s["uses"] for s in steps if "uses" in s] == ["actions/checkout@v7", SETUP_UV, "actions/cache@v6"]
    assert steps[0]["with"] == {"ref": "master"}
    assert next(s for s in steps if s.get("uses") == SETUP_UV)["with"]["python-version"] == "3.12.13"
    assert _step(steps, "paper-aeth")["run"] == RUN
    assert _step(steps, "push")["run"] == f"uv run python -m ewbot.aeth.sync push {RUN}"
    assert "uv run python -m ewbot.aeth.sync checkout" in _step(steps, "checkout-paper-aeth")["run"]
    cache = next(s for s in steps if s.get("uses") == "actions/cache@v6")["with"]
    assert cache["path"] == "data/cache" and cache["restore-keys"].strip() == "eth-usd-1h-"
    budget = sum(s.get("timeout-minutes", 0) for s in steps)
    assert wf["jobs"]["paper-aeth"]["timeout-minutes"] >= budget + 10


def test_secrets_reach_only_the_steps_that_send_and_the_alert_has_the_a_eth_prefix():
    _, steps = _wf()
    with_secrets = [s.get("id") for s in steps if "secrets." in yaml.safe_dump(s)]
    assert with_secrets == ["paper-aeth", "push", "alert"]
    for i in with_secrets:
        assert {k: v for k, v in _step(steps, i)["env"].items() if "secrets." in v} == SECRETS
    alert = _step(steps, "alert")
    assert alert["if"] == "failure() || cancelled()" and "[ewbot paper-A-ETH] 실패" in alert["run"]


def test_nothing_of_strategy_a_or_b_is_referenced():
    text = WORKFLOW.read_text(encoding="utf-8")
    for other in ("paper_sync.sh", "btc-usd-1h", "stratb-15m", "ewbot paper run", "ewbot stratb", "paperb",
                  "group: paper\n", "group: paper-b\n", "holdout.lock", "holdout-b.lock", "PAPER_B_ENABLED"):
        assert other not in text
```

**수정 `tests/paper/test_static.py`** (1/2) — 다음 부분을 찾아:

```python
def test_the_repository_has_exactly_the_paper_ci_and_paper_b_workflows():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper-b.yml", "paper.yml"]
```

바꿀 내용:

```python
def test_the_repository_has_exactly_the_paper_ci_paper_b_and_paper_aeth_workflows():
    assert sorted(p.name for p in WORKFLOWS.iterdir()) == ["ci.yml", "paper-aeth.yml", "paper-b.yml", "paper.yml"]
```

**수정 `tests/paper/test_static.py`** (2/2) — 다음 부분을 찾아:

```python
def test_workflow_triggers_concurrency_and_permissions():
```

바꿀 내용:

```python
def test_the_paper_aeth_workflow_never_touches_strategy_a():
    text = (WORKFLOWS / "paper-aeth.yml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["concurrency"]["group"] == "paper-aeth"
    for a_thing in ("paper_sync.sh", "btc-usd-1h", "ewbot paper run", "holdout.lock"):
        assert a_thing not in text


def test_workflow_triggers_concurrency_and_permissions():
```

- [ ] **Step 2: 테스트를 실행해 실패를 확인**

```bash
uv run --group live pytest tests/aeth/test_sync.py tests/aeth/test_workflow.py tests/paper/test_static.py -q
```

예상: `cannot import name 'sync' from 'ewbot.aeth'`
예상: `1 error`

- [ ] **Step 3: 최소 구현**

**파일 `src/ewbot/aeth/sync.py`** (새 파일, 전체 내용)

```python
"""Git side of .github/workflows/paper-aeth.yml (Plan 5a spec 8): strategy A's scripts/paper_sync.sh rules for the
orphan branch `paper-aeth` (A's script and its `paper` branch, and strategy B's `paper-b`, are never touched). Run
from the master checkout:

  python -m ewbot.aeth.sync checkout
      worktree of origin's paper-aeth at $PAPER_AETH_DIR (shallow), or an orphan paper-aeth when origin has none
      (ls-remote exit 2); any other ls-remote failure fails: a network or auth error must not start a fresh ledger.
  python -m ewbot.aeth.sync push RECOMPUTE...
      commit ledger.json and runs.log in $PAPER_AETH_DIR and push to origin's paper-aeth, 3 pushes at most. After a
      failed push: origin unmoved or unreadable -> the same commit again after a wait (no recompute, so no Telegram
      message twice); origin already at our commit -> done; origin moved -> reset to it, run RECOMPUTE
      (`uv run ewbot aeth paper run ...`), commit and push on top. Fails when every push failed.
Environment: PAPER_AETH_DIR (required); PAPER_AETH_PUSH_BACKOFF = seconds, retry n waits n times it (default 5)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

BRANCH = "paper-aeth"
LEDGER_FILES = ("ledger.json", "runs.log")
ATTEMPTS = 3


class SyncError(RuntimeError):
    """A git step of the paper-aeth sync failed."""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def origin_branch(cwd: Path) -> str:
    """The sha of origin's paper-aeth, "" when origin has none; SyncError on any other ls-remote failure."""
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
    _git(paper_dir, "commit", "-q", "-m", f"paper-aeth: run {datetime.now(UTC).strftime('%Y-%m-%dT%H:%MZ')}")
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
    paper_dir = os.environ.get("PAPER_AETH_DIR", "")
    try:
        if not paper_dir:
            raise SyncError("PAPER_AETH_DIR is not set")
        if args == ["checkout"]:
            checkout(Path.cwd(), Path(paper_dir))
            return 0
        if len(args) > 1 and args[0] == "push":
            backoff = float(os.environ.get("PAPER_AETH_PUSH_BACKOFF", "5"))
            print(push(Path(paper_dir), lambda: subprocess.run(args[1:]).returncode, backoff=backoff))
            return 0
    except SyncError as exc:
        print(f"paper-aeth sync: {exc}", file=sys.stderr)
        return 1
    print("usage: python -m ewbot.aeth.sync checkout | push RECOMPUTE...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

**파일 `.github/workflows/paper-aeth.yml`** (새 파일, 전체 내용)

```yaml
# ewbot paper trading of A-ETH (Plan 5a spec 8): strategy A, frozen, on ETH - one deterministic recompute per run.
# Code comes from master; ledger.json / runs.log live on the orphan `paper-aeth` branch, which only this workflow
# writes (docs/aeth.md: never push to it by hand). Strategy A's workflow, branch, sync script and candle cache key
# and strategy B's are never touched. The git side (paper-aeth worktree, commit, push and retry) is
# `python -m ewbot.aeth.sync`.
name: paper-aeth

# Primary trigger: a second Cloudflare Workers cron calls workflow_dispatch at minute 42 of every hour
# (docs/aeth.md, user step). `schedule` is only a sparse backup (minute 42, every 3 hours).
on:
  schedule:
    - cron: "42 */3 * * *"
  workflow_dispatch:

concurrency:
  group: paper-aeth
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  paper-aeth:
    # Dormant until the user sets the repository variable PAPER_AETH_ENABLED=true after a passed ETH holdout
    # (docs/aeth.md): a skipped job uses no runner minutes and sends nothing.
    if: vars.PAPER_AETH_ENABLED == 'true'
    # pinned like strategy A's paper workflow: the runner image is part of each run's recorded environment
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

      - name: Check out the paper-aeth branch (orphan on first use)
        id: checkout-paper-aeth
        timeout-minutes: 5
        run: |
          set -euo pipefail
          echo "PAPER_AETH_DIR=$RUNNER_TEMP/paper-aeth" >> "$GITHUB_ENV"
          echo "EWBOT_FAILURE_MARKER=$RUNNER_TEMP/ewbot-aeth-failure-notified" >> "$GITHUB_ENV"
          git config --global user.name "ewbot-paper-aeth"
          git config --global user.email "ewbot-paper-aeth@users.noreply.github.com"
          PAPER_AETH_DIR="$RUNNER_TEMP/paper-aeth" uv run python -m ewbot.aeth.sync checkout

      - name: Restore the ETH 1h candle cache
        uses: actions/cache@v6
        with:
          path: data/cache
          key: eth-usd-1h-${{ github.run_id }}-${{ github.run_attempt }}
          restore-keys: |
            eth-usd-1h-

      - name: Paper-A-ETH run
        id: paper-aeth
        timeout-minutes: 15
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run ewbot aeth paper run --ledger-dir "$PAPER_AETH_DIR"

      - name: Commit and push the ledger
        id: push
        timeout-minutes: 15
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: uv run python -m ewbot.aeth.sync push uv run ewbot aeth paper run --ledger-dir "$PAPER_AETH_DIR"

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
          text="[ewbot paper-A-ETH] 실패 · 단계 workflow (checkout / setup / sync / timeout / push) · ${JOB_STATUS}"$'\n'"로그: ${EWBOT_RUN_URL}"
          curl -sS --fail-with-body --retry 3 --max-time 20 -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${text}"
          echo
```

**파일 `docs/aeth.md`** (새 파일, 전체 내용)

````markdown
# A-ETH (Plan 5a) — strategy A, frozen, on ETH: stage 1, one-shot holdout, paper trading

Spec: `docs/superpowers/specs/2026-10-07-ewbot-plan5a-aeth-design.md`. Plan: `docs/superpowers/plans/2026-10-07-ewbot-plan5a-aeth.md`.

A-ETH runs strategy A exactly as frozen — the params of the committed `holdout.lock` (K4 = 2.0, Q_MIN = 0.7, S_MIN = 0.5), the conservative profile, A's breakers, A's code — on Coinbase `ETH-USD` 1h candles, with A's costs and the contract size of `ETP-20DEC30-CDE` (0.1 ETH). Nothing is tuned and no other asset is tried. It is a separate research and paper track: strategy A's fingerprint (`e0435223…`), `holdout.lock`, BTC 50-trade count, `paper.yml` and live runtime are never touched, and ETH trades are never pooled into A-BTC's count.

Three stages, one attempt each; a failure ends A-ETH:

1. **Stage 1** (2017-01-01 .. 2023-12-31, no 2024 data): A's `compare()` on ETH. Pass = A's net total return > 0, A's max drawdown ≤ 15%, A's Sharpe and Calmar each above C's and above the random-entry control's mean. The 2019–2023 sub-period and fees and slippage × 2 are reported for information only.
2. **Stage 2** (only after a passed stage 1): `holdout-aeth.lock`, then the ETH holdout from 2024-01-01 to the lock's last bar, run once after your approval. Pass = strategy A's `evaluate_criteria`, unchanged: A is a winner (Sharpe and Calmar above C, buy & hold and the random control's mean) and A's max drawdown ≤ 15% (A's go-live items 1 and 2). The trade count is recorded.
3. **Stage 3** (only after a passed stage 2): hourly paper trading of A on ETH (`paper-aeth.yml`, ledger on the orphan branch `paper-aeth`).

## Isolation from strategy A (and B)

- A-ETH's code is `src/ewbot/aeth/` (its own fingerprint: every `*.py` there). A's fingerprinted folders, `backtest/`, `data/coinbase_history.py`, `paper/`, `live/`, `stratb/`, `paperb/`, `holdout.lock`, `paper.yml`, `ci.yml`, `paper-b.yml`, `uv.lock` and `pyproject.toml` are never changed; A-ETH only imports A's modules.
- `ewbot.aeth` is imported only inside the `ewbot aeth …` handlers of `cli.py`: `ewbot live …` and `ewbot paper …` load the same modules as before.
- A-ETH's own artifacts: `data/cache/ETH-USD_1h.parquet`, `aeth-stage1.json`, `holdout-aeth.lock`, the `paper-aeth` branch, `.github/workflows/paper-aeth.yml`, the Actions cache key `eth-usd-1h-` and the Telegram prefix `[ewbot paper-A-ETH]`.
- `tests/aeth/test_isolation.py` checks A's fingerprint, that `aeth/` is outside A's fingerprinted folders, that a branch touching A-ETH touches no strategy-A or strategy-B path, and that only the holdout and the paper runtime read 2024+ candles; `tests/paper/test_static.py` (run by `ci.yml`) carries the CI part.
- **Never run strategy A's `ewbot fetch --product ETH-USD`**: it writes the same cache file up to now. Before the lock, A-ETH refuses a cache that holds 2024+ bars (delete the file and run `ewbot aeth fetch` again).

## Commands

```bash
uv run ewbot aeth fetch                    # ETH-USD 1h cache from 2017-01-01; stops at 2024-01-01 until holdout-aeth.lock exists
uv run ewbot aeth stage1                   # stage 1, ONCE -> aeth-stage1.json (commit it at once)
uv run ewbot aeth holdout lock             # holdout-aeth.lock from the committed, passed aeth-stage1.json (commit it at once)
uv run ewbot aeth holdout run --confirm    # ONCE, after your approval (commit the lock at once)
uv run ewbot aeth paper status --ledger-dir <paper-aeth worktree>   # read-only
```

There is no option for params, product, costs, windows or the random-control size: they are A's, fixed in code and in the lock.

## Stage 1 (controller; network: ETH before 2024 only)

1. On a clean working tree of `master` (`git status --porcelain --untracked-files=no` prints nothing): `uv run ewbot aeth fetch` (about 205 public Coinbase requests, a minute or two; an interrupted download resumes). Check the printed bar count (about 61,000) and `fetch window: stops at 2024-01-01 (no lock)`.
2. `uv run ewbot aeth stage1` (a few minutes). It refuses when `aeth-stage1.json` exists (one attempt), when tracked files have uncommitted changes, when A's fingerprint is not `e0435223…`, and when the bars do not cover 2017-01-01 .. 2023-12-31. Commit `aeth-stage1.json` right away, whatever the result.
3. If stage 1 failed, A-ETH ends here: no lock, no 2024 data, A stays BTC-only, no other asset is tried. Record the result in the spec's end record.

## Stage 2 (controller with your approval)

1. On a clean tree: `uv run ewbot aeth holdout lock`, then commit `holdout-aeth.lock` right away. The lock is refused unless `aeth-stage1.json` is committed unchanged, passed, and was made with A's params (and `holdout.lock` sha256), this A-ETH code, A's frozen fingerprint and the same pre-2024 bars. The lock fixes the product and contract, params, config, holdout start, its last bar (the last complete hour now), the 100-run random control, A's fingerprint, the A-ETH fingerprint, `code_sha256` (every `src/ewbot` `*.py`, like A's lock), the pre-2024 bars and the stage-1 record's sha256.
2. Approval: the user sees the lock and the pass rule and approves running the holdout once.
3. Before the run: the tree is clean, `HEAD` is the lock commit, and the protection tests pass (`uv run --group live pytest tests/paper/test_fingerprint.py tests/paper/test_static.py tests/aeth/test_isolation.py -q`). Then `uv run ewbot aeth holdout run --confirm` (it first extends the cache to now: 2024+ bars are fetched only now) and commit `holdout-aeth.lock` **immediately** (it holds `first_run`). A second run, a changed A or A-ETH or `src/ewbot` file, a lock that is not this code's, other pre-2024 bars or uncommitted changes are refused. Keep `data/cache/ETH-USD_1h.parquet` until paper trading has reproduced the holdout (`holdout data ok` in `ewbot aeth paper status`).
4. If `first_run.verdict.passed` is false, A-ETH ends: no paper trading.

## Paper trading (only after a passed ETH holdout; you)

What runs: `.github/workflows/paper-aeth.yml` recomputes ETH from 2017 with the committed `holdout-aeth.lock`, trading from 2024-01-01, so it reproduces the holdout and continues it. Decisions go to `ledger.json` and `runs.log` on the orphan branch `paper-aeth` (only the workflow writes it). Paper trades are the trades entering after the lock's last bar. The ETH count is the holdout's A trades plus the paper trades, 50 needed, on this ledger only. A change of strategy A's fingerprint or of the A-ETH fingerprint opens a new epoch and restarts the count. There is no `report.html` and no daily criteria 1–2 recompute (Plan 5c judges them once on the whole ETH out-of-sample window).

One-time setup:

1. `master` has the committed, passed `holdout-aeth.lock`. Until step 2 the workflow is dormant (its job only runs when the repository variable `PAPER_AETH_ENABLED` is `true`).
2. `gh variable set PAPER_AETH_ENABLED --body true` (to stop it: `gh variable delete PAPER_AETH_ENABLED`).
3. Hourly trigger: a second Cloudflare Worker (A's worker always dispatches `paper.yml`). **Workers & Pages** → **Create Worker**, name `ewbot-paper-aeth-trigger`, paste the code below, deploy; secret `GITHUB_TOKEN` (the same fine-grained token as A's worker: this repository, Actions read and write); cron trigger `42 * * * *`.

   ```js
   const DISPATCH_URL =
     "https://api.github.com/repos/taejunoh/elliott-wave-btc-bot/actions/workflows/paper-aeth.yml/dispatches";

   export default {
     async scheduled(event, env, ctx) {
       const response = await fetch(DISPATCH_URL, {
         method: "POST",
         headers: {
           Authorization: `Bearer ${env.GITHUB_TOKEN}`,
           Accept: "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "ewbot-paper-aeth-trigger",
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

   The workflow's own `schedule` (`42 */3 * * *`) is only a backup.
4. First run by hand: `gh workflow run paper-aeth.yml --ref master`, then `gh run watch`. Check `[ewbot paper-A-ETH]` messages and, in a `paper-aeth` worktree, `uv run ewbot aeth paper status --ledger-dir <dir>`: `holdout data ok` and `holdout trades recomputed` equal to the lock's.
5. Actions usage: A-ETH adds one or two billed minutes per run, 24 hourly and 8 backup runs a day (roughly 1,000–2,000 minutes a month), on top of strategy A's paper runs, which are already near the 2,000 free minutes. Keep the small Actions budget of `docs/paper-trading.md`; do not thin the hourly trigger (criterion 4 counts hourly coverage).

## Failures

- `[ewbot paper-A-ETH] 실패 · 단계 <stage>`: `precheck` (lock missing, not committed, edited, holdout not run or failed, or not this code's lock for A's params), `data` (the candle update failed three times, or the data ends before the lock's last bar), or another stage. Nothing is committed after a failed run.
- `경고: 홀드아웃 구간 데이터 해시 불일치` / `홀드아웃 재현 불일치`: the holdout window's bars changed (a Coinbase revision or a re-download); warned once, never refused; affected decisions are `DATA_REVISION`.
- Discrepancies are classified like A's: `DATA_REVISION`, `FINGERPRINT_CHANGE`, `ENVIRONMENT_CHANGE`, `UNEXPLAINED`.

## Toward live ETH (Plan 5c, never automatic)

Plan 5c starts only after A-BTC's CANARY has passed and A-ETH's stage 3 is met: the passed ETH holdout, at least 50 ETH out-of-sample trades (holdout + paper), at least 28 days of paper with 0 `UNEXPLAINED` discrepancies and ≥ 95% hourly coverage, A's go-live items 1–2 re-judged on the whole ETH out-of-sample window, and your approval. The live runtime trades one product today; account-level breakers and margin are shared with A-BTC; any live code change restarts SHADOW. Confirm ETP liquidity and slippage, the per-contract minimum fee and ETH funding first.
````

**수정 `README.md`** (1/5) — 다음 부분을 찾아:

```markdown
| **Strategy B** (Plan 4) | A separate 15m strategy on BTC and ETH (1h swing structure, Fibonacci zone with an earlier swing, 15m RSI divergence and breakout). Its own walk-forward, one-shot holdout (`holdout-b.lock`) and paper trading (`paper-b` branch). Never touches A. | Your laptop, then GitHub Actions (`paper-b.yml`) | Research ([`docs/strategy-b.md`](docs/strategy-b.md)) |
```

바꿀 내용:

```markdown
| **Strategy B** (Plan 4) | A separate 15m strategy on BTC and ETH (1h swing structure, Fibonacci zone with an earlier swing, 15m RSI divergence and breakout). Its own walk-forward, one-shot holdout (`holdout-b.lock`) and paper trading (`paper-b` branch). Never touches A. | Your laptop, then GitHub Actions (`paper-b.yml`) | Research ([`docs/strategy-b.md`](docs/strategy-b.md)) |
| **A-ETH** (Plan 5a) | Strategy A, frozen (same params, code and costs except the 0.1 ETH contract), on ETH-USD: a pre-registered stage 1 on 2017–2023, a one-shot ETH holdout (`holdout-aeth.lock`) and paper trading (`paper-aeth` branch) with its own 50-trade count. Never touches A. | Your laptop, then GitHub Actions (`paper-aeth.yml`) | Stage 1 pending ([`docs/aeth.md`](docs/aeth.md)) |
```

**수정 `README.md`** (2/5) — 다음 부분을 찾아:

```markdown
- **Strategy B never touches A:** its code stays in `src/ewbot/stratb/` and `src/ewbot/paperb/`, and any change in `stratb/` restarts B's paper count (B fingerprint), never A's.
```

바꿀 내용:

```markdown
- **Strategy B never touches A:** its code stays in `src/ewbot/stratb/` and `src/ewbot/paperb/`, and any change in `stratb/` restarts B's paper count (B fingerprint), never A's.
- **A-ETH never touches A:** its code stays in `src/ewbot/aeth/`; stage 1 and the ETH holdout run once each, and ETH trades are never pooled into A-BTC's 50. Never run `ewbot fetch --product ETH-USD` (it would download 2024+ ETH bars into A-ETH's cache).
```

**수정 `README.md`** (3/5) — 다음 부분을 찾아:

```markdown
  - [`docs/strategy-b.md`](docs/strategy-b.md): strategy B research, holdout and paper trading.
```

바꿀 내용:

```markdown
  - [`docs/strategy-b.md`](docs/strategy-b.md): strategy B research, holdout and paper trading.
  - [`docs/aeth.md`](docs/aeth.md): A-ETH stage 1, ETH holdout and paper trading.
```

**수정 `README.md`** (4/5) — 다음 부분을 찾아:

```markdown
  - [Plan 4: strategy B](docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md)
```

바꿀 내용:

```markdown
  - [Plan 4: strategy B](docs/superpowers/specs/2026-10-07-ewbot-plan4-strategy-b-design.md)
  - [Plan 5a: A-ETH](docs/superpowers/specs/2026-10-07-ewbot-plan5a-aeth-design.md)
```

**수정 `README.md`** (5/5) — 다음 부분을 찾아:

```markdown
src/ewbot/paperb/     strategy B paper trading (Plan 4): runner, ledger adapters, paper-b git sync
```

바꿀 내용:

```markdown
src/ewbot/paperb/     strategy B paper trading (Plan 4): runner, ledger adapters, paper-b git sync
src/ewbot/aeth/       A-ETH (Plan 5a): strategy A on ETH - data guard, stage 1, ETH holdout, paper-aeth runtime and sync
```

- [ ] **Step 4: 테스트를 실행해 통과를 확인**

```bash
uv run --group live pytest tests/aeth/test_sync.py tests/aeth/test_workflow.py tests/paper/test_static.py -q
```

예상: `39 passed`

`tests/aeth/test_sync.py` 6개, `tests/aeth/test_workflow.py` 4개, `tests/paper/test_static.py` 29개다.

- [ ] **Step 5: 커밋**

```bash
git add src/ewbot/aeth/sync.py .github/workflows/paper-aeth.yml docs/aeth.md README.md tests/aeth/test_sync.py tests/aeth/test_workflow.py tests/paper/test_static.py
git commit -m "feat(aeth): paper-aeth workflow (dormant) and git sync, CI guard for strategy A, A-ETH ops guide" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: 최종 검증 (전체 테스트, A 지문, A 보호, 바뀐 파일)

**Files:** 없음. 이 작업은 코드를 바꾸지 않고, 병합 전에 확인만 한다.

**Interfaces:**
- Consumes: Task 1~7의 결과 전체.
- Produces: 병합 가능 여부의 판단 근거(전체 테스트 수, A 지문 값, 바뀐 파일 목록).

- [ ] **Step 1: 전체 테스트**

```bash
uv run --group live pytest -q
```

예상: `1502 passed` 또는 `1501 passed, 1 skipped`

`data/cache/BTC-USD_1h.parquet`가 없으면 `tests/backtest/test_e2e_real.py` 1개가 건너뛰어진다. 기준선보다 정확히 121개가 많아야 한다.

- [ ] **Step 2: A 지문이 그대로인지 확인**

```bash
uv run python -c "from ewbot.paper.fingerprint import current_fingerprint; print(current_fingerprint())"
```

예상: `e0435223a3f9f1ef69c79b54541958f9634989b86728da02332f21d298bac5f1`

- [ ] **Step 3: A 보호 검사를 건너뜀 없이 실행**

```bash
uv run --group live pytest tests/aeth/test_isolation.py tests/paper/test_fingerprint.py tests/paper/test_static.py tests/stratb/test_isolation.py -q -rs
```

예상: `59 passed`

출력에 `SKIPPED`가 없어야 한다: `master`가 있는 작업 트리에서는 `test_aeth_work_changes_no_protected_path`(merge-base 이후 A-ETH를 바꾼 이 브랜치가 A·B 경로를 바꾸지 않음)와 `test_baseline_recomputes_from_git_and_equals_the_working_tree`(holdout 커밋 `64e628a`에서 다시 계산한 A 지문)가 실제로 돈다.

- [ ] **Step 4: `master` 대비 바뀐 파일이 허용 목록뿐인지 확인**

```bash
git diff --name-only "$(git merge-base HEAD master)" -- . ':(exclude)docs/superpowers'
```

예상 출력(정확히 이 목록):

```text
.github/workflows/paper-aeth.yml
README.md
docs/aeth.md
src/ewbot/aeth/__init__.py
src/ewbot/aeth/config.py
src/ewbot/aeth/data.py
src/ewbot/aeth/fingerprint.py
src/ewbot/aeth/holdout.py
src/ewbot/aeth/lockfile.py
src/ewbot/aeth/paper.py
src/ewbot/aeth/stage1.py
src/ewbot/aeth/sync.py
src/ewbot/cli.py
tests/aeth/__init__.py
tests/aeth/helpers.py
tests/aeth/records.py
tests/aeth/test_cli_aeth.py
tests/aeth/test_config.py
tests/aeth/test_data.py
tests/aeth/test_fingerprint.py
tests/aeth/test_holdout.py
tests/aeth/test_isolation.py
tests/aeth/test_paper.py
tests/aeth/test_stage1.py
tests/aeth/test_sync.py
tests/aeth/test_workflow.py
tests/paper/test_static.py
```

목록에 다른 경로(A의 지문 대상, `backtest/`, `data/`, `paper/`, `live/`, `stratb/`, `paperb/`, `holdout.lock`, `paper.yml`, `ci.yml`, `paper-b.yml`, `scripts/`, `ops/`, `deploy/`, `uv.lock`, `pyproject.toml`)가 하나라도 있으면 병합하지 않는다.

- [ ] **Step 5: 작업 트리가 깨끗한지 확인**

```bash
git status --porcelain
```

예상: 출력 없음.

---

### Task 9: 수동 절차 — PR·병합, 그리고 단계 1 (컨트롤러 전용, 네트워크: 2024 이전 ETH만 — 서브에이전트 금지)

이 작업은 처음으로 ETH 1시간봉을 받고, 결과가 영구 기록으로 남는 1회 실행을 포함한다. 사전 등록(명세 2절)상 Step 3의 결과를 본 뒤에는 코드, 설정, 기준을 고치지 않는다.

**Files:** `aeth-stage1.json`(새 영구 기록, 커밋), `data/cache/ETH-USD_1h.parquet`(gitignore). 탈락이면 명세 14절과 `docs/aeth.md`·`README.md`의 상태 줄.

**Interfaces:**
- Consumes: Task 6의 `ewbot aeth fetch`, `ewbot aeth stage1`.
- Produces: 커밋된 `aeth-stage1.json`과 사용자 보고. Task 10을 진행할지의 근거(`criteria.passed`).

- [ ] **Step 1: PR과 병합(사용자)**

```bash
git push -u origin feat/aeth
gh pr create --base master --title "Plan 5a: A-ETH - strategy A, frozen, on ETH (stage 1, one-shot holdout, paper-aeth)" --body-file - <<'BODY'
A-ETH (Plan 5a): strategy A exactly as frozen on ETH-USD, a separate research and paper track. ETH data guard (no 2024+ bar before holdout-aeth.lock), one-shot stage 1 (2017-2023), holdout-aeth.lock and the one-shot ETH holdout judged by A's evaluate_criteria, paper-aeth runtime and workflow (dormant until PAPER_AETH_ENABLED). Pre-registered: the spec and plan were committed before any ETH 1h data was downloaded. Strategy A's fingerprint e0435223… is unchanged; no strategy-A or strategy-B file, workflow, lock or ledger is modified.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
```

`ci` 체크(A 지문·정적 검사)가 통과해야 사용자가 병합한다. `paper-aeth.yml`은 병합되어도 `PAPER_AETH_ENABLED`가 없어 돌지 않는다.

- [ ] **Step 2: 기록 브랜치와 2024 이전 ETH 1시간봉 수집(네트워크)**

```bash
git switch master && git pull --ff-only && git switch -c chore/aeth-validation
test ! -e holdout-aeth.lock && test ! -e aeth-stage1.json && echo "no A-ETH record yet"
uv run ewbot aeth fetch
```

약 205번의 공개 Coinbase 요청(1~2분; 중단되면 다시 실행해 이어 받는다). 출력에서 `ETH bars before 2024-01-01: …`(약 61,000개, `2017-01-01 00:00 .. 2023-12-31 23:00 UTC`에 가까워야 함)와 `fetch window: stops at 2024-01-01 (no lock)`을 확인하고, `[data]` 줄의 누락 봉 수를 기록한다. 이 단계는 성과를 계산하지 않는다.

- [ ] **Step 3: 단계 1을 1회 실행하고 곧바로 커밋**

```bash
git status --porcelain --untracked-files=no
uv run ewbot aeth stage1
git add aeth-stage1.json
git commit -m "chore(aeth): record stage 1" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

첫 줄의 출력이 비어 있어야 한다. 몇 분 걸린다(비교 두 번, 무작위 대조군 100회씩). 결과와 상관없이 기록을 **곧바로** 커밋한다(그 전에 파일을 지우거나 바꾸지 않는다: 1회 규칙). `stage1`이 실행 전에 거부하면(기간을 덮지 못함 등) 결과는 계산되지 않았으므로 이유를 사용자에게 보고하고 멈춘다(명세 2절의 예외: 사용자 승인과 명세 개정 뒤에만 고친다).

- [ ] **Step 4: 사용자에게 보고**

출력 그대로 보고한다: 전략별 표(A, HYBRID, C, BH, EMA), 무작위 대조군(100회, 평균 샤프·Calmar), 여섯 항목과 `passed`, 정보 실행(2019~2023의 A, 비용 2배의 A; 판정에 쓰지 않음을 밝힘), A의 연도별 거래 수(`aeth-stage1.json`의 `per_year_A`), 누락 봉 수, 기록 커밋. 명세 13절의 해석 한계(A는 BTC로 설계, BTC·ETH 상관, 같은 기간)를 함께 적는다.

- [ ] **Step 5: 탈락이면 종료를 기록한다**

`passed`가 거짓이면 A-ETH는 여기서 끝난다: 잠금을 만들지 않고, 2024 이후 ETH 봉을 받지 않으며, A는 BTC 전용, 다른 자산은 시도하지 않는다. 명세 14절에 결과 표(A의 순수익률·최대 낙폭·샤프·Calmar, C의 샤프·Calmar, 대조군 평균, 실패한 항목)와 "A-ETH 종료"를, `docs/aeth.md` 맨 위에 `> **Status (YYYY-MM-DD): ENDED at stage 1.** …`를, `README.md`의 A-ETH 줄 상태를 `Ended (stage 1)`로 적고 커밋한 뒤 PR을 만든다(사용자 병합). Task 10·11은 하지 않는다. 합격이면 기록 커밋을 그대로 두고 Task 10으로 간다.

---

### Task 10: 수동 절차 — 단계 2: 잠금, 사용자 승인, ETH 홀드아웃 1회 (컨트롤러와 사용자만, 서브에이전트 금지)

Task 9가 합격일 때만 한다. 되돌릴 수 없는 1회 실행이며 처음으로 2024 이후 ETH 봉을 받는다.

**Files:** `holdout-aeth.lock`(새 영구 기록, 두 번 커밋). 탈락이면 명세 14절과 문서의 상태 줄.

**Interfaces:**
- Consumes: Task 6의 `ewbot aeth holdout lock`, `ewbot aeth holdout run --confirm`; Task 9의 커밋된 `aeth-stage1.json`.
- Produces: `first_run`이 든 커밋된 `holdout-aeth.lock`과 사용자 보고. Task 11을 진행할지의 근거(`first_run.verdict.passed`).

- [ ] **Step 1: 잠금 작성과 즉시 커밋**

```bash
git status --porcelain --untracked-files=no
uv run ewbot aeth holdout lock
git add holdout-aeth.lock
git commit -m "chore(aeth): lock the ETH holdout" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git rev-parse HEAD
```

첫 줄의 출력이 비어 있어야 한다. 단계 1 기록이 커밋된 그대로가 아니거나, 합격이 아니거나, 다른 파라미터·코드·A 지문·봉으로 만든 것이면 거부된다. 잠금의 `last_bar_ts`(지금의 마지막 완성 시간)가 홀드아웃의 끝이다. 마지막 줄의 커밋(잠금 커밋)을 보고에 적는다.

- [ ] **Step 2: 사용자 승인(필수)**

사용자에게 잠금 내용(파라미터 (2.0, 0.7, 0.5)와 `holdout.lock` sha256, ETP 0.1 비용, 기간 2024-01-01 ~ `last_bar_ts`, 무작위 대조군 100회, A 지문, A-ETH 지문, `code_sha256`, 2024 이전 봉 기록)과 합격 기준(명세 7.3: A의 `evaluate_criteria` 그대로, `winners`에 A와 A의 최대 낙폭 ≤ 15%)을 보여 주고, ETH 홀드아웃을 **1회** 실행해도 되는지 명시적 승인을 받는다. 승인이 없으면 멈춘다.

- [ ] **Step 3: 실행 전 확인, 홀드아웃 1회, 즉시 커밋(네트워크)**

```bash
git status --porcelain --untracked-files=no
test "$(git rev-parse HEAD)" = "$(git log -1 --format=%H -- holdout-aeth.lock)" && echo "HEAD is the lock commit"
uv run --group live pytest tests/paper/test_fingerprint.py tests/paper/test_static.py tests/aeth/test_isolation.py -q
uv run ewbot aeth holdout run --confirm
git add holdout-aeth.lock
git commit -m "chore(aeth): record the ETH holdout run" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

실행 전: 첫 줄의 출력이 비어 있고, 둘째 줄이 `HEAD is the lock commit`을 출력하고, 보호 테스트가 모두 통과해야 한다(`54 passed`). 하나라도 어긋나면 실행하지 않고 보고한다. `holdout run`은 캐시를 지금까지 늘린 뒤(2024 이후 ETH 봉의 첫 다운로드) 1회 실행한다(몇 분). 끝나면 **바로** 커밋한다: 커밋 전에는 `holdout-aeth.lock`을 되돌리거나 지우지 않는다(두 번째 실행이 가능해진다). 같은 명령을 다시 실행하면 거부된다. `data/cache/ETH-USD_1h.parquet`은 모의매매가 홀드아웃을 재현할 때까지(`ewbot aeth paper status`에서 `holdout data ok`, `holdout trades recomputed`가 잠금과 같음) 지우지 않는다.

- [ ] **Step 4: 사용자에게 보고하고 병합**

출력 그대로 보고한다: 전략별 표, 무작위 대조군, `evaluate_criteria`의 `winners`·`trade_count` 줄, `verdict`(`c1`, `c2`, `n_trades`, `passed`), 홀드아웃 구간 봉 수와 해시. 명세 13절의 해석 한계(A의 BTC 홀드아웃과 같은 기간, 상관)를 함께 적는다. 그다음:

```bash
git push -u origin chore/aeth-validation
gh pr create --base master --title "chore(aeth): A-ETH stage 1 record, holdout-aeth.lock and its one run" --body "aeth-stage1.json and holdout-aeth.lock with first_run. 🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

탈락(`passed`가 거짓)이어도 두 기록은 영구 기록으로 병합하고 A-ETH를 끝낸다: 명세 14절, `docs/aeth.md` 상태 줄(`ENDED at stage 2`), `README.md` 상태를 같은 PR에 커밋한다. 모의매매를 켜지 않는다.

---

### Task 11: 수동 절차 — 단계 3: 모의매매 시작 (사용자)

Task 10이 합격이고 그 PR이 `master`에 병합된 뒤에만 한다. `docs/aeth.md`의 "Paper trading" 절차를 따른다.

**Files:** 없음(저장소 변수, Cloudflare Worker, `paper-aeth` 브랜치는 워크플로가 만든다). 첫 실행 뒤 `README.md`의 A-ETH 상태를 `Paper trading since YYYY-MM-DD`로 고치는 문서 커밋은 선택이다.

**Interfaces:**
- Consumes: Task 7의 `paper-aeth.yml`, `docs/aeth.md`의 Worker 코드; Task 10의 병합된 합격 잠금.
- Produces: 매시 실행되는 ETH 모의매매와 `paper-aeth` 장부.

- [ ] **Step 1: 워크플로 켜기와 두 번째 트리거(사용자)**

```bash
gh variable set PAPER_AETH_ENABLED --body true
```

Cloudflare에 두 번째 Worker `ewbot-paper-aeth-trigger`를 만들고(`docs/aeth.md`의 코드, A의 Worker와 같은 토큰을 시크릿 `GITHUB_TOKEN`으로), 크론 `42 * * * *`을 단다. A의 Worker는 바꾸지 않는다.

- [ ] **Step 2: 첫 실행과 확인**

```bash
gh workflow run paper-aeth.yml --ref master
gh run watch "$(gh run list --workflow paper-aeth.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
git fetch origin paper-aeth
git worktree add "$TMPDIR/paper-aeth" origin/paper-aeth
uv run ewbot aeth paper status --ledger-dir "$TMPDIR/paper-aeth"
git worktree remove "$TMPDIR/paper-aeth"
```

텔레그램에 `[ewbot paper-A-ETH]` 메시지가 오는지, 상태에서 `holdout data ok`이고 `holdout trades recomputed`가 잠금의 거래 수와 같은지, `ETH 표본 외 거래 n/50`이 홀드아웃 거래 수에서 시작하는지 확인한다.

- [ ] **Step 3: 첫 주 사용량과 4주 점검(사용자)**

1주 뒤 GitHub Billing에서 Actions 사용 시간을 확인한다(명세 8절: A와 합쳐 무료 2,000분을 넘을 수 있으므로 소액 예산을 유지한다; 매시 트리거는 줄이지 않는다). 28일 뒤 `ewbot aeth paper status`의 3번(재현)을 확인한다. 실거래는 Plan 5c이며 명세 8.1의 조건(A-BTC CANARY 통과 포함)이 모두 충족되고 사용자가 승인한 뒤에만 계획한다.

---

## 자기 검토 (Self-Review)

**1. 명세 대응표**

| 명세 | 요구 | 작업 |
|---|---|---|
| 1.1–1.4 목표·결정 사항 | A를 그대로 ETH에, 별도 트랙, 3단계·1회, 튜닝 없음, 실패 시 종료, 다른 자산 없음 | 1~11 |
| 2 사전 등록 | 명세·계획이 ETH 데이터보다 먼저, 결과 뒤 수정 없음, 기술적 거부의 예외 | Global Constraints, 9(Step 3), 10 |
| 3 격리 | `aeth/`만, import만, 수정 금지 목록, `cli.py`·`README.md`·`test_static.py` 최소 수정, 지연 import, 별도 산출물, A·B 보호 테스트 | 1(`test_isolation`, CI 검사), 4·5·7(`test_static`), 6(지연 import 테스트), 8 |
| 4 구조 | `config`, `fingerprint`, `lockfile`, `data`, `stage1`, `holdout`, `paper`, `sync`, 워크플로, 두 기록, 브랜치, CLI 6개 | 1~7 |
| 5.1 파라미터·설정 | `holdout.lock`에서 읽기(다이제스트 확인), A의 홀드아웃 설정, 옵션 없음 | 1, 6(해석 1) |
| 5.2 비용 | 계약 크기만 0.1, 최소 수수료 미적용, 실거래 확인 항목 | 1, 문서(7) |
| 5.3 데이터·가드 | `fetch_1h` import, 조각 저장, `update_cache` 미사용, 잠금 전 2024 요청·읽기 없음, 외부 2024 봉 거부 | 2(해석 10) |
| 6 단계 1 | `compare` 그대로, 여섯 항목, BH 등은 보고만, 정보 실행 2개, 1회 기록과 출처, 거부 조건, 탈락 시 종료 | 3(해석 2~4), 6, 9 |
| 7.1 잠금 | 커밋된 합격 기록에서만, 출처 확인, 묶는 필드(A 지문, 파라미터, 2024 이전 봉, 코드 상태 등), 깨끗한 트리, 즉시 커밋 | 4(해석 5·6), 10 |
| 7.2 1회 실행 | 승인 뒤 1회, 거부 조건, 시도 기록, `compare(allow_holdout=True)`, 즉시 커밋 | 4(해석 7), 6, 10 |
| 7.3 합격 | A의 `evaluate_criteria` 그대로, `winners`에 A + 낙폭 ≤ 15%, 거래 수 기록 | 4(해석 8) |
| 8 모의매매 | A의 런타임을 상품·비용·잠금만 바꿔, 장부 `paper-aeth`, 구간 지문, ETH 카운트 50(합산 없음), 4번 기준, 휴면 워크플로·두 번째 트리거, 접두어, 재계산 없음, 사용량 | 5(해석 9), 7(해석 13), 11 |
| 8.1 Plan 5c 조건 | 1~3 자동 표시, 4~6 수동 | 5(`status_lines_aeth`), 문서 |
| 9 실거래 제약 | 단일 상품, 계좌 단위 한도·증거금, SHADOW 재시작 규칙, 배포하지 않음 | 문서(7), 6(지연 import) |
| 10 오류 처리 | 수집 재시도·중단, 거부와 이유, 홀드아웃 데이터·거래 수 경고, 마지막 봉 누락, A·B와 충돌 없음 | 2, 4, 5, 6, 7 |
| 11 테스트 | 표의 모든 종류 | 1~7 |
| 12 진행 절차 | 커밋 → 구현·PR → 단계 1 → 단계 2(승인) → 단계 3 | 9~11 |
| 13 연구 정직성 | 보고에 해석 한계를 함께 적음 | 9(Step 4), 10(Step 4) |
| 14 결과 기록 | 단계별 결과·종료 기록 | 9(Step 5), 10(Step 4) |

**2. 자리 표시자 검사:** "TBD", "TODO", "나중에", "적절히 처리", "Task N과 같이" 같은 표현이 없다. 모든 코드 단계에 전체 코드(새 파일은 전체 내용, 기존 파일은 찾을 부분과 바꿀 내용)가 있고, 모든 명령에 예상 출력이 있다. 수동 작업(9~11)의 명령도 실제 명령이다.

**3. 타입 일관성:** 파라미터는 어디서나 `Combo = tuple[float, ...]`이고 JSON에서는 리스트다(`lock["params"]`, `rec["params"]`; 설정은 `aeth_config(tuple(...))`). 잠금 경로는 `ewbot.aeth.config.LOCK_PATH`, 단계 1 기록은 `STAGE1_PATH`, A의 잠금은 `A_LOCK_PATH`(모두 CLI가 호출 때 읽음). `data_record`의 키(`first_ts, last_ts, n_bars, missing, sha256`)는 단계 1 기록의 `provenance.data`, 잠금의 `pre2024`, 홀드아웃 확인에서 같다. `first_run.verdict.n_trades`·`passed`와 `first_run.data.sha256`은 홀드아웃(Task 4), 합성 잠금(`records.aeth_lock`), 장부 요약(`holdout_summary_aeth`)에서 같다. 구간 지문은 기준값(`epoch_fingerprint(lock["a_fingerprint"], lock["aeth_sha256"])`)과 실행값(`current_epoch_fingerprint()`)이 같은 함수다. 오류는 연구 쪽이 `AethError`(가드는 그 하위 `HoldoutGuardError`), 모의매매가 `AethPaperError(stage, message)`다.

**4. 검증:** 이 계획은 명세·계획 커밋 직후(2026-10-08) 저장소의 새 클론에서 그 커밋(`master` `cbf57ea` 위의 `feat/aeth` 첫 커밋, Task 1 코드 이전; Python 3.12.13, 네트워크 없음)부터 문서 그대로 적용해 확인했다: 모든 파일과 수정을 적힌 대로 쓰고 Task 1~8의 모든 명령을 실행했으며, 모든 `예상` 항목이 맞았다(각 Step 2는 적힌 대로 실패하고 각 Step 4는 적힌 수로 통과; 테스트 수는 앞에 다른 숫자가 붙지 않는지까지 확인). Task 8의 전체 테스트는 `1501 passed, 1 skipped`(실제 데이터 캐시 없음), A 지문은 `e0435223…` 그대로, A 보호 검사는 `59 passed`(건너뜀 없음), 바뀐 파일 목록은 위와 같고, 작업 트리는 깨끗했다. 적용 결과의 27개 파일은 개발 사본과 바이트 단위로 같았다. Task 9~11은 네트워크와 사용자 승인이 필요한 수동 단계라 재생에서 제외했다(ETH 데이터는 받지 않았다).
