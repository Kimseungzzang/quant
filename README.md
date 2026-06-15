# Jarvis — AI 자율 트레이딩 시스템

> KIS Open API + LLM Tool-Use 기반 국내/미국 주식 자동매매 에이전트

---

## 프로젝트 개요

| 항목 | 내용 |
|------|------|
| **개발 기간** | 2026.05.30 – 2026.06.10 (약 2주) |
| **팀 규모** | 1인 (개인 프로젝트) |
| **핵심 목표** | LLM의 추론 능력 + 실제 증권사 API = 자율 트레이딩 |
| **대상 시장** | KRX(국내), NYSE/NASDAQ(미국) |
| **거래 모드** | paper / live — `config.yaml` 한 줄 전환 |

사용자가 "NVDA 과매도 구간 진입하면 알려줘"처럼 자연어로 지시하면,
AI가 직접 KIS API를 호출해 감시 조건을 등록하고, 조건 충족 시 매매 판단을 내린다.

---

## 기술 스택

| 분류 | 기술 | 용도 |
|------|------|------|
| **서버** | Python 3.12 / FastAPI / asyncio | 메인 서버, WebSocket 스트리밍, 자동매매 루프 |
| **AI** | Claude / Gemini / Groq (config 1줄 전환) | LLM 추론 엔진 |
| **증권 API** | KIS Open API (REST + WebSocket) | 실시간 체결가, 주문 실행, 잔고 조회 |
| **캐시** | Redis | 실시간 가격 캐시, 감시 조건 저장, 기술 지표 캐시 |
| **DB** | PostgreSQL | 매매 이력, 채팅 이력, AI 메모 |
| **지표** | pandas / pandas_ta | 5분봉 기반 RSI / MACD / 볼린저밴드 / 스토캐스틱 |
| **수식 평가** | simpleeval | AI 작성 Python 불리언 수식 안전 실행 |
| **데스크탑** | Electron + Three.js | Jarvis UI (3D 구체, 채팅, 실시간 패널) |
| **차트** | LightweightCharts v5 | 실시간 5분봉 차트 (WebSocket 틱 기반) |

---

## 시스템 아키텍처

```
사용자 (Jarvis Electron 앱)
        ↓ 자연어 메시지
  FastAPI 서버 (:8000)
        ↓ POST /ai/chat
    AI Agent (ai/agent.py)
        ↓ LLM Tool-Use 반복 루프
  ToolExecutor (ai/tools.py)        ← 시스템의 핵심
        ↓
  ┌─────────────────────────────┐
  │  KIS REST API               │  → 현재가, 주문, 잔고
  │  KIS WebSocket              │  → 실시간 체결가 스트리밍
  │  Redis                      │  → 가격 캐시, 감시 조건, 지표
  │  PostgreSQL                 │  → 매매 이력, 채팅, 메모
  └─────────────────────────────┘
        ↑ Redis 폴링 (10초)
  EventDetector (events/detector.py)
        ↓ 조건 충족 시 MarketEvent
  EventEngine → AIAgent.handle_event()
```

---

## 핵심 강점 — AI Tool-Use 아키텍처 설계

### 설계 철학: Claude Code에서 가져온 패턴

이 시스템의 핵심 설계 철학은 Claude Code(LLM CLI)에서 가져왔다.  
Claude Code가 LLM으로 파일을 읽고 코드를 실행하듯, Jarvis는 같은 패턴을 트레이딩에 적용한다.

```
Claude Code:  LLM → Read / Edit / Bash 툴 → 파일시스템/쉘
Jarvis:       LLM → get_price / place_order / set_watch 툴 → KIS API / Redis
```

**LLM은 추론만 한다. 실제 세상과의 접점은 전부 18개 툴이다.**

이 구조의 장점:
- LLM 교체 시 툴 인터페이스는 그대로 (Anthropic → Gemini → Groq 전환 비용 0)
- 툴이 실패하면 LLM이 에러 메시지를 받아 다음 판단에 반영
- 모든 행동이 로깅 가능 (어떤 툴을 어떤 인자로 호출했는지 추적)

