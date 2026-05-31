"""
KIS Open API Mock Server (aiohttp 기반)
- REST: http://localhost:8000
- WebSocket: ws://localhost:8000/ws

실행: python tests/mock_server.py
"""
import asyncio
import json
import logging
import random
import uuid
from datetime import date, datetime, timedelta

import numpy as np
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [MOCK] %(message)s")
logger = logging.getLogger("mock")

# ── 목(Mock) 종목 유니버스 ────────────────────────────────────────────

DOMESTIC_STOCKS = [
    {"code": "005930", "name": "삼성전자",  "base_price": 75000,  "sigma": 0.018},
    {"code": "000660", "name": "SK하이닉스","base_price": 185000, "sigma": 0.022},
    {"code": "035420", "name": "NAVER",     "base_price": 220000, "sigma": 0.020},
    {"code": "051910", "name": "LG화학",    "base_price": 340000, "sigma": 0.025},
    {"code": "005380", "name": "현대차",    "base_price": 195000, "sigma": 0.016},
    {"code": "068270", "name": "셀트리온",  "base_price": 185000, "sigma": 0.028},
]

OVERSEAS_STOCKS = [
    {"code": "AAPL",  "name": "Apple",    "base_price": 185.0,  "sigma": 0.015, "exch": "NASD"},
    {"code": "NVDA",  "name": "NVIDIA",   "base_price": 875.0,  "sigma": 0.030, "exch": "NASD"},
    {"code": "MSFT",  "name": "Microsoft","base_price": 415.0,  "sigma": 0.014, "exch": "NASD"},
]

# ── OHLCV 데이터 생성 (기하 브라운 운동) ─────────────────────────────

def generate_ohlcv(base_price: float, sigma: float, days: int = 100,
                   trend: float = 0.0003, seed: int = 42) -> list[dict]:
    """
    실제 주가처럼 보이는 OHLCV 생성.
    trend > 0: 상승 추세 (매수 신호 발생 가능)
    """
    rng = np.random.default_rng(seed)
    prices = [base_price]
    for _ in range(days - 1):
        ret = trend + sigma * rng.standard_normal()
        prices.append(prices[-1] * np.exp(ret))

    end = date.today()
    rows = []
    for i, close in enumerate(prices):
        d = end - timedelta(days=days - 1 - i)
        if d.weekday() >= 5:          # 주말 제외
            continue
        noise = sigma * 0.5
        open_p  = close * (1 + rng.uniform(-noise, noise))
        high_p  = max(open_p, close) * (1 + abs(rng.uniform(0, noise)))
        low_p   = min(open_p, close) * (1 - abs(rng.uniform(0, noise)))
        vol_base = int(base_price * 1000)
        vol = int(vol_base * rng.uniform(0.5, 2.5))
        rows.append({
            "stck_bsop_date": d.strftime("%Y%m%d"),
            "stck_oprc":      str(int(open_p)),
            "stck_hgpr":      str(int(high_p)),
            "stck_lwpr":      str(int(low_p)),
            "stck_clpr":      str(int(close)),
            "acml_vol":       str(vol),
            "acml_tr_pbmn":   str(int(close * vol)),
        })
    return rows


# 서버 시작 시 데이터 미리 생성 (종목별 고정 시드로 재현 가능)
_OHLCV_CACHE: dict[str, list[dict]] = {}

def _init_ohlcv():
    for i, s in enumerate(DOMESTIC_STOCKS):
        trend = 0.0008 if i < 3 else 0.0002   # 200일치, 강한 상승 추세
        _OHLCV_CACHE[s["code"]] = generate_ohlcv(s["base_price"], s["sigma"],
                                                   days=200, trend=trend, seed=i)
    for i, s in enumerate(OVERSEAS_STOCKS):
        _OHLCV_CACHE[s["code"]] = generate_ohlcv(s["base_price"], s["sigma"],
                                                   days=200, trend=0.0006, seed=100+i)

