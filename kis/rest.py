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
        self.fast_session = self._build_fast_session()

    def get(self, path: str, tr_id: str, params: dict, timeout: int = 10, fast: bool = False) -> dict:
        url = f"{self.base_url}{path}"
        headers = self.auth.get_headers(tr_id)
        sess = self.fast_session if fast else self.session
        resp = self._send_with_rate_retry(
            lambda: sess.get(url, headers=headers, params=params, timeout=timeout),
            retry_limit=0 if fast else 2,
        )
        return self._handle(resp)

    def post(self, path: str, tr_id: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = self.auth.get_headers(tr_id)
        resp = self._send_with_rate_retry(
            lambda: self.session.post(url, headers=headers, json=body, timeout=10),
            retry_limit=3,
        )
        return self._handle(resp)

    def _send_with_rate_retry(self, send, retry_limit: int) -> requests.Response:
        for attempt in range(retry_limit + 1):
            _throttle()
            resp = send()
            if self._is_kis_rate_limited(resp) and attempt < retry_limit:
                wait = 1.0 + attempt * 0.5
                logger.warning("KIS rate limit 응답 — %.1f초 후 재시도 (%d/%d)", wait, attempt + 1, retry_limit)
                time.sleep(wait)
                continue
            return resp
        raise RuntimeError("unreachable")

    @staticmethod
    def _is_kis_rate_limited(resp: requests.Response) -> bool:
        if resp.status_code not in (429, 500):
            return False
        return "EGW00201" in (resp.text or "")

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
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _build_fast_session(self) -> requests.Session:
        """분봉 조회 등 best-effort read용 — retry 없이 즉시 실패."""
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
