"""
토스증권 Open API 읽기 전용 스모크테스트.
주문/포지션에 영향을 주지 않는 GET 엔드포인트만 순서대로 호출해서
client_id/secret, 계좌, 시세 조회가 실제로 동작하는지 눈으로 확인한다.

사용법:
    python toss_smoke_test.py [SYMBOL]

SYMBOL 생략 시 삼성전자(005930)로 조회한다.
"""
import sys

from utils import load_config
from toss.auth import TossAuth
from toss.rest import TossRestClient
from toss.api import TossBrokerAPI


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    config = load_config()

    toss_cfg = config.get("toss", {})
    if not toss_cfg.get("client_id") or not toss_cfg.get("client_secret"):
        print("[FAIL] config.yaml의 toss.client_id / toss.client_secret 이 비어있습니다.")
        print("       토스증권 WTS 설정 > Open API 에서 발급받아 채운 뒤 다시 실행하세요.")
        return 1

    auth = TossAuth(config, redis_client=None)
    client = TossRestClient(auth)
    broker = TossBrokerAPI(client)

    print("[1/5] 토큰 발급")
    auth.get_access_token()
    print("      OK")

    print("[2/5] GET /accounts")
    accounts = broker.get_accounts()
    print(f"      OK — {accounts}")
    if not accounts:
        print("[FAIL] 계좌가 조회되지 않습니다. WTS에서 종합매매 계좌 개설 여부를 확인하세요.")
        return 1

    print("[3/5] GET /holdings")
    holdings = broker.get_holdings()
    items = holdings.get("items", [])
    print(f"      OK — 보유종목 {len(items)}건")
    for it in items[:5]:
        print(f"        {it.get('symbol')} {it.get('name')} qty={it.get('quantity')}")

    print(f"[4/5] GET /prices?symbols={symbol}")
    prices = broker.get_prices([symbol])
    print(f"      OK — {prices}")

    print(f"[5/5] GET /candles?symbol={symbol}&interval=1d&count=5")
    candles = broker.get_candles(symbol, interval="1d", count=5)
    rows = candles.get("candles", [])
    print(f"      OK — {len(rows)}건")
    for c in rows[-5:]:
        print(f"        {c}")

    print("\n모두 성공. 다음 단계(README/plan 참고): 자동매매 루프 연결 전, "
          "별도로 최소 수량 지정가 주문 1건을 수동 테스트하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
