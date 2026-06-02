"""
AI 트레이더 CLI
사용법: python cli.py
서버가 먼저 실행 중이어야 함: uvicorn fastapi_app:app --port 8000
"""
import asyncio
import json
import sys
import threading
from datetime import datetime

import requests
import websockets

BASE = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws/stream"

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
GRAY   = "\033[90m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _print_ai(source: str, message: str) -> None:
    color = {
        "morning_brief": CYAN,
        "event": YELLOW,
        "tool": GRAY,
        "chat": GREEN,
        "system": BLUE,
    }.get(source, RESET)
    prefix = {
        "morning_brief": "📋 브리핑",
        "event":         "⚡ 이벤트",
        "tool":          "🔧 툴",
        "chat":          "🤖 AI",
        "system":        "⚙️  시스템",
    }.get(source, source)
    print(f"\n{color}{BOLD}[{_ts()}] {prefix}{RESET}")
    for line in message.split("\n"):
        print(f"  {color}{line}{RESET}")


def _print_status(data: dict) -> None:
    print(f"\n{BOLD}{'─'*50}{RESET}")
    print(f"{BOLD}  AI 트레이더 상태{RESET}")
    print(f"{'─'*50}")
    print(f"  모드:       {data.get('mode', '?')}")
    print(f"  거래 상태:  {'활성' if data.get('trading_active') else '대기'}")
    print(f"{'─'*50}\n")


def _print_plan(data: dict) -> None:
    if "message" in data:
        print(f"\n  {GRAY}오늘 계획 없음 (아직 브리핑 전){RESET}\n")
        return
    print(f"\n{BOLD}{'─'*50}{RESET}")
    print(f"{BOLD}  오늘 계획{RESET}  {GRAY}({data.get('created_at', '')[:16]}){RESET}")
    print(f"{'─'*50}")
    print(f"  전망: {data.get('market_outlook', '')}")
    print(f"  전략: {data.get('strategy', '')}")
    watches = data.get("watch_stocks", [])
    if watches:
        print(f"  주목 종목:")
        for w in watches:
            print(f"    • {w.get('name', '')} ({w.get('code', '')}) — {w.get('reason', '')}")
    print(f"{'─'*50}\n")


def _print_positions(positions: list) -> None:
    print(f"\n{BOLD}{'─'*50}{RESET}")
    print(f"{BOLD}  현재 포지션{RESET}")
    print(f"{'─'*50}")
    if not positions:
        print(f"  {GRAY}보유 포지션 없음{RESET}")
    for p in positions:
        pnl = p.get("unrealizedPct") or p.get("unrealized_pct") or 0
        color = GREEN if float(pnl) >= 0 else RED
        print(f"  {p.get('stockName') or p.get('stock_name', '')} "
              f"({p.get('stockCode') or p.get('stock_code', '')}) "
              f"{p.get('quantity', 0)}주  "
              f"{color}{float(pnl):+.2f}%{RESET}")
    print(f"{'─'*50}\n")


def _print_decisions(decisions: list) -> None:
    print(f"\n{BOLD}{'─'*50}{RESET}")
    print(f"{BOLD}  최근 판단 이력{RESET}")
    print(f"{'─'*50}")
    if not decisions:
        print(f"  {GRAY}판단 이력 없음{RESET}")
    for d in decisions[:10]:
        action = d.get("action", "")
        color = GREEN if "BUY" in action.upper() else RED if "SELL" in action.upper() else GRAY
        ts = str(d.get("decided_at", ""))[:16]
        print(f"  {GRAY}{ts}{RESET}  {color}{action:8}{RESET}  "
              f"{d.get('stock_code', ''):8}  {d.get('reason', '')[:40]}")
    print(f"{'─'*50}\n")


