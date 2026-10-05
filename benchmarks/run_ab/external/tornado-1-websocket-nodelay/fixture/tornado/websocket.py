import abc


class WebSocketHandler:
    def __init__(self, stream, ws_connection):
        self.stream = stream
        self.ws_connection = ws_connection

    def set_nodelay(self, value: bool) -> None:
        assert self.stream is not None
        self.stream.set_nodelay(value)


class WebSocketProtocol(abc.ABC):
    @abc.abstractmethod
    def start_pinging(self) -> None:
        raise NotImplementedError()


class WebSocketProtocol13(WebSocketProtocol):
    def __init__(self, stream):
        self.stream = stream

    def start_pinging(self) -> None:
        pass


class WebSocketClientConnection:
    pass