_init_ohlcv()


def _latest_price(code: str) -> float:
    rows = _OHLCV_CACHE.get(code, [])
    if not rows:
        return 0.0
    return float(rows[-1]["stck_clpr"])


# ── 인메모리 상태 (주문, 잔고) ────────────────────────────────────────

_orders: list[dict] = []
_cash_krw = 10_000_000.0       # 1천만 원
_cash_usd = 10_000.0           # 1만 달러
_order_counter = 1000000


def _next_order_no() -> str:
    global _order_counter
    _order_counter += 1
    return str(_order_counter)


def _is_buy_tr(tr_id: str) -> bool:
    return tr_id in ("TTTC0012U", "VTTC0012U", "TTTT1002U", "VTTT1002U")


def _fill_tr_id(order: dict) -> str:
    tr_id = order.get("tr_id", "")
    if order.get("type") == "domestic":
        return "H0STCNI9" if tr_id.startswith("V") else "H0STCNI0"
    return "H0GSCNI9" if tr_id.startswith("V") else "H0GSCNI0"


def _order_fill_price(order: dict) -> str:
    raw = str(order.get("price") or "0")
    try:
        price = float(raw)
    except ValueError:
        price = 0.0
    if price <= 0:
        price = _latest_price(order.get("code", ""))
    if order.get("type") == "domestic":
        return str(int(price))
    return f"{price:.2f}"


# ── 공통 응답 헬퍼 ────────────────────────────────────────────────────

