import json
import logging
import requests
from pathlib import Path
from datetime import datetime

from .constants import (
    TradingMode,
    AuthPath,
    KIS_REST_URL_LIVE, KIS_REST_URL_PAPER, KIS_REST_URL_MOCK,
    KIS_WS_BASE_LIVE, KIS_WS_BASE_PAPER, KIS_WS_PATH, KIS_WS_URL_MOCK,
)

logger = logging.getLogger(__name__)

TOKEN_CACHE_FILE = Path("data/.token_cache.json")


class KISAuth:
    def __init__(self, config: dict):
        self.app_key = config["kis"]["app_key"]
        self.app_secret = config["kis"]["app_secret"]
        mode = TradingMode(config["mode"])
        self.is_mock  = mode == TradingMode.MOCK
        self.is_paper = mode in (TradingMode.PAPER, TradingMode.MOCK)

        if mode == TradingMode.MOCK:
            self.base_url    = config["kis"].get("rest_url", KIS_REST_URL_MOCK)
            self.ws_full_url = config["kis"].get("ws_url",  KIS_WS_URL_MOCK)
        elif mode == TradingMode.PAPER:
            self.base_url    = KIS_REST_URL_PAPER
            self.ws_full_url = KIS_WS_BASE_PAPER + KIS_WS_PATH
        else:
            self.base_url    = KIS_REST_URL_LIVE
            self.ws_full_url = KIS_WS_BASE_LIVE + KIS_WS_PATH

        self._access_token: str | None = None
        self._token_expired_at: datetime | None = None
        self._ws_approval_key: str | None = None

    def get_access_token(self) -> str:
        if self._is_token_valid():
            return self._access_token

        cached = self._load_token_cache()
        if cached:
            self._access_token = cached["access_token"]
            self._token_expired_at = datetime.fromisoformat(cached["expired_at"])
            if self._is_token_valid():
                logger.debug("캐시에서 토큰 로드")
                return self._access_token

        return self._issue_token()

    def get_ws_approval_key(self) -> str:
        if self._ws_approval_key:
            return self._ws_approval_key
        return self._issue_ws_key()

    def get_headers(self, tr_id: str, **extra) -> dict:
        token = self.get_access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        headers.update(extra)
        return headers

    def _is_token_valid(self) -> bool:
        if not self._access_token or not self._token_expired_at:
            return False
        return datetime.now() < self._token_expired_at

    def _issue_token(self) -> str:
        url = f"{self.base_url}{AuthPath.TOKEN}"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        expired_str = data["access_token_token_expired"]
        self._token_expired_at = datetime.strptime(expired_str, "%Y-%m-%d %H:%M:%S")

        self._save_token_cache()
        logger.info("신규 액세스 토큰 발급 완료, 만료: %s", expired_str)
        return self._access_token

    def _issue_ws_key(self) -> str:
        url = f"{self.base_url}{AuthPath.APPROVAL}"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        self._ws_approval_key = resp.json()["approval_key"]
        logger.info("WebSocket 접속키 발급 완료")
        return self._ws_approval_key

    def _load_token_cache(self) -> dict | None:
        if not TOKEN_CACHE_FILE.exists():
            return None
        try:
            with TOKEN_CACHE_FILE.open() as f:
                return json.load(f)
        except Exception:
            return None

    def _save_token_cache(self):
        TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TOKEN_CACHE_FILE.open("w") as f:
            json.dump(
                {
                    "access_token": self._access_token,
                    "expired_at": self._token_expired_at.isoformat(),
                },
                f,
            )