---

### Tool-Use 반복 루프

```python
# ai/provider.py
while True:
    response = llm.call(messages, tools=TOOL_DEFINITIONS)

    if response.stop_reason == "end_turn":
        break  # 텍스트 응답 → 루프 종료

    for tool_use in response.tool_uses:
        result = await executor.execute(tool_use.name, tool_use.input)
        messages.append({"role": "tool", "content": result})
    # tool_result를 context에 추가하고 다시 LLM 호출
```

---

### 툴 설계 원칙 4가지

**① 스키마가 곧 계약 — description에 예시와 금지 조건 명시**

```python
{
    "name": "set_watch",
    "description": (
        "expr 타입만 허용. 반드시 2개 이상 지표 조합. "
        "⚠️ volume_ratio 사용 금지 — 세션 시작 시 초기화돼 항상 0이 됨. "
        "대신 volume > avg_volume * N 패턴 사용. "
        "예시: 'rsi < 30 and volume > avg_volume * 1.5'"
    ),
}
```

**② 툴이 AI를 가르친다 — 에러도 LLM이 읽고 수정 가능하게**

```python
# 나쁜 에러: raise ValueError("조건 타입 오류")  ← LLM이 해석 불가
# 좋은 에러: LLM이 읽고 스스로 수정할 수 있는 구조화된 응답
return json.dumps({
    "error": "price_change 타입은 사용 불가",
    "instruction": "expr 타입을 사용하고 formula에 파이썬 식을 작성하세요",
    "example": "rsi < 30 and bb_pct < 0.15"
})
```

**③ 주문 전 정보 수집 강제 — 시스템 레벨 가드레일**

```python
# ai/tools.py — place_order 진입 시 선행 툴 호출 여부 검사
missing_precheck = [
    name for name in ("get_portfolio", "get_price")
    if name not in set(self.executed_tools)
]
if missing_precheck:
    return json.dumps({"error": "주문 전 필수 확인 도구 누락", "missing": missing_precheck})
```

LLM이 "포지션 확인 없이 바로 매수"하는 것을 시스템 레벨에서 차단한다.

**④ 환각 방지 — 재무 질문 시 툴 미호출 감지 후 강제 재시도**

```python
# ai/agent.py
def _requires_data_tool(self, user_input: str) -> str | None:
    if any(k in user_input for k in ("잔고", "포지션", "보유")):
        return "get_portfolio"
    if any(k in user_input for k in ("현재가", "주가", "얼마")):
        return "get_price"
    return None
```

LLM이 `get_portfolio`를 호출하지 않고 잔고를 임의로 지어내는 경우,
해당 툴을 강제 재호출한 뒤 응답을 생성하게 한다.

---

### 이벤트 기반 자율 매매

```
watch 조건 충족 (EventDetector 10초 폴링)
    ↓ MarketEvent 발생
AIAgent.handle_event(event)
    ↓
    ├── get_price("NVDA")       → 현재가 확인
    ├── get_candles("NVDA")     → 추세 확인
    ├── get_portfolio()          → 잔고·포지션 확인
    └── place_order(...)         → 또는 save_memo("관망")
```

사용자 없이 조건 충족 시 AI가 자율적으로 판단·주문·기록을 수행한다.  
`handle_event` 완료 후 `get_price`, `save_memo` 누락 시 재시도를 강제해  
"판단만 하고 기록을 남기지 않는" 케이스를 방지한다.

---

### AI 툴 목록 (18개)