def _ok(output=None, **kwargs) -> dict:
    resp = {"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "정상처리되었습니다."}
    if output is not None:
        resp["output"] = output
    resp.update(kwargs)
    return resp


def _json(data: dict, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
        status=status,
    )


# ── REST 핸들러 ───────────────────────────────────────────────────────

# OAuth
async def token_issue(req: web.Request) -> web.Response:
    expired = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return _json({
        "access_token": f"mock_token_{uuid.uuid4().hex}",
        "access_token_token_expired": expired,
        "token_type": "Bearer",
        "expires_in": 86400,
    })


async def ws_approval(req: web.Request) -> web.Response:
    return _json({"approval_key": f"mock_wskey_{uuid.uuid4().hex[:8]}"})


# 국내 시세
async def domestic_price(req: web.Request) -> web.Response:
    code = req.rel_url.query.get("FID_INPUT_ISCD", "005930")
    price = _latest_price(code)
    rows = _OHLCV_CACHE.get(code, [])
    prev = float(rows[-2]["stck_clpr"]) if len(rows) >= 2 else price
    change = int(price - prev)
    change_pct = round((price - prev) / prev * 100, 2) if prev else 0
    return _json(_ok(output={
        "stck_prpr":      str(int(price)),
        "prdy_vrss":      str(change),
        "prdy_vrss_sign": "2" if change >= 0 else "5",
        "prdy_ctrt":      str(change_pct),
        "acml_vol":       str(random.randint(1_000_000, 50_000_000)),
        "acml_tr_pbmn":   str(int(price * random.randint(1_000_000, 50_000_000))),
        "stck_oprc":      rows[-1]["stck_oprc"] if rows else str(int(price)),
        "stck_hgpr":      rows[-1]["stck_hgpr"] if rows else str(int(price * 1.02)),
        "stck_lwpr":      rows[-1]["stck_lwpr"] if rows else str(int(price * 0.98)),
        "hts_kor_isnm":   next((s["name"] for s in DOMESTIC_STOCKS if s["code"] == code), code),
    }))


async def domestic_daily_chart(req: web.Request) -> web.Response:
    code = req.rel_url.query.get("FID_INPUT_ISCD", "005930")
    rows = _OHLCV_CACHE.get(code, [])
    price = _latest_price(code)
    return _json(_ok(
        output1={
            "stck_prpr": str(int(price)),
            "hts_kor_isnm": next((s["name"] for s in DOMESTIC_STOCKS if s["code"] == code), code),
        },
        output2=rows[-100:],
    ))


async def domestic_minute_chart(req: web.Request) -> web.Response:
    code = req.rel_url.query.get("FID_INPUT_ISCD", "005930")
    price = _latest_price(code)
    now = datetime.now()
    rows = []
    for i in range(30):
        t = now - timedelta(minutes=i)
        p = price * (1 + random.uniform(-0.003, 0.003))
        rows.append({
            "stck_bsop_date": t.strftime("%Y%m%d"),
            "stck_cntg_hour": t.strftime("%H%M%S"),
            "stck_oprc":      str(int(p * 0.999)),
            "stck_hgpr":      str(int(p * 1.002)),
            "stck_lwpr":      str(int(p * 0.997)),
            "stck_prpr":      str(int(p)),
            "cntg_vol":       str(random.randint(10_000, 500_000)),
        })
    return _json(_ok(output1={"stck_prpr": str(int(price))}, output2=rows))


async def domestic_hist_minute(req: web.Request) -> web.Response:
    """과거 분봉 커서 방식 페이지네이션 (inquire-time-dailychartprice).

    domestic.py._fetch_minute_range이 cursor 기반으로 페이징하므로,
    cursor_dt부터 과거 방향으로 200분치 1분봉을 한 번에 반환한다.
    """
    code = req.rel_url.query.get("FID_INPUT_ISCD", "005930")
    cursor_date = req.rel_url.query.get("FID_INPUT_DATE_1", date.today().strftime("%Y%m%d"))
    cursor_hour = req.rel_url.query.get("FID_INPUT_HOUR_1", "160000")

    stock = next((s for s in DOMESTIC_STOCKS if s["code"] == code), None)
    base_price = stock["base_price"] if stock else 50_000
    sigma = stock["sigma"] if stock else 0.015

    try:
        cursor_dt = datetime.strptime(cursor_date + cursor_hour, "%Y%m%d%H%M%S")
    except ValueError:
        cursor_dt = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)

    rows = []
    t = cursor_dt
    filled = 0
    iters = 0
    price = base_price

    while filled < 200 and iters < 700:
        iters += 1
        if t.weekday() >= 5:
            t -= timedelta(days=1)
            t = t.replace(hour=15, minute=30, second=0, microsecond=0)
            continue
        h, m = t.hour, t.minute
        if h < 9 or (h == 9 and m == 0) or h > 15 or (h == 15 and m > 30):
            if h < 9:
                t -= timedelta(days=1)
                t = t.replace(hour=15, minute=30, second=0, microsecond=0)
            else:
                t = t.replace(hour=15, minute=30, second=0, microsecond=0)
            continue

        price = price * (1 + sigma * 0.3 * random.gauss(0, 1))
        p_open  = int(price * (1 + random.uniform(-0.001, 0.001)))
        p_close = int(price)
        p_high  = max(p_open, p_close, int(price * (1 + abs(random.uniform(0, 0.002)))))
        p_low   = min(p_open, p_close, int(price * (1 - abs(random.uniform(0, 0.002)))))

        rows.append({
            "stck_bsop_date": t.strftime("%Y%m%d"),
            "stck_cntg_hour": t.strftime("%H%M%S"),
            "stck_oprc":      str(p_open),
            "stck_hgpr":      str(p_high),
            "stck_lwpr":      str(p_low),
            "stck_prpr":      str(p_close),
            "cntg_vol":       str(random.randint(10_000, 500_000)),
        })
        t -= timedelta(minutes=1)
        filled += 1

    return _json(_ok(output1={"stck_prpr": str(int(base_price))}, output2=rows))


