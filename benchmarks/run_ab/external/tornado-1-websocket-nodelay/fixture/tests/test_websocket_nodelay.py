import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tornado.websocket import (  # noqa: E402
    WebSocketHandler,
    WebSocketProtocol,
    WebSocketProtocol13,
)


class Stream:
    def __init__(self):
        self.nodelay_values = []

    def set_nodelay(self, value):
        self.nodelay_values.append(value)


stream = Stream()
connection = WebSocketProtocol13(stream)
handler = WebSocketHandler(stream=None, ws_connection=connection)
handler.set_nodelay(True)

assert stream.nodelay_values == [True]
assert "set_nodelay" in WebSocketProtocol.__abstractmethods__
