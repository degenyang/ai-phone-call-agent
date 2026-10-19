import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

SECRET = "test-secret-" + "x" * 32
CALL_ID = "20261019_120000_0123456789ab"


def stream_path(call_id=CALL_ID, ttl=600, secret=SECRET, expires=None, token=None):
    """The request path Twilio opens for a call prepared by aicall.py: /media/<call_id>/<expires>/<token>."""
    import server
    expires = int(time.time()) + ttl if expires is None else expires
    token = server.call_token_signature(secret, call_id, expires) if token is None else token
    return f"/media/{call_id}/{expires}/{token}"


class FakeRequest:
    def __init__(self, path, headers=None):
        from websockets.datastructures import Headers
        self.path = path
        self.headers = Headers(headers or ())  # a dict or (name, value) pairs; repeated names allowed


class FakeTwilioSocket:
    """Stands in for the Twilio websocket: yields queued messages, records what the bridge sends."""

    def __init__(self, incoming=(), hold_open=False):
        self.incoming = list(incoming)
        self.sent = []
        self.close_code = None
        self.hold_open = hold_open  # after the queued messages, stay idle until closed (like a live call)
        self.closed = asyncio.Event()

    def respond(self, status, text):  # the HTTP-stage response, like a websockets ServerConnection
        return (int(status), text)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self.incoming:
            if self.hold_open:
                await self.closed.wait()
            raise StopAsyncIteration
        msg = self.incoming.pop(0)
        if isinstance(msg, BaseException):  # e.g. the peer dropping the connection
            raise msg
        return msg if isinstance(msg, str) else json.dumps(msg)

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        if self.close_code is None:
            self.close_code = code
        self.closed.set()


async def open_stream(bridge, ws, path, headers=None):
    """What the websockets server does: process_request on the HTTP request, the handler only if it passes."""
    response = bridge.process_request(ws, FakeRequest(path, headers))
    if response is None:
        await bridge.handler(ws)
    return response


@pytest.fixture
def fake_socket():
    return FakeTwilioSocket