def _get(path: str) -> dict | list:
    try:
        r = requests.get(f"{BASE}{path}", timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _post(path: str, body: dict) -> dict:
    try:
        r = requests.post(f"{BASE}{path}", json=body, timeout=30)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _handle_command(cmd: str) -> bool:
    """커맨드 처리. 종료면 True 반환."""
    cmd = cmd.strip()
    if not cmd:
        return False

    if cmd in ("q", "quit", "exit"):
        print(f"\n{GRAY}종료합니다.{RESET}\n")
        return True

    if cmd in ("s", "status"):
        _print_status(_get("/health"))

    elif cmd in ("p", "plan"):
        _print_plan(_get("/ai/plan"))

    elif cmd in ("pos", "positions"):
        _print_positions(_get("/trade/positions/live"))

    elif cmd in ("d", "decisions"):
        _print_decisions(_get("/ai/decisions"))

    elif cmd in ("b", "brief"):
        print(f"\n{CYAN}아침 브리핑 시작 중...{RESET}")
        result = _post("/ai/brief", {})
        print(f"  {result.get('status', result)}")

    elif cmd in ("w", "watches"):
        result = _post("/ai/chat", {"message": "list_watches 툴로 현재 감시 목록 알려줘"})
        print(f"\n{GREEN}🤖 AI{RESET}: {result.get('response', result)}")

    elif cmd in ("h", "help"):
        _print_help()

    elif cmd.startswith("/"):
        # /로 시작하면 AI에게 직접 전달
        message = cmd[1:].strip()
        if message:
            print(f"\n{GRAY}AI에게 전달 중...{RESET}")
            result = _post("/ai/chat", {"message": message})
            _print_ai("chat", result.get("response", str(result)))
    else:
        # 그냥 입력하면 AI 채팅
        print(f"\n{GRAY}AI에게 전달 중...{RESET}")
        result = _post("/ai/chat", {"message": cmd})
        _print_ai("chat", result.get("response", str(result)))

    return False


def _print_help() -> None:
    print(f"""
{BOLD}{'─'*50}{RESET}
{BOLD}  명령어{RESET}
{'─'*50}
  s, status      서버 상태 확인
  p, plan        오늘 AI 계획 보기
  pos            현재 보유 포지션
  d, decisions   최근 판단 이력
  b, brief       아침 브리핑 수동 실행
  w, watches     현재 감시 목록
  h, help        도움말
  q, quit        종료

  [메시지]       AI에게 채팅 (예: 삼성전자 지금 어때?)
  /[명령]        AI에게 직접 지시 (예: /삼성전자 분석해줘)
{'─'*50}
""")


async def _ws_listener() -> None:
    """백그라운드에서 WebSocket 메시지 수신."""
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                print(f"  {GREEN}✓ 실시간 스트림 연결됨{RESET}")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        mtype = msg.get("type")
                        if mtype == "ai_message":
                            _print_ai(msg.get("source", "ai"), msg.get("message", ""))
                            print(f"{CYAN}> {RESET}", end="", flush=True)
                        elif mtype == "price":
                            pass  # 틱 노이즈 무시
                    except Exception:
                        pass
        except Exception as e:
            print(f"\n  {GRAY}스트림 재연결 중... ({e}){RESET}")
            await asyncio.sleep(3)


def _start_ws_thread() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_ws_listener())


def main() -> None:
    print(f"""
{CYAN}{BOLD}
  ╔══════════════════════════════════╗
  ║      AI 트레이더 CLI v1.0        ║
  ╚══════════════════════════════════╝
{RESET}""")

    # 서버 연결 확인
    try:
        health = _get("/health")
        if "error" in health:
            print(f"  {RED}✗ 서버 연결 실패: {health['error']}{RESET}")
            print(f"  {GRAY}먼저 서버를 시작하세요: uvicorn fastapi_app:app --port 8000{RESET}\n")
            sys.exit(1)
        print(f"  {GREEN}✓ 서버 연결됨{RESET}  모드: {health.get('mode', '?')}")
    except Exception as e:
        print(f"  {RED}✗ 서버 연결 실패: {e}{RESET}\n")
        sys.exit(1)

    # WebSocket 리스너 백그라운드 실행
    ws_thread = threading.Thread(target=_start_ws_thread, daemon=True)
    ws_thread.start()

    _print_help()

    while True:
        try:
            user_input = input(f"{CYAN}> {RESET}").strip()
            if _handle_command(user_input):
                break
        except (KeyboardInterrupt, EOFError):
            print(f"\n{GRAY}종료합니다.{RESET}\n")
            break


if __name__ == "__main__":
    main()
