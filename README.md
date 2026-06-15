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

> AI에게 툴을 줬을 때 실제로 발생한 문제들.  
> LLM이 툴을 어떻게 잘못 사용하는지, 그걸 어떻게 시스템 레벨에서 막았는지를 중심으로 기록한다.

---

### 1. AI가 watch 조건을 설정하고도 절대 트리거되지 않는 문제

**A — 문제**  
AI가 `set_watch`로 감시 조건을 등록했는데, RSI가 실제로 27까지 내려가도 이벤트가 전혀 발생하지 않았다.  
AI는 "조건을 설정했습니다"라고 응답했고 오류도 없었다.

**B — 원인 (두 가지가 겹쳐 있었다)**  
① AI가 `volume_ratio > 1.5` 조건을 watch 수식에 사용했다.  
`volume_ratio = volume / baseline_volume` 인데, `baseline_volume`은 `set_watch` 호출 시점의 당일 누적 거래량이다.  
다음 날 세션이 시작되면 `volume`이 0부터 다시 쌓이므로 `volume_ratio ≒ 0`이 되고,  
조건이 항상 False가 됐다.  
② AI가 `set_watch` 호출 전 `get_indicators`를 부르지 않고 현재 지표 상태를 확인하지 않았다.  
RSI가 이미 68인 상태에서 `rsi < 60` 조건을 등록해, 현재 상태에서 이미 위반된 조건이 만들어졌다.

**C — 해결 및 결과**  
① `volume_ratio`를 시스템 전체에서 금지.  
시스템 프롬프트와 `set_watch` 툴 description에 `⚠️ volume_ratio 사용 금지` 경고와  
올바른 대안(`volume > avg_volume * N`, 20봉 롤링 평균 대비) 예시를 명시.  
② 시스템 프롬프트에 CRITICAL 검증 블록 추가 —  
`set_watch` 호출 전 반드시 `get_indicators`를 먼저 호출하고,  
현재 지표 값과 조건 방향이 정합한지 AI가 스스로 검증하도록 강제.

```
# 수정 후 AI 동작
set_watch 전: get_indicators("NVDA") 호출
→ 현재 RSI 45, bb_pct 0.22, avg_volume 1,240,000
→ "rsi < 30 and bb_pct < 0.1 and volume > avg_volume * 1.5" 등록
→ 현재 RSI 45 → 목표 30, 달성 가능한 방향 확인 후 등록
```

---

### 2. AI가 툴을 호출하지 않고 JSON을 텍스트로 출력하는 문제

**A — 문제**  
AI가 watch 조건을 설정하거나 주문을 실행해야 하는 상황에서  
실제 툴을 호출하는 대신 "이렇게 설정하면 됩니다: `{"name": "set_watch", ...}`" 식으로  
raw JSON을 텍스트 응답에 포함시키는 경우가 발생했다.  
채팅창에 JSON이 그대로 노출되고, 실제 감시 등록과 주문은 이뤄지지 않았다.

**B — 원인**  
LLM이 특정 패턴의 입력에서 "도구 사용 방법을 설명하는" 텍스트 모드로 전환됐다.  
모델이 tool_use 블록 대신 텍스트로 응답을 완료해 루프가 종료됐다.

**C — 해결 및 결과**  
① `_looks_like_raw_tool_output()` — 응답 텍스트에 JSON 마커(`{`, `"name":`, `"formula":` 등) 감지.  
② 감지 시 "JSON을 포함하지 말고 툴을 실제로 호출하라"는 retry prompt를 삽입해 LLM을 재호출.  
③ 계획 수립 요청(`save_plan`, `set_watch` 등) 완료 후 필수 툴 누락 시,  
`_retry_missing_plan_tools()`에서 시스템이 직접 해당 툴을 실행하는 fallback 추가.

---

### 3. AI가 재무 데이터를 환각(hallucination)으로 지어내는 문제

