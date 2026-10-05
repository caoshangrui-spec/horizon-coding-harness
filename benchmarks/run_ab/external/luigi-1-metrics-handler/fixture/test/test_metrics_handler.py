import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from luigi.server import MetricsHandler  # noqa: E402


class MetricsPayload:
    def __init__(self):
        self.configured_handlers = []

    def configure_http_handler(self, handler):
        self.configured_handlers.append(handler)


class MetricsCollector:
    def __init__(self):
        self.payload = MetricsPayload()
        self.configured_handlers = []

    def generate_latest(self):
        return self.payload

    def configure_http_handler(self, handler):
        self.configured_handlers.append(handler)


collector = MetricsCollector()
state = type("State", (), {"_metrics_collector": collector})()
scheduler = type("Scheduler", (), {"_state": state})()
handler = MetricsHandler()
handler.initialize(scheduler)
handler.get()

assert collector.configured_handlers == [handler]
assert collector.payload.configured_handlers == []
assert handler.written is collector.payload
