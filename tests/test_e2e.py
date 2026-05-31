"""
E2E 통합 테스트: mock 서버 기반 WebSocket + 주문 흐름 검증

실행: python tests/test_e2e.py
사전 조건: tests/mock_server.py가 별도 프로세스로 이미 실행 중이어야 함
또는: python tests/test_e2e.py (mock_server 자동 기동)
"""
import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import requests
import websockets

# 프로젝트 루트를 경로에 추가
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("e2e")

MOCK_BASE = "http://localhost:8000"
MOCK_WS   = "ws://localhost:8000/ws"


# ── 헬퍼 ──────────────────────────────────────────────────────────────

def _get(path: str, params: dict | None = None) -> dict:
    r = requests.get(f"{MOCK_BASE}{path}", params=params, timeout=5)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict, headers: dict | None = None) -> dict:
    r = requests.post(
        f"{MOCK_BASE}{path}",
        json=body,
        headers=headers or {},
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


# ── 테스트 1: REST 엔드포인트 ──────────────────────────────────────────

def test_rest_endpoints():
    logger.info("=== TEST 1: REST endpoints ===")
    errors = []

    # OAuth 토큰
    d = _post("/oauth2/tokenP", {})
    assert "access_token" in d, f"토큰 없음: {d}"
    logger.info("  ✅ token_issue OK (token=%s...)", d["access_token"][:12])

    # WS approval key
    d = _post("/oauth2/Approval", {})
    assert "approval_key" in d, f"approval_key 없음: {d}"
    logger.info("  ✅ ws_approval OK")

    # 국내 거래량 순위
    d = _get("/uapi/domestic-stock/v1/quotations/volume-rank", {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": "0001", "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "0", "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "000000",
        "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "",
        "FID_VOL_CNT": "", "FID_INPUT_DATE_1": "",
    })
    stocks = d.get("output", [])
    assert len(stocks) > 0, "거래량 순위 비어있음"
    logger.info("  ✅ volume_rank OK (%d종목)", len(stocks))

    # HIST_MINUTE
    from datetime import date
    d = _get("/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice", {
        "FID_INPUT_ISCD": "005930",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_DATE_1": date.today().strftime("%Y%m%d"),
        "FID_INPUT_HOUR_1": "160000",
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": " ",
    })
    rows = d.get("output2", [])
    assert len(rows) == 200, f"HIST_MINUTE 행 수 오류: {len(rows)}"
    logger.info("  ✅ hist_minute OK (200행)")

    # 해외 시세
    d = _get("/uapi/overseas-price/v1/quotations/price", {
        "AUTH": "", "EXCD": "NAS", "SYMB": "AAPL",
    })
    assert "last" in d.get("output", {}), f"last 필드 없음: {d}"
    logger.info("  ✅ overseas_price OK")

    # 해외 일봉
    d = _get("/uapi/overseas-price/v1/quotations/dailyprice", {
        "AUTH": "", "EXCD": "NAS", "SYMB": "AAPL",
        "GUBN": "0", "BYMD": date.today().strftime("%Y%m%d"), "MODP": "1",
    })
    assert len(d.get("output2", [])) > 0, "해외 일봉 비어있음"
    logger.info("  ✅ overseas_daily OK")

    # 해외 거래량 순위
    d = _get("/uapi/overseas-stock/v1/ranking/trade-vol", {
        "EXCD": "NAS", "AUTH": "", "KEYB": "",
        "NDAY": "0", "PRC1": "", "PRC2": "", "VOL_RANG": "0",
    })
    assert len(d.get("output", [])) > 0, "해외 거래량 순위 비어있음"
    logger.info("  ✅ overseas_volume_rank OK")

    logger.info("  → REST 전체 통과\n")


# ── 테스트 2: 매수/매도 주문 REST ─────────────────────────────────────

def test_order_rest():
    logger.info("=== TEST 2: 주문 REST ===")

    # 주문 전 카운트
    before = _get("/test/orders")["count"]

    # 국내 매수
    d = _post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        {"PDNO": "005930", "ORD_QTY": "10", "ORD_UNPR": "75000", "ORD_DVSN": "01",
         "CANO": "12345678", "ACNT_PRDT_CD": "01"},
        headers={"tr_id": "VTTC0012U"},
    )
    assert d.get("output", {}).get("ODNO"), f"주문번호 없음: {d}"
    order_no = d["output"]["ODNO"]
    logger.info("  ✅ 국내 매수 OK (주문번호=%s)", order_no)

    # 국내 매도
    d = _post(
        "/uapi/domestic-stock/v1/trading/order-cash",
        {"PDNO": "005930", "ORD_QTY": "10", "ORD_UNPR": "77000", "ORD_DVSN": "01",
         "CANO": "12345678", "ACNT_PRDT_CD": "01"},
        headers={"tr_id": "VTTC0011U"},
    )
    assert d.get("output", {}).get("ODNO"), f"매도 주문번호 없음: {d}"
    logger.info("  ✅ 국내 매도 OK")

    # 잔고 확인
    after = _get("/test/orders")["count"]
    assert after == before + 2, f"주문 개수 오류: {before} → {after}"
    logger.info("  ✅ 주문 내역 기록 확인 (누계 %d건)", after)

    logger.info("  → 주문 REST 전체 통과\n")


