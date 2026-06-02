import time
import logging
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .constants import KIS_RATE_LIMIT_SEC

logger = logging.getLogger(__name__)

_last_request_time: float = 0.0
_throttle_lock = threading.Lock()


def _throttle():
    global _last_request_time
    with _throttle_lock:
        elapsed = time.monotonic() - _last_request_time
        if elapsed < KIS_RATE_LIMIT_SEC:
            time.sleep(KIS_RATE_LIMIT_SEC - elapsed)
        _last_request_time = time.monotonic()


class KISRestClient:
    def __init__(self, auth):
        self.auth = auth
        self.base_url = auth.base_url
        self.session = self._build_session()

    def get(self, path: str, tr_id: str, params: dict) -> dict:
        _throttle()
        url = f"{self.base_url}{path}"
        headers = self.auth.get_headers(tr_id)
        resp = self.session.get(url, headers=headers, params=params, timeout=10)
        return self._handle(resp)

    def post(self, path: str, tr_id: str, body: dict) -> dict:
        _throttle()
        url = f"{self.base_url}{path}"
        headers = self.auth.get_headers(tr_id)
        resp = self.session.post(url, headers=headers, json=body, timeout=10)
        return self._handle(resp)

    def _handle(self, resp: requests.Response) -> dict:
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            body = resp.text[:1000] if resp.text else ""
            logger.error("KIS HTTP 오류 [%s] %s: %s", resp.status_code, resp.url, body)
            raise e
        if not resp.text:
            raise RuntimeError("KIS API 빈 응답 (시장 닫힘 또는 데이터 없음)")
        try:
            data = resp.json()
        except ValueError as e:
            raise RuntimeError(f"KIS API 응답 파싱 실패: {e} | body={resp.text[:200]}") from e
        if data.get("rt_cd") != "0":
            logger.error("KIS API 오류 [%s]: %s", data.get("msg_cd"), data.get("msg1"))
            raise RuntimeError(f"KIS API 오류: {data.get('msg1')}")
        return data

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
