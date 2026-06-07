# Jarvis — AI 트레이딩 시스템

KIS(한국투자증권) Open API 기반 AI 자율 트레이딩 시스템.  
AI 에이전트가 자연어 대화를 통해 분석 → 감시 → 매매를 자율 실행한다.

## 구조

```
quant/
├── fastapi_app.py          # 메인 서버 (포트 8000)
├── ai/
│   ├── agent.py            # AI 에이전트 루프
│   ├── provider.py         # LLM 공급자 추상화 (Anthropic / Gemini / Groq)
│   ├── tools.py            # 툴 스키마 + 실행 디스패처
│   ├── prompts.py          # 시스템 프롬프트
│   └── memory.py           # Redis 기반 대화·매매 이력
├── events/
│   ├── types.py            # WatchEntry, WatchCondition, MarketEvent
│   ├── detector.py         # EventDetector (10초 폴링, expr 조건 평가)
│   ├── indicator_cache.py  # IndicatorCache (5분봉 지표, Redis 캐시)
│   └── engine.py           # EventEngine (이벤트 라우팅)
├── jarvis/                 # Electron 데스크탑 앱
│   ├── main.js
│   ├── preload.js
│   └── src/
│       ├── index.html
│       ├── styles/main.css
│       └── renderer/
│           ├── index.js    # 채팅 + 슬래시 명령어
│           ├── sphere.js   # Three.js 3D 구체
│           └── panels.js   # 동적 패널 (차트·지표·감시)
├── kis/                    # KIS API 클라이언트
├── trading/                # OrderManager, 전략
├── collector/              # MarketDataCollector, AccountCollector
└── config.yaml             # 설정 (모드, API 키, AI 공급자)
```

## AI 툴 목록

| 툴 | 설명 |
|----|------|
| `get_price` | 현재가·거래량·등락률 |
| `get_orderbook` | 실시간 호가 |
| `get_portfolio` | 보유 포지션·잔고 |
| `get_rankings` | 거래량/거래대금 순위 |
| `get_candles` | 분봉·일봉 차트 |
| `get_indicators` | RSI·볼린저%B·Stoch·MA (IndicatorCache) |
| `set_watch` | 감시 조건 등록 (expr 타입) |
| `clear_watch` | 감시 해제 |
| `list_watches` | 감시 목록 조회 |
| `place_order` | 매수·매도 주문 |
| `cancel_order` | 미체결 취소 |
| `search_web` | 뉴스·시황 검색 |
| `save_plan` | 매매 전략 저장 |
| `save_memo` | 분석 메모 기록 |
| `get_history` | 매매 이력 조회 |
| `get_chat_history` | 대화 이력 조회 |
| `set_trading_mode` | paper/live 전환 |
| `get_system_status` | 서버·Redis·WebSocket 상태 |
| `get_logs` | 서버 로그 조회 |

## 감시 조건 (expr 타입)

AI는 자유 Python 불리언 수식으로 매수·매도 조건을 설정한다.

```python
# 사용 가능한 변수
price, volume, change_pct, volume_ratio   # 실시간 (10초)
rsi, macd, ma5, ma10, ma20, ma60          # 5분봉 기반 (5분 갱신)
bb_pct, bb_upper, bb_lower                # 볼린저 밴드
stoch_k, stoch_d                          # 스토캐스틱

# 예시
"rsi < 30 and bb_pct < 0.15 and change_pct < -2"  # 과매도 진입
"rsi > 70 and bb_pct > 0.85"                       # 과매수 익절
```

## Jarvis 앱 슬래시 명령어

| 명령어 | 설명 |
|--------|------|
| `/chart NVDA` | 일봉 차트 (TradingView Lightweight Charts) |
| `/min AAPL` | 분봉 차트 |
| `/watch` | 감시 종목 패널 |
| `/ind NVDA` | 기술 지표 패널 |
| `/mode paper\|live` | 거래 모드 전환 |
| `/clear` | 채팅 초기화 |
| `/help` | 명령어 목록 |

## FastAPI 엔드포인트

| 경로 | 설명 |
|------|------|
| `POST /ai/chat` | AI 대화 |
| `GET /ai/watches` | 감시 목록 |
| `GET /ai/indicators/{code}` | 기술 지표 |
| `GET /ai/candles/{code}` | 캔들 차트 데이터 |
| `GET /ai/system/status` | 시스템 상태 |
| `GET /ai/system/logs` | 서버 로그 |
| `GET /health` | 서버 헬스 |
| `GET /trade/positions/live` | 실시간 포지션 |
| `GET /account/balance` | 계좌 잔고 |

## 실행

```bash
# 1. Redis
redis-server

# 2. FastAPI 서버
cd ~/quant
.venv/bin/uvicorn fastapi_app:app --host 127.0.0.1 --port 8000

# 3-A. Jarvis 앱
cd ~/quant/jarvis && npm start

# 3-B. CLI
python cli.py
```

`config.yaml` AI 설정:
```yaml
ai:
  provider: "gemini"   # anthropic | gemini | groq
```

## 거래 모드

| 모드 | 설명 |
|------|------|
| `live` | 실전 계좌 실거래 |
| `paper` | KIS 모의투자 |
| `mock` | 로컬 테스트 (DB 미기록) |