| 툴 | 설명 |
|----|------|
| `get_market_session` | 현재 국내/해외 세션 및 가격 API 기준 조회 |
| `get_price` | 현재가 (국내 통합/KRX/NXT/시간외 분리) |
| `get_indicators` | RSI·볼린저·Stoch·MA (5분봉 + 일봉 분리) |
| `get_candles` | 분봉·일봉 차트 데이터 |
| `get_portfolio` | 보유 포지션 + 계좌 잔고 |
| `screen_candidates` | 거래량 상위 종목 스크리닝 (일봉 지표 필터) |
| `set_watch` | 감시 조건 등록 (expr Python 불리언 수식) |
| `clear_watch` / `list_watches` | 감시 해제 / 목록 조회 |
| `place_order` / `cancel_order` | KIS 실제 주문 실행 / 취소 |
| `search_web` | DuckDuckGo 실시간 뉴스·시황 검색 |
| `save_plan` / `save_memo` | 전략·판단 기록 (PostgreSQL) |
| `get_history` / `get_chat_history` | 매매·대화 이력 조회 |
| `get_system_status` / `get_logs` | 서버·Redis 상태 / 로그 조회 (자가 진단) |

---

## 트러블슈팅 & 성과 (A → B → C 패턴)

### 1. 손절·익절이 전혀 작동하지 않는 문제

**A — 문제**  
포지션 보유 중 5% 이상 손실이 나도 자동 청산이 일어나지 않았다.  
WebSocket 틱은 정상 수신 중이었고 로그에도 에러가 없었다.

**B — 원인**  
WebSocket 가격 콜백 `on_domestic_price`에서 `order_mgr.record_price(code, price)` 를 호출하고 있었다.  
`record_price`는 내부 가격 dict만 갱신하는 메서드다.  
손절·익절 평가는 `on_price_update(code, price, signal)` 에 들어 있었는데,  
두 메서드가 이름이 비슷해 잘못된 것을 호출하고 있었다.

**C — 해결 및 결과**  
모든 WebSocket 가격 콜백에서 `record_price` → `on_price_update`로 교체.  
이후 paper 모드에서 손절 -5% 조건이 틱 수신 즉시 트리거되는 것을 확인했다.

---

### 2. 감시 종목 11개 중 6개의 기술 지표 캐시가 항상 비어 있는 문제

**A — 문제**  
`get_indicators` 툴이 일부 종목에서 `{}` 빈 딕셔너리를 반환했다.  
로그 버퍼를 조회해도 지표 계산 실패 로그가 보이지 않았다.

**B — 원인 (두 가지가 겹쳐 있었다)**  
① `EventDetector`가 expr 수식 평가 실패 시 `logger.warning()`으로 기록했는데,  
500개 항목짜리 로그 버퍼에 10초마다 감시 종목 수만큼 WARN이 쌓여  
실제 에러(지표 계산 타임아웃)가 버퍼에서 밀려났다.  
② `IndicatorCache._refresh_all()`에서 `run_in_executor()`에 타임아웃이 없어,  
KIS API `ReadTimeoutError` 발생 시 해당 종목이 조용히 블로킹 상태로 남아 있었다.

**C — 해결 및 결과**  
① expr 평가 실패 로그를 `WARNING` → `DEBUG`로 낮춰 로그 버퍼 오염 제거.  
② `asyncio.wait_for(..., timeout=30)`를 각 종목 갱신 호출에 적용.  
타임아웃 시 명확한 WARN 로그 출력 후 다음 5분 주기에 재시도.  
이후 6개 종목의 지표 캐시가 다음 갱신 주기(5분) 내에 정상 채워졌다.

---

### 3. 감시 조건이 절대 트리거되지 않는 문제

**A — 문제**  
"RSI < 30 진입 조건"을 watch로 등록했는데,  
RSI가 실제로 27까지 내려가도 이벤트가 전혀 발생하지 않았다.

**B — 원인 (구조적 문제 두 가지)**  
① `set_watch` 시 `baseline_price`를 WebSocket Redis 캐시에서만 읽었다.  
세션 초기이거나 해당 종목이 아직 WebSocket에 구독되지 않았으면 캐시가 없어  
`baseline_price = 0.0`으로 저장됐다.  
`change_pct = (price - 0) / 0 * 100` → 분모 0으로 `change_pct = 0.0`,  
관련 조건이 항상 False였다.  
② `volume_ratio = volume / baseline_volume` 구조적 한계.  
`baseline_volume`은 감시 등록 시점의 당일 누적 거래량인데,  
다음 날 세션 시작 시 `volume`이 0부터 다시 쌓이므로 `volume_ratio ≒ 0`이 됐다.

