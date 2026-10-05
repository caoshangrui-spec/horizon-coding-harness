class MetricsHandler:
    def initialize(self, scheduler):
        self._scheduler = scheduler

    def write(self, metrics):
        self.written = metrics

    def get(self):
        metrics = self._scheduler._state._metrics_collector.generate_latest()
        if metrics:
            metrics.configure_http_handler(self)
            self.write(metrics)