async def domestic_volume_rank(req: web.Request) -> web.Response:
    output = []
    for s in DOMESTIC_STOCKS:
        price = _latest_price(s["code"])
        rows = _OHLCV_CACHE.get(s["code"], [])
        prev = float(rows[-2]["stck_clpr"]) if len(rows) >= 2 else price
        chg = round((price - prev) / prev * 100, 2) if prev else 0
        output.append({
            "mksc_shrn_iscd": s["code"],
            "hts_kor_isnm":   s["name"],
            "stck_prpr":      str(int(price)),
            "prdy_ctrt":      str(chg),
            "acml_vol":       str(random.randint(5_000_000, 80_000_000)),
            "acml_tr_pbmn":   str(int(price * random.randint(5_000_000, 80_000_000))),
        })
    return _json(_ok(output=output))


# 국내 주문
async def domestic_order(req: web.Request) -> web.Response:
    body = await req.json()
    order_no = _next_order_no()
    now = datetime.now()
    order = {
        "type": "domestic",
        "tr_id": req.headers.get("tr_id", ""),
        "code": body.get("PDNO"),
        "qty":  body.get("ORD_QTY"),
        "price": body.get("ORD_UNPR"),
        "order_no": order_no,
        "time": now.strftime("%H%M%S"),
    }
    _orders.append(order)
    asyncio.create_task(_broadcast_fill_notice(order))
    logger.info("주문 접수: %s %s주 @ %s (번호:%s)",
                body.get("PDNO"), body.get("ORD_QTY"), body.get("ORD_UNPR"), order_no)
    return _json(_ok(output={
        "KRX_FWDG_ORD_ORGNO": "06010",
        "ODNO": order_no,
        "ORD_TMD": now.strftime("%H%M%S"),
    }))


# 국내 잔고
async def domestic_balance(req: web.Request) -> web.Response:
    return _json(_ok(
        output1=[],
        output2=[{
            "dnca_tot_amt":  str(int(_cash_krw)),
            "tot_evlu_amt":  str(int(_cash_krw)),
            "nass_amt":      str(int(_cash_krw)),
            "pchs_amt_smtl_amt": "0",
            "evlu_amt_smtl_amt": "0",
            "evlu_pfls_smtl_amt": "0",
        }],
    ))


# 국내 일별 체결 내역
async def domestic_daily_ccld(req: web.Request) -> web.Response:
    return _json(_ok(output1=_orders[-20:], output2=[]))


# 테스트용: 주문 내역 전체 조회
async def get_orders(req: web.Request) -> web.Response:
    return _json({"orders": _orders, "count": len(_orders)})


# 해외 시세
async def overseas_price(req: web.Request) -> web.Response:
    code = req.rel_url.query.get("SYMB", "AAPL")
    price = _latest_price(code)
    rows = _OHLCV_CACHE.get(code, [])
    prev = float(rows[-2]["stck_clpr"]) if len(rows) >= 2 else price
    diff = round(price - prev, 2)
    rate = round(diff / prev * 100, 2) if prev else 0
    return _json(_ok(output={
        "last": f"{price:.2f}",
        "sign": "2" if diff >= 0 else "5",
        "diff": f"{diff:.2f}",
        "rate": f"{rate:.2f}",
        "tvol": str(random.randint(10_000_000, 200_000_000)),
        "tamt": f"{price * random.randint(10_000_000, 200_000_000):.0f}",
        "ordy": rows[-1]["stck_oprc"] if rows else f"{price:.2f}",
    }))


async def overseas_daily_chart(req: web.Request) -> web.Response:
    code = req.rel_url.query.get("SYMB", "AAPL")
    rows = _OHLCV_CACHE.get(code, [])
    # 해외 포맷으로 변환
    output2 = [{
        "xymd":  r["stck_bsop_date"],
        "open":  r["stck_oprc"],
        "high":  r["stck_hgpr"],
        "low":   r["stck_lwpr"],
        "clos":  r["stck_clpr"],
        "tvol":  r["acml_vol"],
        "tamt":  r["acml_tr_pbmn"],
    } for r in rows[-100:]]
    return _json(_ok(output1={}, output2=output2))