**C — 해결 및 결과**  
① `_resolve_watch_baseline`에 국내 종목 REST API 폴백 추가 —  
WebSocket 캐시가 없으면 즉시 REST로 현재가를 조회해 `baseline_price`를 채운다.  
② `volume_ratio`를 시스템 전체에서 금지 —  
시스템 프롬프트와 툴 description에 `⚠️ volume_ratio 사용 금지` 경고와  
올바른 대안(`volume > avg_volume * N`) 예시를 추가했다.  
이후 AI가 모든 watch 조건에서 `avg_volume` 기반 패턴을 사용하게 됐다.

---

### 4. AI가 달성 불가한 Watch 조건을 등록하는 문제

**A — 문제**  
RSI가 이미 68인 상태에서 AI가 `rsi < 60 and price > ma5` 조건을 등록했다.  
현재 상태에서 이미 위반된 조건이라 영원히 트리거될 수 없었다.

**B — 원인**  
AI가 현재 지표 상태를 확인하지 않고 템플릿 조건을 그대로 사용했다.

**C — 해결 및 결과**  
시스템 프롬프트에 CRITICAL 검증 블록 추가:  
`set_watch` 호출 전 반드시 `get_indicators`를 먼저 호출하고,  
현재 지표 값과 조건 방향의 정합성을 AI 스스로 검증하도록 강제.  
이후 AI가 "현재 RSI 45 → 목표 30, 합리적 거리"처럼 근거를 명시하고 조건을 설정하게 됐다.

---

### 5. TOCTOU 레이스 컨디션 — 동시 매수 중복 주문

**A — 문제**  
AI가 짧은 시간 내에 같은 종목에 매수 신호를 연속으로 보내면 중복 주문이 발생했다.

**B — 원인**  
`open_position`이 락 안에서 중복 체크 후 락을 해제하고 KIS API를 호출했다.  
API 응답 대기 시간(~300ms) 동안 두 번째 호출이 중복 체크를 통과할 수 있었다 (TOCTOU).

**C — 해결 및 결과**  
API 호출 전 sentinel `PendingOrder`를 먼저 등록해 즉시 슬롯을 점유하고,  
`finally` 블록에서 실제 주문번호로 교체하거나 실패 시 제거하는 패턴 적용.  
이후 동시 매수 신호에 대해 두 번째 호출이 "이미 처리 중" 응답을 받고 차단됐다.

---

### 6. Paper 모드 WebSocket 연결이 시작 직후 끊기는 문제

**A — 문제**  
paper 모드로 서버 시작 시 WebSocket이 수 초 내에 종료됐다.  
체결 데이터도 수신되지 않았다.

**B — 원인**  
KIS paper 서버(port 31000)는 H0STCNI9(체결통보 TR) 구독을 지원하지 않아  
구독 즉시 연결을 강제 종료했다.

**C — 해결 및 결과**  
`not self.auth.is_paper` 조건으로 paper에서 체결통보 구독 자체를 스킵.  
대신 `_paper_fill_poll_loop`에서 30초마다 `get_daily_orders()` REST 폴링으로 체결을 감지해  
`reconcile_order_rows()`로 포지션에 반영.  
이후 paper 모드에서 WebSocket이 안정적으로 유지되고 체결도 정상 감지됐다.

---

### 7. 동기 KIS API 호출이 asyncio 이벤트 루프를 블로킹하는 문제

**A — 문제**  
AI가 `get_price`, `get_portfolio` 같은 툴을 호출하는 동안  
다른 WebSocket 수신, HTTP 요청이 모두 지연됐다.

**B — 원인**  
`tools.py`의 툴 실행 메서드들이 `requests` 라이브러리(동기 HTTP)를 사용하면서  
`async def execute()` 안에서 직접 호출됐다.  
동기 IO 호출은 이벤트 루프 전체를 블로킹한다.

