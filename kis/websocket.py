import json
import asyncio
import logging
from datetime import datetime
from base64 import b64decode
from typing import Callable

import websockets
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .constants import WebSocketTRID

logger = logging.getLogger(__name__)

_MSG_REALTIME = ("0", "1")

# KIS WebSocket TR별 레코드당 필드 수 (공식 문서 기준)
_KNOWN_FIELD_COUNTS: dict[str, int] = {
    "H0STCNT0": 46,   # 국내주식 실시간체결가
    "H0STASP0": 57,   # 국내주식 실시간호가
    "HDFSCNT0": 26,   # 해외주식 실시간체결가
}


def aes_cbc_base64_dec(key: str, iv: str, cipher_text: str) -> str:
    """AES-CBC + Base64 복호화. encrypt=Y 데이터에 사용."""
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return bytes.decode(unpad(cipher.decrypt(b64decode(cipher_text)), AES.block_size))


class KISWebSocket:
    """
    KIS 실시간 WebSocket 클라이언트.

    메시지 포맷 (공식 문서 기준):
      데이터:  {0|1}|{TR_ID}|{COUNT}|{DATA^DATA^...}
      시스템:  JSON {"header": {"tr_id": "...", "encrypt": "N|Y"}, "body": {...}}
    """

    def __init__(self, auth):
        self.auth = auth
        self.ws_url = auth.ws_full_url
        self._running = False
        self._ws = None           # 활성 WebSocket 연결 (동적 구독에 사용)
        self._approval_key = None

        # tr_id → {columns, encrypt, key, iv}
        self._data_map: dict[str, dict] = {}
        # "{tr_id}:{stock_code}" 또는 "{tr_id}:*" → Callable
        self._handlers: dict[str, Callable] = {}
        # 구독 목록: [(tr_id, stock_code)]
        self._subscriptions: list[tuple[str, str]] = []
        self.last_message_at: str | None = None
        self.last_realtime_at: str | None = None
        self.last_realtime_sample: str | None = None
        self.last_unmatched_key: str | None = None
        self.realtime_count = 0
        self.unmatched_count = 0

    def subscribe(self, tr_id: WebSocketTRID | str, stock_code: str, handler: Callable):
        """실시간 데이터 구독 등록. handler(tr_id, fields: list[str])"""
        handler_code = self._handler_code(tr_id, stock_code)
        key = f"{tr_id}:{handler_code}"
        if key not in self._handlers:
            self._subscriptions.append((tr_id, stock_code))
        self._handlers[key] = handler
        self._data_map.setdefault(str(tr_id), {"columns": [], "encrypt": "N", "key": None, "iv": None, "field_count": _KNOWN_FIELD_COUNTS.get(str(tr_id))})

    def subscribe_global(self, tr_id: WebSocketTRID | str, tr_key: str, handler: Callable):
        """종목코드가 첫 필드가 아닌 계좌 단위 통보 구독."""
        key = f"{tr_id}:*"
        if key not in self._handlers:
            self._subscriptions.append((tr_id, tr_key))
        self._handlers[key] = handler
        self._data_map.setdefault(str(tr_id), {"columns": [], "encrypt": "N", "key": None, "iv": None, "field_count": _KNOWN_FIELD_COUNTS.get(str(tr_id))})

    @staticmethod
    def _handler_code(tr_id: WebSocketTRID | str, stock_code: str) -> str:
        # 해외 실시간 구독키는 D/R + 거래소 3자리 + 심볼(DNASAAPL) 형식이고,
        # 수신 payload의 첫 필드는 심볼(AAPL)이다.
        if str(tr_id) == str(WebSocketTRID.OVERSEAS_PRICE):
            if len(stock_code) > 4 and stock_code[0] in ("D", "R"):
                return stock_code[4:]
        return stock_code

    def unsubscribe(self, tr_id: WebSocketTRID | str, stock_code: str):
        self._subscriptions = [
            (t, s) for t, s in self._subscriptions
            if not (t == tr_id and s == stock_code)
        ]
        self._handlers.pop(f"{tr_id}:{stock_code}", None)

    async def add_live_subscription(self, tr_id: WebSocketTRID | str, stock_code: str, handler: Callable) -> bool:
        """연결 중인 WebSocket에 실시간으로 구독 추가. 연결 전이면 False 반환."""
        max_subscriptions = int(self.auth.config.get("kis", {}).get("max_ws_subscriptions", 3))
        existing = (tr_id, stock_code) in self._subscriptions
        if max_subscriptions > 0 and not existing and len(self._subscriptions) >= max_subscriptions:
            logger.warning("동적 구독 제한 초과: max=%d [%s] %s", max_subscriptions, tr_id, stock_code)
            return False
        self.subscribe(tr_id, stock_code, handler)
        if self._ws is None or self._approval_key is None:
            return False
        try:
            msg = {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": str(tr_id), "tr_key": stock_code}},
            }
            await self._ws.send(json.dumps(msg))
            logger.info("동적 구독 추가: [%s] %s", tr_id, stock_code)
            return True
        except Exception as e:
            logger.warning("동적 구독 실패: %s", e)
            return False

    async def connect_and_subscribe(
        self,
        domestic_codes: list[str],
        overseas_codes: list[str],
        callbacks: dict,
        overseas_exchanges: dict[str, str] | None = None,
    ) -> None:
        """편의 메서드: 종목 목록으로 구독 등록 후 run() 실행."""
        from kis.constants import WebSocketTRID as _TRID

        price_cb = callbacks.get(_TRID.DOMESTIC_PRICE)
        askbid_cb = callbacks.get(_TRID.DOMESTIC_ASKBID)
        ovs_price_cb = callbacks.get(_TRID.OVERSEAS_PRICE)
        domestic_fill_trid = _TRID.DOMESTIC_FILL_PAPER if self.auth.is_paper else _TRID.DOMESTIC_FILL_LIVE
        overseas_fill_trid = _TRID.OVERSEAS_FILL_PAPER if self.auth.is_paper else _TRID.OVERSEAS_FILL_LIVE
        fill_cb = callbacks.get(domestic_fill_trid)
        ovs_fill_cb = callbacks.get(overseas_fill_trid)
        kis_cfg = self.auth.config.get("kis", {})
        subscribe_orderbook = bool(kis_cfg.get("subscribe_orderbook", False))
        subscribe_fills = bool(kis_cfg.get("subscribe_fills", False))

        for code in domestic_codes:
            if price_cb:
                self.subscribe(_TRID.DOMESTIC_PRICE, code, price_cb)
            if askbid_cb and subscribe_orderbook:
                self.subscribe(_TRID.DOMESTIC_ASKBID, code, askbid_cb)

        overseas_exchanges = overseas_exchanges or {}
        for code in overseas_codes:
            if ovs_price_cb:
                exchange = overseas_exchanges.get(str(code).upper(), "NAS")
                tr_key = f"D{exchange}{code}"
                self.subscribe(_TRID.OVERSEAS_PRICE, tr_key, ovs_price_cb)

        hts_id = kis_cfg.get("hts_id", "")
        account_no = self.auth.get_account_no()
        # Paper WS 서버(31000)는 체결통보 TR을 지원하지 않으므로 live 전용 구독
        if fill_cb and hts_id and subscribe_fills and not self.auth.is_paper:
            self.subscribe_global(domestic_fill_trid, hts_id, fill_cb)
        if ovs_fill_cb and account_no and subscribe_fills and not self.auth.is_paper:
            self.subscribe_global(overseas_fill_trid, account_no, ovs_fill_cb)

        await self.run()

    async def run(self):
        """WebSocket 수신 루프. 재연결 포함."""
        self._running = True
        self._approval_key = self.auth.get_ws_approval_key()

        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self._ws = ws
                    logger.info("WebSocket 연결됨: %s", self.ws_url)
                    await self._send_subscriptions(ws, self._approval_key)
                    await self._recv_loop(ws)
            except (websockets.ConnectionClosed, OSError) as e:
                logger.warning("WebSocket 연결 종료: %s — 5초 후 재연결", e)
                self._ws = None
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("WebSocket 오류: %s", e)
                self._ws = None
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    def status(self) -> dict:
        return {
            "connected": self._ws is not None,
            "subscription_count": len(self._subscriptions),
            "subscriptions": [f"{tr_id}:{stock_code}" for tr_id, stock_code in self._subscriptions],
            "last_message_at": self.last_message_at,
            "last_realtime_at": self.last_realtime_at,
            "last_realtime_sample": self.last_realtime_sample,
            "last_unmatched_key": self.last_unmatched_key,
            "realtime_count": self.realtime_count,
            "unmatched_count": self.unmatched_count,
        }

    async def _recv_loop(self, ws):
        while self._running:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
            except asyncio.TimeoutError as e:
                raise TimeoutError("WebSocket realtime receive timeout") from e
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            await self._dispatch(ws, raw)

    async def _dispatch(self, ws, raw: str):
        self.last_message_at = datetime.now().isoformat()
        if raw[0] in _MSG_REALTIME:
            self.last_realtime_at = self.last_message_at
            self.last_realtime_sample = raw[:300]
            self.realtime_count += 1
            # 데이터 메시지: {0|1}|{TR_ID}|{COUNT}|{DATA}
            parts = raw.split("|", 3)
            if len(parts) < 4:
                return
            tr_id   = parts[1]
            payload = parts[3]
            try:
                count = int(parts[2])
            except (ValueError, IndexError):
                count = 1

            dm = self._data_map.get(tr_id)
            if dm is None:
                dm = {}
            if dm.get("encrypt") == "Y":
                try:
                    payload = aes_cbc_base64_dec(dm["key"], dm["iv"], payload)
                except Exception as e:
                    logger.error("AES 복호화 실패 (%s): %s", tr_id, e)
                    return

            all_fields = payload.split("^")

            if count > 1:
                field_count = dm.get("field_count") or _KNOWN_FIELD_COUNTS.get(tr_id)
                if field_count is None and len(all_fields) % count == 0:
                    field_count = len(all_fields) // count
                if field_count:
                    if tr_id in self._data_map:
                        self._data_map[tr_id]["field_count"] = field_count
                if field_count and len(all_fields) == count * field_count:
                    records = [
                        all_fields[i * field_count:(i + 1) * field_count]
                        for i in range(count)
                    ]
                else:
                    records = [all_fields]
            else:
                records = [all_fields]
                if all_fields and dm.get("field_count") is None and tr_id in self._data_map:
                    self._data_map[tr_id]["field_count"] = len(all_fields)

            for fields in records:
                if not fields:
                    continue
                stock_code = fields[0]
                handler = self._handlers.get(f"{tr_id}:{stock_code}")
                if handler is None and tr_id == str(WebSocketTRID.OVERSEAS_PRICE) and len(fields) > 1:
                    handler = self._handlers.get(f"{tr_id}:{fields[1]}")
                if handler is None:
                    handler = self._handlers.get(f"{tr_id}:*")
                if handler:
                    handler(tr_id, fields)
                else:
                    self.unmatched_count += 1
                    self.last_unmatched_key = f"{tr_id}:{stock_code}"
        else:
            await self._handle_system(ws, raw)

    async def _handle_system(self, ws, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("JSON 파싱 실패: %s", raw[:100])
            return

        header  = msg.get("header", {})
        tr_id   = header.get("tr_id", "")
        encrypt = header.get("encrypt", "N")

        if tr_id == "PINGPONG":
            await ws.pong(raw)
            logger.debug("PINGPONG 응답 전송")
            return

        body = msg.get("body", {})
        if body:
            rt_cd = body.get("rt_cd", "")
            msg1  = body.get("msg1", "")
            if rt_cd == "0":
                logger.info("구독 성공 [%s]: %s", tr_id, msg1)
                output = body.get("output", {})
                iv  = output.get("iv")
                key = output.get("key")
                if tr_id and tr_id in self._data_map:
                    self._data_map[tr_id]["encrypt"] = encrypt
                    if iv:
                        self._data_map[tr_id]["iv"] = iv
                    if key:
                        self._data_map[tr_id]["key"] = key
            elif msg1 and msg1.startswith("UNSUB"):
                logger.info("구독 해제 [%s]", tr_id)
            else:
                logger.warning("시스템 메시지 [%s] rt_cd=%s: %s", tr_id, rt_cd, msg1)

    async def _send_subscriptions(self, ws, approval_key: str):
        for tr_id, stock_code in self._subscriptions:
            msg = {
                "header": {
                    "approval_key": approval_key,
                    "custtype": "P",
                    "tr_type": "1",
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": tr_id, "tr_key": stock_code}},
            }
            await ws.send(json.dumps(msg))
            logger.info("구독 요청: [%s] %s", tr_id, stock_code)
            await asyncio.sleep(0.05)


# ── 체결 데이터 파싱 ──────────────────────────────────────────────────

def parse_domestic_price(fields: list[str]) -> dict:
    """H0STCNT0 체결 데이터 파싱."""
    get = lambda i, d="": fields[i] if len(fields) > i else d
    return {
        "stock_code":   get(0),
        "time":         get(1),
        "price":        get(2),
        "sign":         get(3),
        "change":       get(4),
        "change_pct":   get(5),
        "weighted_avg": get(6),
        "open":         get(7),
        "high":         get(8),
        "low":          get(9),
        "ask":          get(10),
        "bid":          get(11),
        "vol":          get(12),
        "acml_vol":     get(13),
        "acml_val":     get(14),
    }


def parse_domestic_askbid(fields: list[str]) -> dict:
    """H0STASP0 국내주식 실시간호가 파싱."""
    get = lambda i, d="0": fields[i] if len(fields) > i else d
    def _f(i): return float(get(i) or 0)

    ask_prices  = [_f(3+i)  for i in range(10)]   # 매도호가 1~10
    bid_prices  = [_f(13+i) for i in range(10)]   # 매수호가 1~10
    ask_volumes = [_f(23+i) for i in range(10)]   # 매도호가잔량 1~10
    bid_volumes = [_f(33+i) for i in range(10)]   # 매수호가잔량 1~10
    total_ask   = _f(43)
    total_bid   = _f(44)
    total       = total_ask + total_bid
    imbalance   = total_bid / total if total > 0 else 0.5

    return {
        "stock_code":   get(0),
        "time":         get(1),
        "ask1":         ask_prices[0],
        "bid1":         bid_prices[0],
        "ask_prices":   ask_prices,
        "bid_prices":   bid_prices,
        "ask_volumes":  ask_volumes,
        "bid_volumes":  bid_volumes,
        "total_ask":    total_ask,
        "total_bid":    total_bid,
        "imbalance":    round(imbalance, 4),  # 0~1, >0.55=매수우위 <0.45=매도우위
    }


def parse_overseas_price(fields: list[str]) -> dict:
    """HDFSCNT0 해외주식 체결 데이터 파싱."""
    get = lambda i, d="": fields[i] if len(fields) > i else d
    return {
        "subscription_key": get(0),
        "stock_code": get(1),
        "date":       get(3),
        "time":       get(5),
        "kst_time":   get(7),
        "open":       get(8),
        "high":       get(9),
        "low":        get(10),
        "price":      get(11),
        "sign":       get(12),
        "change":     get(13),
        "change_pct": get(14),
        "vol":        get(17),
        "acml_vol":   get(20),
        "acml_val":   get(21),
    }


def parse_domestic_fill_notice(fields: list[str]) -> dict:
    """H0STCNI0/H0STCNI9 국내주식 체결통보 파싱."""
    get = lambda i, d="": fields[i] if len(fields) > i else d
    return {
        "cust_id": get(0),
        "account_no": get(1),
        "order_no": get(2),
        "original_order_no": get(3),
        "side_code": get(4),
        "receipt_code": get(5),
        "order_kind": get(6),
        "order_condition": get(7),
        "stock_code": get(8),
        "filled_qty": get(9),
        "filled_price": get(10),
        "filled_time": get(11),
        "rejected": get(12),
        "filled": get(13),  # 2=체결, 1=접수/정정/취소/거부 접수
        "accepted": get(14),
        "order_qty": get(16),
        "stock_name": get(24),
        "order_price": get(25),
    }


def parse_overseas_fill_notice(fields: list[str]) -> dict:
    """H0GSCNI0/H0GSCNI9 해외주식 체결통보 파싱."""
    get = lambda i, d="": fields[i] if len(fields) > i else d
    return {
        "cust_id": get(0),
        "account_no": get(1),
        "order_no": get(2),
        "original_order_no": get(3),
        "side_code": get(4),
        "receipt_code": get(5),
        "order_kind": get(6),
        "stock_code": get(7),
        "filled_qty": get(8),
        "filled_price": get(9),
        "filled_time": get(10),
        "rejected": get(11),
        "filled": get(12),  # 2=체결, 1=접수/정정/취소/거부 접수
        "accepted": get(13),
        "order_qty": get(16),
        "stock_name": get(17),
        "order_condition": get(18),
        "filled_price_12": get(24),
    }
