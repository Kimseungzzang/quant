import json
import asyncio
import logging
from base64 import b64decode
from typing import Callable

import websockets
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .constants import WebSocketTRID

logger = logging.getLogger(__name__)

_MSG_REALTIME = ("0", "1")


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

        # tr_id → {columns, encrypt, key, iv}
        self._data_map: dict[str, dict] = {}
        # "{tr_id}:{stock_code}" → Callable
        self._handlers: dict[str, Callable] = {}
        # 구독 목록: [(tr_id, stock_code)]
        self._subscriptions: list[tuple[str, str]] = []

    def subscribe(self, tr_id: WebSocketTRID | str, stock_code: str, handler: Callable):
        """실시간 데이터 구독 등록. handler(tr_id, fields: list[str])"""
        key = f"{tr_id}:{stock_code}"
        if key not in self._handlers:
            self._subscriptions.append((tr_id, stock_code))
        self._handlers[key] = handler
        self._data_map.setdefault(tr_id, {"columns": [], "encrypt": "N", "key": None, "iv": None, "field_count": None})

    def unsubscribe(self, tr_id: WebSocketTRID | str, stock_code: str):
        self._subscriptions = [
            (t, s) for t, s in self._subscriptions
            if not (t == tr_id and s == stock_code)
        ]
        self._handlers.pop(f"{tr_id}:{stock_code}", None)

    async def run(self):
        """WebSocket 수신 루프. 재연결 포함."""
        self._running = True
        approval_key = self.auth.get_ws_approval_key()

        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    logger.info("WebSocket 연결됨: %s", self.ws_url)
                    await self._send_subscriptions(ws, approval_key)
                    await self._recv_loop(ws)
            except (websockets.ConnectionClosed, OSError) as e:
                logger.warning("WebSocket 연결 종료: %s — 5초 후 재연결", e)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("WebSocket 오류: %s", e)
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    async def _recv_loop(self, ws):
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            await self._dispatch(ws, raw)

    async def _dispatch(self, ws, raw: str):
        if raw[0] in _MSG_REALTIME:
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
                field_count = dm.get("field_count")
                if field_count and len(all_fields) == count * field_count:
                    records = [
                        all_fields[i * field_count:(i + 1) * field_count]
                        for i in range(count)
                    ]
                else:
                    # field_count 미파악 시 첫 틱만 처리
                    logger.warning(
                        "TR_ID %s: COUNT=%d이지만 field_count=%s — 첫 틱만 처리",
                        tr_id, count, field_count,
                    )
                    records = [all_fields]
            else:
                records = [all_fields]
                # 첫 단일 틱에서 레코드 필드 수 학습
                if all_fields and "field_count" not in dm and tr_id in self._data_map:
                    self._data_map[tr_id]["field_count"] = len(all_fields)

            for fields in records:
                if not fields:
                    continue
                stock_code = fields[0]
                handler = self._handlers.get(f"{tr_id}:{stock_code}")
                if handler:
                    handler(tr_id, fields)
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


def parse_overseas_price(fields: list[str]) -> dict:
    """HDFSCNT0 해외주식 체결 데이터 파싱."""
    get = lambda i, d="": fields[i] if len(fields) > i else d
    return {
        "stock_code": get(0),
        "date":       get(1),
        "time":       get(2),
        "price":      get(3),
        "sign":       get(4),
        "change":     get(5),
        "change_pct": get(6),
        "vol":        get(7),
        "acml_val":   get(8),
    }