**C — 해결 및 결과**  
`asyncio.to_thread()`로 스레드 풀에 위임해 이벤트 루프를 해방.

```python
# 수정 전 — 이벤트 루프 블로킹
case "get_price":
    return self._get_price(...)

# 수정 후 — 스레드 풀에서 실행
case "get_price":
    return await asyncio.to_thread(self._get_price, ...)
```

이후 AI 툴 호출 중에도 WebSocket 틱 수신이 끊기지 않았다.

---

## 주요 설계 결정

| 결정 | 이유 |
|------|------|
| **EventDetector 10초 폴링** | RSI·볼린저 등 기술 지표는 수십 개 캔들 기반 → WebSocket 틱 1개로 계산 불가. Redis에서 최신 상태를 읽고 IndicatorCache와 조합하는 구조가 최적 |
| **asyncio.Queue (내부 큐)** | 이벤트 빈도 하루 수십 건, 영속성 불필요 → Redis Pub/Sub 등 외부 브로커 과잉. in-process 큐가 충분하고 배포 복잡도가 낮음 |
| **simpleeval** | AI가 작성한 임의 Python 수식을 `eval()` 대신 안전하게 실행. 허용 함수와 연산자를 화이트리스트로 제한 |
| **routers/state.py** | FastAPI 라우터 분리 시 순환 임포트 발생 → 공유 상태를 단방향 단일 모듈로 격리. 라우터는 `state`만 import, `fastapi_app.py`가 lifespan에서 값을 주입 |
| **런타임 모드 변경 차단** | `KISAuth.base_url`, WebSocket TR ID 등 초기화 시 고정 → 런타임 전환 시 일부만 바뀌어 paper 주문이 live DB에 기록되는 오작동 방지 |

---

## 실행 방법

```bash
# 1. Redis
redis-server

# 2. FastAPI 서버
cd ~/quant && .venv/bin/uvicorn fastapi_app:app --host 0.0.0.0 --port 8000

# 3. Jarvis Electron 앱
cd ~/quant/jarvis && npm start
```

`config.yaml`:
```yaml
mode: paper          # paper | live
ai:
  provider: gemini   # anthropic | gemini | groq
```

---

## 디렉토리 구조

```
quant/
├── fastapi_app.py          # 메인 서버 (lifespan, WebSocket, 스케줄러)
├── ai/
│   ├── agent.py            # AI 에이전트 루프 (chat, handle_event, morning_brief)
│   ├── provider.py         # LLM 공급자 추상화 (Anthropic / OpenAI-compatible)
│   ├── tools.py            # 툴 스키마 + execute() 디스패처  ← 시스템 핵심
│   └── prompts.py          # 시스템 프롬프트 + 툴 사용 지침
├── events/
│   ├── detector.py         # EventDetector (10초 폴링, pandas_ta 지표 평가)
│   ├── indicator_cache.py  # 5분봉 기술 지표 캐시 (Redis, 5분 갱신)
│   └── engine.py           # EventEngine (asyncio.Queue 기반 이벤트 라우팅)
├── kis/
│   ├── auth.py             # 토큰 관리 (메모리 + Redis 2단계 캐시)
│   ├── websocket.py        # WebSocket (AES-CBC 복호화, 다중레코드, 자동재연결)
│   ├── domestic.py         # 국내주식 API
│   └── overseas.py         # 해외주식 API
├── trading/
│   └── order_manager.py    # 주문 실행 + 손절·익절 평가
├── routers/
│   ├── state.py            # 공유 전역 상태 (순환 임포트 방지)
│   ├── ai_routes.py        # /ai/* 엔드포인트
│   ├── trade_routes.py     # /account/*, /trade/*, /trades/*
│   └── system_routes.py    # /health, /ai/system/*
└── jarvis/                 # Electron 데스크탑 앱
    └── src/renderer/
        ├── index.js        # 채팅 + 슬래시 명령어
        ├── sphere.js       # Three.js 3D 구체 (idle/thinking/speaking)
        └── panels.js       # 실시간 차트·포지션 패널 (WebSocket 틱 기반)
```