async def overseas_volume_rank(req: web.Request) -> web.Response:
    output = []
    for s in OVERSEAS_STOCKS:
        price = _latest_price(s["code"])
        rows = _OHLCV_CACHE.get(s["code"], [])
        prev = float(rows[-2]["stck_clpr"]) if len(rows) >= 2 else price
        rate = round((price - prev) / prev * 100, 2) if prev else 0
        output.append({
            "symb": s["code"],
            "name": s["name"],
            "last": f"{price:.2f}",
            "rate": f"{rate:.2f}",
            "tvol": str(random.randint(10_000_000, 200_000_000)),
        })
    return _json(_ok(output=output))


async def overseas_order(req: web.Request) -> web.Response:
    body = await req.json()
    order_no = _next_order_no()
    now = datetime.now()
    order = {
        "type": "overseas",
        "tr_id": req.headers.get("tr_id", ""),
        "code": body.get("PDNO"),
        "qty":  body.get("ORD_QTY"),
        "price": body.get("OVRS_ORD_UNPR"),
        "exchange": body.get("OVRS_EXCG_CD"),
        "order_no": order_no,
        "time": now.strftime("%H%M%S"),
    }
    _orders.append(order)
    asyncio.create_task(_broadcast_fill_notice(order))
    logger.info("해외 주문: %s %s주 @ %s (번호:%s)",
                body.get("PDNO"), body.get("ORD_QTY"), body.get("OVRS_ORD_UNPR"), order_no)
    return _json(_ok(output={"ODNO": order_no, "ORD_TMD": now.strftime("%H%M%S")}))


async def overseas_balance(req: web.Request) -> web.Response:
    return _json(_ok(
        output1=[],
        output2=[{
            "tot_asst_amt": f"{_cash_usd:.2f}",
            "frcr_dncl_amt_2": f"{_cash_usd:.2f}",
        }],
    ))


# ── WebSocket 핸들러 ──────────────────────────────────────────────────

# 구독 정보: ws_id → [(tr_id, stock_code)]
_ws_subscriptions: dict[int, list[tuple[str, str]]] = {}
_ws_clients: dict[int, web.WebSocketResponse] = {}
_ws_id_counter = 0


