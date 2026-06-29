#!/usr/bin/env python3
"""IRBANKスクリプト共通ユーティリティ。

fetch_with_retry: タイムアウト・5xxエラー時に自動リトライするGETラッパー。
各スクリプトは `from irbank_utils import fetch_with_retry` でインポートして使う。
"""
import time

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

_RETRY_STATUS = {500, 502, 503, 504}


def fetch_with_retry(url: str, timeout: int = 30, retries: int = 3, **kwargs) -> requests.Response:
    """タイムアウト・5xxエラーを最大retries回リトライするGETリクエスト。

    - Timeout / ConnectionError: 指数バックオフ（1, 2, 4秒）でリトライ
    - 500 / 502 / 503 / 504: 同上でリトライ
    - その他のHTTPエラー / 4xx: 即座に例外を再送出
    """
    headers = kwargs.pop("headers", {"User-Agent": UA})
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        if resp.status_code in _RETRY_STATUS and attempt < retries - 1:
            last_exc = Exception(f"HTTP {resp.status_code} (リトライ {attempt + 1}/{retries}): {url}")
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp
    # 最後のリトライでも5xxなら raise_for_status() で HTTPError を発生させる
    resp.raise_for_status()
    return resp  # unreachable
