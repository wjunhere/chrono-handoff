"""HTTP servers that bind without asking DNS who they are.

``http.server.HTTPServer.server_bind`` fills in ``server_name`` by calling
``socket.getfqdn(host)`` -- a reverse lookup, inside the bind.  On a machine whose
resolver is slow or unreachable that lookup takes tens of seconds, and everything
that wanted to serve waits behind it.

Measured, not assumed: on a GitHub macOS runner ``socket.getfqdn("127.0.0.1")``
took **35.0 s**, and the conductor's own start-up marks put all 35 s in the panel
step, so `chrono daemon` served nothing for 35 s after it was started -- while its
log, once it finally appeared, looked completely healthy.  Nothing here needs the
fully-qualified name: the conductor and its panel are addressed by IP on loopback.
"""

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` whose bind path cannot block on reverse DNS."""

    daemon_threads = True

    def server_bind(self) -> None:
        # Deliberately *not* ``super().server_bind()``: that is where the lookup is.
        # ``TCPServer.server_bind`` is the part that actually binds, and it is also
        # what sets ``server_address`` -- which callers read back to learn the port
        # when they asked for port 0.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port