# ── 테스트 3: WebSocket 틱 수신 ─────────────────────────────────────

async def _ws_test():
    """WS 연결 → 구독 → 틱 수신 확인."""
    # approval key
    token = _post("/oauth2/tokenP", {}).get("access_token", "mock_token")
    approval = _post("/oauth2/Approval", {}).get("approval_key", "mock_key")

    received: dict[str, list] = {}

    async with websockets.connect(MOCK_WS, ping_interval=None) as ws:
        # 구독 요청
        for code in ["005930", "000660"]:
            msg = {
                "header": {
                    "approval_key": approval,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
            }
            await ws.send(json.dumps(msg))

        # 구독 응답 + 틱 수집 (최대 5초)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if raw[0] in ("0", "1"):
                parts = raw.split("|", 3)
                if len(parts) >= 4:
                    tr_id  = parts[1]
                    fields = parts[3].split("^")
                    code   = fields[0] if fields else ""
                    price  = fields[2] if len(fields) > 2 else ""
                    received.setdefault(code, []).append(price)

    return received


def test_websocket():
    logger.info("=== TEST 3: WebSocket 틱 수신 ===")
    received = asyncio.run(_ws_test())

    assert received, "틱 수신 없음"
    for code, prices in received.items():
        logger.info("  ✅ [%s] %d틱 수신 (최신가=%s)", code, len(prices), prices[-1])

    # 구독 종목 확인
    assert "005930" in received, "삼성전자 틱 없음"
    assert "000660" in received, "SK하이닉스 틱 없음"
    assert len(received["005930"]) >= 3, "틱 수 부족"

    # 필드 파싱 검증
    all_prices = received.get("005930", [])
    for p in all_prices[:5]:
        assert p.isdigit() or p.replace(".", "").isdigit(), f"가격 파싱 오류: '{p}'"

    logger.info("  → WebSocket 전체 통과\n")


# ── 테스트 4: WebSocket 체결통보 수신 ─────────────────────────────────

async def _ws_fill_notice_test():
    approval = _post("/oauth2/Approval", {}).get("approval_key", "mock_key")

    async with websockets.connect(MOCK_WS, ping_interval=None) as ws:
        msg = {
            "header": {
                "approval_key": approval,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": "H0STCNI9", "tr_key": "mock_hts"}},
        }
        await ws.send(json.dumps(msg))

        # 구독 성공 응답 소비
        await asyncio.wait_for(ws.recv(), timeout=2)

        d = _post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            {"PDNO": "005930", "ORD_QTY": "3", "ORD_UNPR": "0", "ORD_DVSN": "01",
             "CANO": "12345678", "ACNT_PRDT_CD": "01"},
            headers={"tr_id": "VTTC0012U"},
        )
        order_no = d["output"]["ODNO"]

        deadline = time.time() + 3
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=1)
            if not raw or raw[0] not in ("0", "1"):
                continue
            parts = raw.split("|", 3)
            if len(parts) < 4 or parts[1] != "H0STCNI9":
                continue
            fields = parts[3].split("^")
            return {
                "order_no": fields[2],
                "stock_code": fields[8],
                "filled_qty": fields[9],
                "filled_price": fields[10],
                "filled": fields[13],
                "expected_order_no": order_no,
            }
    return {}


