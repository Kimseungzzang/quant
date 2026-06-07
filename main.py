"""AI 트레이더 — uvicorn 시작 래퍼 (주 서버: fastapi_app.py)"""
import argparse
import sys

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="AI Trader")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run("fastapi_app:app", host=args.host, port=args.port, reload=args.reload)
