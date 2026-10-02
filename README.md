# tradingagents-web

[TradingAgents](https://github.com/TauricResearch/TradingAgents)를 브라우저에서 쓰는 웹 UI입니다.
CLI로 할 수 있는 작업을 모두 웹에서 실행합니다. 단일 분석, 백테스트, 포트폴리오 반영, 메모리 로그, 체크포인트 재개가 포함됩니다.

- TradingAgents는 수정하지 않고 의존성(`v0.5.2` 태그 고정)으로만 씁니다.
- FastAPI 서버가 HTML 페이지(Jinja2 + HTMX)와 JSON API(`/api`)를 함께 제공합니다. Node 빌드는 없습니다.
- 사용자 한 명, 계좌 하나를 기준으로 만들었습니다.

> TradingAgents는 연구용 프레임워크입니다. 결과는 LLM 샘플링과 실시간 데이터에 따라 실행마다 달라지며, 투자 조언이 아닙니다.

## 실행

Python 3.11 이상과 [uv](https://docs.astral.sh/uv/)가 필요합니다.

```bash
uv sync
cp .env.example .env          # API 키를 채웁니다
uv run tradingagents-web      # http://127.0.0.1:8000
```

API 키를 TradingAgents 저장소의 `.env`에 이미 두었다면, 이 저장소의 `.env`에는 그 경로와 웹 설정만 적으면 됩니다. 이 `.env`를 먼저 읽고, 그다음 지정한 파일을 읽습니다.

```bash
# tradingagents-web/.env
TRADINGAGENTS_WEB_ENV_FILE=/path/to/TradingAgents/.env
TRADINGAGENTS_WEB_TZ=Asia/Seoul
```

Docker에서는 지정한 경로가 컨테이너 안에 없으므로, API 키를 이 저장소의 `.env`에 직접 적어야 합니다.

### 다른 기기에서 접속

`127.0.0.1`이 아닌 주소로 열려면 토큰이 필요합니다. 토큰 없이 `--host 0.0.0.0`을 주면 서버가 시작하지 않습니다.

```bash
export TRADINGAGENTS_WEB_TOKEN=$(openssl rand -hex 32)
uv run tradingagents-web --host 0.0.0.0
```

브라우저에서는 로그인 화면에 토큰을 입력하고, API는 `Authorization: Bearer <토큰>` 헤더를 씁니다.
인터넷에 직접 열기보다 Tailscale 같은 사설망으로 접속하는 것을 권장합니다.

### Docker

```bash
cp .env.example .env
docker compose up -d --build   # http://127.0.0.1:8000
```

호스트의 `~/.tradingagents`를 마운트하므로 CLI와 같은 리포트, 메모리 로그, 체크포인트를 씁니다(`TRADINGAGENTS_DATA_DIR`로 변경).
포트는 `127.0.0.1`에만 열립니다. 더 넓게 열려면 `.env`에 `TRADINGAGENTS_WEB_TOKEN`을 설정하세요.
웹 DB에는 리포트의 절대 경로가 기록되므로, 같은 데이터 폴더를 Docker와 로컬 실행에서 번갈아 쓰지는 마세요.

## 화면

| 화면 | 경로 | 하는 일 |
| --- | --- | --- |
| 대시보드 | `/` | 진행 중인 작업, 재개할 수 있는 작업, 최근 분석, 대기 중인 메모리 결정 수 |
| 새 분석 | `/runs/new` | 티커(자동 정규화와 회사명·거래소·섹터 미리보기), 날짜, 분석가, 제공자와 모델, 라운드, 언어, 포트폴리오, 체크포인트 |
| 실행 상세 | `/runs/{id}` | 에이전트별 상태, 리포트 섹션, 메시지와 도구 호출이 SSE로 실시간 갱신. 취소, 재개, 다운로드, 같은 설정으로 다시 실행 |
| 실행 이력 | `/runs` | 티커, 상태, 등급 필터. 티커를 고르면 날짜별 등급. CLI 리포트 가져오기, 선택 삭제 |
| 실행 비교 | `/runs/compare?ids=..` | 완료된 실행 2~4개를 나란히. 다른 설정은 강조, 리포트 섹션은 열로 비교 |
| 백테스트 | `/backtests` | 티커 × 날짜 격자 실행, 칸 수와 예상 LLM 호출 수, 진행률, 등급별 적중률과 평균 alpha, 히트맵, 이어서 실행, 대기 칸 정산 |
| 예약 | `/schedules` | 요일과 시각을 정해 관심 티커를 자동 분석. 지금 실행, 켜기·끄기 |
| 포트폴리오 | `/portfolio` | 현금, 통화, 포지션 편집. CLI용 JSON 가져오기와 내보내기 |
| 메모리 | `/memory` | 결정, 수익률, alpha, 회고. 대기 중인 결정을 지금 정산 |
| 설정 | `/settings` | 기본 실행 설정, API 키 설정 여부, 체크포인트 목록과 삭제, 경로 |

JSON API 문서는 `/api/docs`에 있습니다.

## 동작 방식

- 분석, 백테스트, 정산은 모두 Job입니다. 각 Job은 별도 프로세스에서 실행되고, 진행 이벤트를 SQLite(`~/.tradingagents/webapp.db`)에 씁니다. 페이지는 이 이벤트를 SSE로 받습니다.
- 동시 실행은 `TRADINGAGENTS_WEB_MAX_WORKERS`(기본 2)개까지입니다. 같은 티커의 작업은 날짜가 달라도 한 번에 하나만 실행됩니다. 체크포인트 파일과 메모리 정산이 티커 단위이기 때문입니다.
- 취소하면 프로세스를 종료합니다. 체크포인트를 켠 분석과 백테스트는 취소나 실패 후 "재개"로 이어서 실행할 수 있습니다. 재개는 처음 요청(포트폴리오 스냅샷 포함)을 그대로 다시 보냅니다.
- 리포트(`logs/reports/`), 메모리 로그(`memory/trading_memory.md`), 체크포인트(`cache/checkpoints/`)는 CLI와 같은 경로를 씁니다.
- 실행 설정의 우선순위는 실행 폼, 설정 화면의 기본값, `.env`의 `TRADINGAGENTS_*`, 패키지 기본값 순입니다.
- CLI가 `results_dir/reports/`에 저장한 리포트는 서버 시작 때와 "CLI 리포트 가져오기" 버튼으로 실행 이력에 등록됩니다. 파일은 옮기지 않습니다.
- 예약은 `TRADINGAGENTS_WEB_TZ`(예: `Asia/Seoul`, 없으면 시스템 시간대) 기준으로 실행되고, 화면의 시각도 이 시간대로 표시됩니다. 서버가 꺼져 있던 동안 지난 예약은 켜질 때 한 번만 실행합니다.
- 실행을 삭제하면 웹 기록과 진행 이벤트만 지웁니다. "리포트 파일도 삭제"를 고르면 `results_dir` 안의 리포트 폴더도 지우고(다른 실행이 같은 폴더를 쓰면 남김), 메모리 로그의 결정은 어느 경우에도 남깁니다. 파일을 남긴 실행은 CLI 리포트 가져오기로 다시 등록되지 않습니다.
- 서버는 프로세스 하나로만 실행합니다(Job 스케줄러가 서버 안에 있습니다).

## 개발

```bash
uv run pytest        # 네트워크와 API 키 없이, 스크립트된 LLM으로 실제 그래프를 실행합니다
uv run ruff check src tests
```

TradingAgents를 올릴 때는 `pyproject.toml`의 `tag`를 바꾸고 `uv lock --upgrade-package tradingagents` 후 테스트를 돌립니다.
이 저장소는 TradingAgents의 공개 함수(`TradingAgentsGraph`, `backtest`, `TradingMemoryLog`, `PortfolioContext`, `checkpointer`, `model_catalog`)만 호출하고, `cli` 패키지는 쓰지 않습니다.

## 라이선스

포함하거나 가져온 서드파티 코드(htmx, Pico CSS, TradingAgents에서 옮긴 일부 코드)의 출처와 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)와 `LICENSES/`에 있습니다.
