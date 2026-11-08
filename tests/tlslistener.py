"""A real TLS listener on loopback, and a record of what each client asked it.

The certificates come from ``trustme``; this is the half it does not provide.
Nothing here decides whether a certificate is good -- it performs the handshake
with whatever it was handed, so a client that refuses that certificate fails
here exactly as it would against Apache. Every verdict about trust is OpenSSL's,
inside the client under test.

``sni_names`` and ``host_headers`` are how a test sees that one ``server_name``
reached both the handshake and the request, and ``refusals`` is how it sees
that a rejection reached the wire rather than being inferred from the client's
side of it alone.
"""

from __future__ import annotations

import socket
import ssl
import threading
from types import TracebackType

import trustme

_READ_CHUNK = 65536
_HEADER_END = b"\r\n\r\n"
_ACCEPT_TIMEOUT = 0.5
_EXCHANGE_TIMEOUT = 5.0
_BACKLOG = 8
#: Keep-alive, deliberately. A connection that closed after every answer would
#: hide the thing worth testing: httpcore pools by origin, so a client that
#: kept this connection would offer it to the *next* probe of the same origin,
#: whatever name that one asked to verify.
RESPONSE_BODY = "ok"
_RESPONSE = f"HTTP/1.1 200 OK\r\nContent-Length: {len(RESPONSE_BODY) + 1}\r\n\r\n{RESPONSE_BODY}\n".encode()


class TlsListener:
    """A TLS server answering 200 to anything, with the certificate it is given."""

    def __init__(self, certificate: trustme.LeafCert) -> None:
        self._certificate = certificate
        self.sni_names: list[str | None] = []
        self.host_headers: list[str] = []
        #: The alert a client sent instead of a request, one per connection it
        #: refused to use. Empty when every handshake was accepted.
        self.refusals: list[str] = []
        self._connections: list[threading.Thread] = []
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(_BACKLOG)
        self._socket.settimeout(_ACCEPT_TIMEOUT)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def port(self) -> int:
        port: int = self._socket.getsockname()[1]
        return port

    def url(self, path: str = "/") -> str:
        return f"https://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> TlsListener:
        self._thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=_EXCHANGE_TIMEOUT)
        self._socket.close()
        # Joined, not abandoned: a test reads what the connections recorded
        # after this block, and a connection still winding down would make
        # that read a race.
        for connection in self._connections:
            connection.join(timeout=_EXCHANGE_TIMEOUT)

    def _record_sni(self, stream: ssl.SSLObject, name: str | None, context: ssl.SSLContext) -> None:
        """The SNI as the handshake offered it. Signature fixed by ``ssl``."""
        del stream, context
        self.sni_names.append(name)

    def _serve(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._certificate.configure_cert(context)
        context.sni_callback = self._record_sni
        while not self._stop.is_set():
            try:
                raw, _ = self._socket.accept()
            except (TimeoutError, BlockingIOError):
                continue
            except OSError:
                return
            # One thread per connection: a client that keeps its connection
            # open must not stop the next one being accepted.
            connection = threading.Thread(target=self._answer, args=(context, raw), daemon=True)
            self._connections.append(connection)
            connection.start()

    def _answer(self, context: ssl.SSLContext, raw: socket.socket) -> None:
        """One connection, answering requests for as long as it is kept open.

        A client that refuses the certificate is what three of these tests are
        asserting, and it arrives here as an ``SSLError`` -- during the
        handshake, or on the first read under TLS 1.3, where the server
        finishes its side before the client has judged the chain. Either way
        it is recorded as the alert the client sent.
        """
        raw.settimeout(_EXCHANGE_TIMEOUT)
        try:
            with context.wrap_socket(raw, server_side=True) as tls:
                while True:
                    request = b""
                    while _HEADER_END not in request:
                        chunk = tls.recv(_READ_CHUNK)
                        if not chunk:
                            return
                        request += chunk
                    for line in request.decode("latin-1").splitlines():
                        field, _, value = line.partition(":")
                        if field.lower() == "host":
                            self.host_headers.append(value.strip())
                    tls.sendall(_RESPONSE)
        except (ssl.SSLError, OSError) as exc:
            self.refusals.append(str(exc))
        finally:
            raw.close()
