from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class FeishuResult:
    ok: bool
    detail: str


def post_text(webhook_url: str, text: str, *, timeout: float = 10.0, attempts: int = 2) -> FeishuResult:
    body = json.dumps(
        {"msg_type": "text", "content": {"text": text}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    last_detail = "request failed"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(64 * 1024)
                if response.status < 200 or response.status >= 300:
                    last_detail = f"HTTP {response.status}"
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    return FeishuResult(False, "Feishu returned invalid JSON")
                code = payload.get("code", payload.get("StatusCode"))
                if code == 0:
                    return FeishuResult(True, "sent")
                last_detail = f"Feishu rejected the message with code {code!r}"
        except urllib.error.HTTPError as exc:
            last_detail = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, TimeoutError):
            last_detail = "network request failed"
        if attempt + 1 < attempts:
            time.sleep(1)
    return FeishuResult(False, last_detail)
