import json
import logging

from fastapi import APIRouter

from routers import state

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
def health():
    config = state.components.get("config") or {}
    ws = state.components.get("ws")
    kis_ws_status = ws.status() if ws is not None and hasattr(ws, "status") else None
    return {
        "status": "ok",
        "mode": config.get("mode"),
        "trading_active": state.trading_task is not None and not state.trading_task.done(),
        "kis_ws_connected": bool(getattr(ws, "_ws", None)) if ws is not None else False,
        "kis_ws_url": getattr(ws, "ws_url", None) if ws is not None else None,
        "kis_ws_status": kis_ws_status,
        "trading_last_error": state.trading_last_error,
    }


@router.get("/ai/system/status")
async def system_status():
    import datetime as _dt

    r = state.components.get("redis")
    redis_ok = False
    redis_keys: list = []
    if r:
        try:
            r.ping()
            redis_ok = True
            redis_keys = list(r.keys("ai:*"))
        except Exception:
            pass

    ws_info = state.components.get("ws")
    ws_status: dict = {}
    if ws_info:
        raw = ws_info.status() if hasattr(ws_info, "status") else {}
        subs = getattr(ws_info, "_subscriptions", [])
        ws_status = {
            "connected": raw.get("connected", False),
            "subscriptions": list(subs) if isinstance(subs, list) else list(subs.keys()),
            "last_message_at": str(getattr(ws_info, "_last_message_at", None)),
        }

    indicator_cache = state.components.get("indicator_cache")
    cached_stocks = list(getattr(indicator_cache, "_candles", {}).keys()) if indicator_cache else []

    watches: dict = {}
    if r:
        raw = r.get("ai:watches")
        if raw:
            watches = json.loads(raw)

    agent_history_len = len(getattr(state.agent, "_chat_history", [])) if state.agent else 0

    return {
        "timestamp": _dt.datetime.now().isoformat(),
        "server": "ok",
        "redis": {"ok": redis_ok, "ai_keys": redis_keys},
        "websocket": ws_status,
        "indicator_cache": {"cached_stocks": cached_stocks},
        "watches": {"count": len(watches), "stocks": list(watches.keys())},
        "agent": {"history_len": agent_history_len},
        "mode": state.components.get("config", {}).get("mode", "unknown"),
    }


@router.get("/ai/system/logs")
async def system_logs(level: str = "INFO", limit: int = 50, name: str = ""):
    _ORDER = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    logs = list(state.log_buffer)
    if level != "ALL":
        min_idx = _ORDER.index(level) if level in _ORDER else 1
        logs = [l for l in logs if l["level"] in _ORDER and _ORDER.index(l["level"]) >= min_idx]
    if name:
        logs = [l for l in logs if name.lower() in l["name"].lower()]
    return {"logs": logs[-limit:], "total": len(logs)}
