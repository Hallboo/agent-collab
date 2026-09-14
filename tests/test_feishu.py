from __future__ import annotations

import json
from unittest.mock import patch

from agent_collab.feishu import post_text


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return json.dumps({"code": 0}).encode()


def test_feishu_text_payload() -> None:
    with patch("urllib.request.urlopen", return_value=Response()) as request:
        result = post_text("https://example.invalid/test-webhook", "safe summary", attempts=1)
    assert result.ok
    outgoing = request.call_args.args[0]
    assert json.loads(outgoing.data) == {"msg_type": "text", "content": {"text": "safe summary"}}