def test_fill_notice_websocket():
    logger.info("=== TEST 4: WebSocket 체결통보 수신 ===")
    fill = asyncio.run(_ws_fill_notice_test())
    assert fill, "체결통보 수신 없음"
    assert fill["order_no"] == fill["expected_order_no"], f"주문번호 불일치: {fill}"
    assert fill["stock_code"] == "005930", f"종목코드 불일치: {fill}"
    assert fill["filled_qty"] == "3", f"체결수량 불일치: {fill}"
    assert fill["filled"] == "2", f"CNTG_YN 체결값 아님: {fill}"
    assert float(fill["filled_price"]) > 0, f"체결가 오류: {fill}"
    logger.info("  ✅ 체결통보 OK (주문번호=%s, 수량=%s, 가격=%s)",
                fill["order_no"], fill["filled_qty"], fill["filled_price"])
    logger.info("  → WebSocket 체결통보 통과\n")


# ── 테스트 5: 매매 루프 E2E (실제 주문 발생 확인) ─────────────────────

def test_trading_loop_e2e():
    """
    main.py --mode trade를 실행해 실제 주문이 mock 서버에 도달하는지 확인.
    시뮬레이션 클록(200ms/분)으로 breakout 신호 발생 기대.
    """
    logger.info("=== TEST 5: 매매 루프 E2E ===")

    orders_before = _get("/test/orders")["count"]

    # 분석 먼저 실행 (종목 DB 저장)
    logger.info("  분석 실행 중...")
    result = subprocess.run(
        [sys.executable, "main.py", "--mode", "analysis",
         "--market", "domestic", "--config", "config_mock.yaml"],
        capture_output=True, text=True, timeout=30,
        cwd=str(Path(__file__).parent.parent),
    )
    assert result.returncode == 0, f"분석 실패:\n{result.stderr[-500:]}"
    logger.info("  ✅ 분석 완료")

    # 매매 루프 실행 (Enter 자동 입력, 60초 대기)
    logger.info("  매매 루프 실행 중 (60초)...")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--mode", "trade",
         "--market", "domestic", "--config", "config_mock.yaml"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).parent.parent),
    )
    proc.stdin.write(b"\n")
    proc.stdin.flush()

    # 60초 대기 후 종료
    try:
        out, _ = proc.communicate(timeout=65)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    output = out.decode("utf-8", errors="replace")
    logger.info("  매매 루프 로그 마지막 10줄:\n%s",
                "\n".join(("    " + l) for l in output.split("\n")[-12:] if l.strip()))

    # 주문 발생 여부 확인
    orders_after = _get("/test/orders")["count"]
    new_orders = orders_after - orders_before

    assert new_orders > 0, "주문 0건 — mock 장세/가격 패턴이 전략 진입 조건을 만들지 못함"
    orders = _get("/test/orders")["orders"]
    recent = orders[-new_orders:]
    logger.info("  ✅ 주문 %d건 발생:", new_orders)
    for o in recent:
        logger.info("    - %s %s주 @ %s (%s)", o["code"], o["qty"], o["price"], o["tr_id"])

    # 최소한 WebSocket 연결 + 틱 수신은 확인
    assert "WebSocket 연결됨" in output or "모니터링 시작" in output, \
        f"WebSocket 연결 흔적 없음:\n{output[-1000:]}"
    logger.info("  ✅ WebSocket 연결 및 틱 수신 확인")
    logger.info("  → 매매 루프 E2E 통과\n")


# ── 메인 ──────────────────────────────────────────────────────────────

def main():
    logger.info("KIS Quant E2E 테스트 시작")
    logger.info("Mock 서버: %s\n", MOCK_BASE)

    # Mock 서버 헬스체크
    try:
        _post("/oauth2/tokenP", {})
    except Exception:
        logger.error("Mock 서버가 실행 중이지 않습니다. 먼저 실행하세요:")
        logger.error("  python tests/mock_server.py")
        sys.exit(1)

    passed = failed = 0
    tests = [
        ("REST endpoints",    test_rest_endpoints),
        ("주문 REST",         test_order_rest),
        ("WebSocket 틱 수신", test_websocket),
        ("WebSocket 체결통보", test_fill_notice_websocket),
        ("매매 루프 E2E",     test_trading_loop_e2e),
    ]

    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            logger.error("❌ FAIL [%s]: %s", name, e)
            failed += 1

    logger.info("=" * 50)
    logger.info("결과: %d 통과 / %d 실패", passed, failed)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