**A — 문제**  
"현재 잔고 얼마야?"라는 질문에 AI가 `get_portfolio` 툴을 호출하지 않고  
"현재 잔고는 약 5,230,000원입니다"처럼 숫자를 임의로 생성해 답했다.  
실제 잔고와 전혀 다른 수치였다.

**B — 원인**  
LLM은 non-deterministic하고, 학습 데이터에서 유사한 숫자 패턴을 생성할 수 있다.  
Tool-Use 루프에서 툴을 호출하지 않아도 텍스트 응답으로 루프가 종료되면  
시스템은 그 응답을 그대로 사용자에게 전달했다.

**C — 해결 및 결과**  
`agent.py`에서 재무 관련 키워드를 감지하고, 해당 툴 호출 여부를 확인 후 미호출 시 강제 재시도.

```python
def _requires_data_tool(self, user_input: str) -> str | None:
    if any(k in user_input for k in ("잔고", "포지션", "보유")):
        return "get_portfolio"
    if any(k in user_input for k in ("현재가", "주가", "얼마")):
        return "get_price"
    return None

# 재시도 prompt
"get_portfolio를 먼저 호출하지 않고 잔고를 답했습니다. 반드시 툴을 호출하세요."
```

시스템 프롬프트에도 명시: "재무 수치는 반드시 툴 결과에서만 가져올 것. 수치를 지어내는 것은 엄격히 금지."

---

### 4. AI가 계획만 수립하고 set_watch를 빠뜨리는 문제

**A — 문제**  
모닝 브리핑 후 AI가 "삼성전자 RSI 과매도 구간 진입 시 매수 진행하겠습니다"라고 응답했지만  
실제 `set_watch` 호출 없이 텍스트 응답으로 루프를 종료했다.  
EventDetector에 감시 조건이 등록되지 않았으므로, 실제 조건이 충족돼도 알림이 오지 않았다.

**B — 원인**  
LLM이 "계획을 텍스트로 설명하는 것"과 "툴을 호출해 실제로 실행하는 것"을 혼동했다.  
`save_plan`으로 계획을 저장한 뒤 루프를 종료하는 패턴이 반복됐다.

**C — 해결 및 결과**  
`agent.py`의 `_retry_missing_plan_tools()` — 계획 수립 응답 후 `set_watch` 누락 시  
"계획에 감시 조건이 언급됐으나 set_watch가 호출되지 않았습니다. 지금 바로 등록하세요"  
retry prompt를 삽입해 툴 호출을 강제.  
시스템 프롬프트에도 명시: "WAIT_FOR_TRIGGER 전략 선택 시 set_watch 호출은 선택이 아니라 필수."

---

### 5. AI가 금지된 watch 조건 타입을 사용하려는 문제

**A — 문제**  
AI가 `set_watch` 툴을 호출할 때 `type: "price_change"`, `type: "volume_spike"` 같은  
타입을 사용했다. 서버가 이 타입들을 거부하면서 감시 등록이 실패했다.

**B — 원인**  
툴 스키마에 허용 타입 목록이 있었지만 LLM이 학습 데이터의 패턴을 따라  
더 직관적으로 보이는 타입명을 사용하려 했다.  
`price_change: 5%` 같은 표현이 자연어로는 더 명확해 보이기 때문이다.

**C — 해결 및 결과**  
툴 description과 시스템 프롬프트를 강화해 `expr` 타입만 허용임을 명시하고,  
서버 에러 응답에도 올바른 예시를 포함시켜 AI가 즉시 수정할 수 있게 했다.

```python
# 에러 응답 — AI가 읽고 바로 수정 가능한 구조
return json.dumps({
    "error": "price_change 타입은 사용 불가. 서버에서 거부됨.",
    "instruction": "expr 타입을 사용하고 formula 필드에 파이썬 불리언 식을 작성하세요.",
    "example": "rsi < 30 and bb_pct < 0.15 and change_pct < -2"
})
```

이후 AI가 에러 응답을 받고 즉시 `expr` 타입으로 재시도하는 패턴이 정착됐다.

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