async def websocket_handler(req: web.Request) -> web.WebSocketResponse:
    global _ws_id_counter
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(req)

    ws_id = _ws_id_counter
    _ws_id_counter += 1
    _ws_subscriptions[ws_id] = []
    _ws_clients[ws_id] = ws
    logger.info("WebSocket 연결 [%d]", ws_id)

    # 실시간 가격 전송 태스크
    price_task = asyncio.create_task(_price_sender(ws, ws_id))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await _handle_ws_msg(ws, ws_id, msg.data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        price_task.cancel()
        _ws_subscriptions.pop(ws_id, None)
        _ws_clients.pop(ws_id, None)
        logger.info("WebSocket 종료 [%d]", ws_id)

    return ws


async def _handle_ws_msg(ws: web.WebSocketResponse, ws_id: int, raw: str):
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return

    header = msg.get("header", {})
    tr_id = msg.get("body", {}).get("input", {}).get("tr_id", "")
    tr_key = msg.get("body", {}).get("input", {}).get("tr_key", "")
    tr_type = header.get("tr_type", "1")

    if tr_type == "1":  # 구독
        _ws_subscriptions[ws_id].append((tr_id, tr_key))
        logger.info("구독 [%d]: %s %s", ws_id, tr_id, tr_key)
        # 구독 성공 응답 (encrypt=N → iv/key 불필요)
        await ws.send_str(json.dumps({
            "header": {"tr_id": tr_id, "tr_key": tr_key, "encrypt": "N"},
            "body": {
                "rt_cd": "0",
                "msg_cd": "OPSP0000",
                "msg1": f"SUBSCRIBE SUCCESS",
                "output": {"iv": "", "key": ""},
            },
        }))
    elif tr_type == "2":  # 구독 해제
        _ws_subscriptions[ws_id] = [
            (t, k) for t, k in _ws_subscriptions[ws_id]
            if not (t == tr_id and k == tr_key)
        ]
        await ws.send_str(json.dumps({
            "header": {"tr_id": tr_id},
            "body": {"rt_cd": "0", "msg1": f"UNSUBSCRIBE SUCCESS"},
        }))


async def _price_sender(ws: web.WebSocketResponse, ws_id: int):
    """
    구독된 종목에 실시간 가격 데이터 전송.

    가속 시뮬레이션 시계 사용: 실제 200ms = 시뮬레이션 1분
    → 30초면 150분봉 생성 → breakout/pullback 전략 진입 조건 충족 가능
    """
    tick_count: dict[str, int] = {}
    price_state: dict[str, float] = {}  # 종목별 현재 가격 (연속적)

    # 시뮬레이션 시계: 09:01부터 1틱=1분 증가
    sim_date = date.today()
    sim_minute = 9 * 60 + 1   # 09:01 = 541분

    ping_counter = 0

    while not ws.closed:
        await asyncio.sleep(0.2)   # 200ms/tick = 5ticks/sec = 5 simulated min/sec
        ping_counter += 1

        # 시뮬레이션 시각 계산
        h   = (sim_minute % (24 * 60)) // 60
        m   = (sim_minute % (24 * 60)) % 60
        sim_dt = datetime(sim_date.year, sim_date.month, sim_date.day, h, m, 0)

        # 15:30 초과 시 다음날 09:01로 리셋
        if h > 15 or (h == 15 and m > 30):
            sim_date  = sim_date + timedelta(days=1)
            sim_minute = 9 * 60 + 1
            sim_dt    = datetime(sim_date.year, sim_date.month, sim_date.day, 9, 1, 0)

        sim_minute += 1

        for tr_id, code in list(_ws_subscriptions.get(ws_id, [])):
            symbol = _handler_symbol(tr_id, code)
            price_base = _latest_price(symbol)
            if price_base <= 0:
                continue

            count = tick_count.get(symbol, 0)
            tick_count[symbol] = count + 1

            prev_price = price_state.get(symbol, price_base)

            # 처음 20틱: 완만한 상승 (진입 조건 형성)
            # 20~50틱: 강한 상승 (돌파/눌림 진입 유도)
            # 50~70틱: 5% 급등 (익절 유도)
            # 이후: 완만한 등락
            if count < 20:
                drift = random.uniform(0.0005, 0.002)
            elif count < 50:
                drift = random.uniform(0.001, 0.004)
            elif count < 70:
                drift = random.uniform(0.003, 0.007)
            else:
                drift = random.uniform(-0.003, 0.003)

            price = prev_price * (1 + drift)
            price = max(price, price_base * 0.88)
            price_state[symbol] = price

            if tr_id == "H0STCNT0":
                fields = _make_domestic_tick(symbol, price, price_base, sim_dt)
            elif tr_id == "HDFSCNT0":
                fields = _make_overseas_tick(symbol, price, price_base, sim_dt)
            else:
                continue

            data_msg = f"0|{tr_id}|1|{''.join(fields)}"
            try:
                await ws.send_str(data_msg)
            except Exception:
                return

        # 150틱(30초)마다 PINGPONG
        if ping_counter % 150 == 0:
            try:
                await ws.send_str(json.dumps({"header": {"tr_id": "PINGPONG"}}))
            except Exception:
                return


def _handler_symbol(tr_id: str, tr_key: str) -> str:
    if tr_id == "HDFSCNT0" and len(tr_key) > 4 and tr_key[0] in ("D", "R"):
        return tr_key[4:]
    return tr_key


async def _broadcast_fill_notice(order: dict):
    await asyncio.sleep(0.1)
    tr_id = _fill_tr_id(order)
    payload = (
        _make_domestic_fill(order)
        if order.get("type") == "domestic"
        else _make_overseas_fill(order)
    )
    data_msg = f"0|{tr_id}|1|{payload}"

    for ws_id, ws in list(_ws_clients.items()):
        if ws.closed:
            continue
        subscribed = any(t == tr_id for t, _ in _ws_subscriptions.get(ws_id, []))
        if not subscribed:
            continue
        try:
            await ws.send_str(data_msg)
            logger.info("체결통보 전송 [%d]: %s %s %s주 @ %s",
                        ws_id, tr_id, order.get("code"), order.get("qty"), _order_fill_price(order))
        except Exception:
            logger.exception("체결통보 전송 실패 [%d]", ws_id)


def _make_domestic_fill(order: dict) -> str:
    side_code = "02" if _is_buy_tr(order.get("tr_id", "")) else "01"
    code = order.get("code", "")
    name = next((s["name"] for s in DOMESTIC_STOCKS if s["code"] == code), code)
    fields = [
        "mockuser",                 # 0 CUST_ID
        "1234567801",              # 1 ACNT_NO
        order.get("order_no", ""), # 2 ODER_NO
        "",                        # 3 OODER_NO
        side_code,                 # 4 SELN_BYOV_CLS
        "00",                      # 5 RCTF_CLS
        "01",                      # 6 ODER_KIND
        "",                        # 7 ODER_COND
        code,                      # 8 STCK_SHRN_ISCD
        str(order.get("qty") or "0"),       # 9 CNTG_QTY
        _order_fill_price(order),           # 10 CNTG_UNPR
        order.get("time", datetime.now().strftime("%H%M%S")),  # 11 STCK_CNTG_HOUR
        "N",                       # 12 RFUS_YN
        "2",                       # 13 CNTG_YN
        "Y",                       # 14 ACPT_YN
        "06010",                   # 15 BRNC_NO
        str(order.get("qty") or "0"),       # 16 ODER_QTY
        "mock",                    # 17 ACNT_NAME
        "",                        # 18 ORD_COND_PRC
        "KRX",                     # 19 ORD_EXG_GB
        "N",                       # 20 POPUP_YN
        "",                        # 21 FILLER
        "",                        # 22 CRDT_CLS
        "",                        # 23 CRDT_LOAN_DATE
        name,                      # 24 CNTG_ISNM40
        _order_fill_price(order),  # 25 ODER_PRC
    ]
    return "^".join(fields)


def _make_overseas_fill(order: dict) -> str:
    side_code = "02" if _is_buy_tr(order.get("tr_id", "")) else "01"
    code = order.get("code", "")
    name = next((s["name"] for s in OVERSEAS_STOCKS if s["code"] == code), code)
    fill_price = _order_fill_price(order)
    fields = [
        "mockuser",                 # 0 CUST_ID
        "1234567801",              # 1 ACNT_NO
        order.get("order_no", ""), # 2 ODER_NO
        "",                        # 3 OODER_NO
        side_code,                 # 4 SELN_BYOV_CLS
        "00",                      # 5 RCTF_CLS
        "01",                      # 6 ODER_KIND2
        code,                      # 7 STCK_SHRN_ISCD
        str(order.get("qty") or "0"),       # 8 CNTG_QTY
        fill_price,                # 9 CNTG_UNPR
        order.get("time", datetime.now().strftime("%H%M%S")),  # 10 STCK_CNTG_HOUR
        "N",                       # 11 RFUS_YN
        "2",                       # 12 CNTG_YN
        "Y",                       # 13 ACPT_YN
        "06010",                   # 14 BRNC_NO
        "",                        # 15 filler
        str(order.get("qty") or "0"),       # 16 ODER_QTY
        name,                      # 17 CNTG_ISNM
        "",                        # 18 ODER_COND
        "",                        # 19 DEBT_GB
        "",                        # 20 DEBT_DATE
        "",                        # 21 START_TM
        "",                        # 22 END_TM
        "",                        # 23 TM_DIV_TP
        fill_price,                # 24 CNTG_UNPR12
    ]
    return "^".join(fields)

def _make_domestic_tick(code: str, price: float, base: float, now: datetime) -> list[str]:
    change = int(price - base)
    sign = "2" if change >= 0 else "5"
    fields = [
        code,                              # 0: 종목코드
        now.strftime("%H%M%S"),            # 1: 체결시간
        str(int(price)),                   # 2: 현재가
        sign,                              # 3: 전일대비부호
        str(abs(change)),                  # 4: 전일대비
        f"{abs(change/base*100):.2f}",     # 5: 전일대비율
        str(int(price * 0.999)),           # 6: 가중평균가
        str(int(base * 1.001)),            # 7: 시가
        str(int(base * 1.015)),            # 8: 최고가
        str(int(base * 0.988)),            # 9: 최저가
        str(int(price * 1.001)),           # 10: 매도호가1
        str(int(price * 0.999)),           # 11: 매수호가1
        str(random.randint(1000, 50000)),  # 12: 체결거래량
        str(random.randint(5_000_000, 80_000_000)),  # 13: 누적거래량
        str(int(price * random.randint(5_000_000, 80_000_000))),  # 14: 누적거래대금
    ]
    return ["^".join(fields)]


def _make_overseas_tick(code: str, price: float, base: float, now: datetime) -> list[str]:
    diff = round(price - base, 2)
    sign = "2" if diff >= 0 else "5"
    fields = [
        code,
        now.strftime("%Y%m%d"),
        now.strftime("%H%M%S"),
        f"{price:.2f}",
        sign,
        f"{abs(diff):.2f}",
        f"{abs(diff/base*100):.2f}",
        str(random.randint(100_000, 5_000_000)),
        f"{price * random.randint(100_000, 5_000_000):.0f}",
    ]
    return ["^".join(fields)]


# ── 앱 구성 ──────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()

    app.router.add_post("/oauth2/tokenP", token_issue)
    app.router.add_post("/oauth2/Approval", ws_approval)

    # 국내주식
    app.router.add_get("/uapi/domestic-stock/v1/quotations/inquire-price",                domestic_price)
    app.router.add_get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", domestic_daily_chart)
    app.router.add_get("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",  domestic_minute_chart)
    app.router.add_get("/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice", domestic_hist_minute)
    app.router.add_get("/uapi/domestic-stock/v1/quotations/volume-rank",                  domestic_volume_rank)
    app.router.add_post("/uapi/domestic-stock/v1/trading/order-cash",                     domestic_order)
    app.router.add_get("/uapi/domestic-stock/v1/trading/inquire-balance",                 domestic_balance)
    app.router.add_get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld",              domestic_daily_ccld)

    # 해외주식 (overseas-price prefix for quotations, overseas-stock for trading/ranking)
    app.router.add_get("/uapi/overseas-price/v1/quotations/price",               overseas_price)
    app.router.add_get("/uapi/overseas-price/v1/quotations/dailyprice",          overseas_daily_chart)
    app.router.add_get("/uapi/overseas-stock/v1/ranking/trade-vol",              overseas_volume_rank)
    app.router.add_post("/uapi/overseas-stock/v1/trading/order",                 overseas_order)
    app.router.add_get("/uapi/overseas-stock/v1/trading/inquire-balance",        overseas_balance)

    # WebSocket
    app.router.add_get("/ws", websocket_handler)

    # 테스트 유틸
    app.router.add_get("/test/orders", get_orders)

    return app


if __name__ == "__main__":
    print("=" * 55)
    print(" KIS Mock Server")
    print(" REST  : http://localhost:8000")
    print(" WS    : ws://localhost:8000/ws")
    print("=" * 55)
    print(" 테스트 실행:")
    print("   python main.py --mode analysis --config config_mock.yaml")
    print("   python main.py --mode trade    --config config_mock.yaml")
    print("   python main.py --mode report   --config config_mock.yaml")
    print("=" * 55)

    app = create_app()
    web.run_app(app, host="localhost", port=8000, access_log=None)
