"""The front listener of the gRPC binding of `cadl a2a-gateway` (Appendix
G, G.13), standard library only.

`grpcio` has no hook where a connection could be counted or refused before
its calls, so the gateway accepts the TCP connections of `--grpc-listen`
itself, in one asyncio thread, and splices each accepted connection,
byte for byte, to the gRPC server, which listens on a Unix socket in a
directory of mode 0700 that only the user that runs the gateway can reach
(where there are no Unix sockets, on 127.0.0.1 with a port chosen by the
system; other local processes can then connect to that port without the
caps, and their calls are refused). TLS and mutual TLS are not terminated
here: the bytes are passed on as they come, so the gRPC server does the
handshake, sees the client certificate and keeps its identities as before.
A gateway killed with SIGKILL leaves its directory (`cadl-grpc-*` in the
temporary directory) behind.

The listener bounds:

- the open connections of one remote address (an IPv4 address, or the /64
  of an IPv6 address), of one IPv4 /24 or IPv6 /48, and in all; a
  connection beyond the cap of its address or prefix is reset at once and
  recorded at the capped rate (`overloaded`, reason `connections`). A
  connection that finds only the cap in all full waits up to `wait`
  seconds for a place (at most `max_connections` wait, and a waiting
  connection counts against the caps of its address and prefix); a
  place that comes back goes to the waiting connection whose address,
  then whose prefix, holds the fewest connections, before any new
  connection. One that gets none is reset and recorded as above. A
  connection counts until its sockets are closed;
- the time to the first call: a connection on which no call has reached
  the gateway within the deadline (`--read-timeout`, counted from the
  accept, or from the end of its wait) is reset and recorded
  (`read_timeout`, reason `grpc handshake`). This covers a client that
  sends nothing, one that never starts or never finishes TLS, one that
  sends its handshake byte by byte, and a half-open connection. A call
  that gRPC refuses before a handler runs (an unknown method, or beyond
  the workers) counts too: `a2a_grpc.hook_rejections` tells `client` of
  it;
- the age: gRPC sends GOAWAY to a connection at `max_age` (with a jitter
  of 10 %) and closes it after `age_grace`; a connection still open at
  `lifetime()` from the accept (or the end of its wait) is reset and
  recorded (`read_timeout`, reason `grpc age`);
- the time a side may refuse the bytes for the other: when the bytes
  that wait for the client (beyond `PENDING`) or for the gRPC server
  (beyond the high-water mark of the transport) do not drain to its
  low-water mark within `DRAIN_TIMEOUT` seconds, both sides are reset
  and recorded
  (`read_timeout`, reason `grpc stalled`). The gRPC server is read on
  while up to `PENDING` bytes wait for the client, so that its closing is
  seen at once; then the client's side is closed after what was sent to
  it, or reset after `CLOSE_GRACE` seconds. A client that stops reading
  therefore keeps its connection at most until the gRPC server closes it
  (its keepalive ping, which the client does not answer, ends a
  connection with a call open after 60 + 20 seconds, the idle limit one
  without calls after 2 minutes) plus `CLOSE_GRACE`, or for
  `DRAIN_TIMEOUT` once more than `PENDING` bytes wait.

After its first call the gRPC server closes a connection that has had no
call for 2 minutes, and one with a call open whose keepalive ping is not
answered within 20 seconds; the listener then closes the client's side.

The real client address is carried to the gRPC server: each connection to
the backend is made from its own address (a Unix socket bound to a path of
its own, or a TCP port of its own), which gRPC reports as the peer of its
calls, and `client` maps that peer back to the accepted connection. The
caps of the binding on calls not yet authenticated are therefore keyed by
the real client address, as before; a call whose peer is not a connection
of the listener is refused.

Refusals are recorded by a thread of their own, so that the audit log and
the lock of the gateway never hold up the relay.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import itertools
import os
import re
import shutil
import socket
import tempfile
import threading
from typing import Any

from .gateway_core import log

CHUNK = 1 << 16
CLOSE_GRACE = 5.0          # seconds to flush a connection that is closed
DRAIN_TIMEOUT = 30.0       # seconds for written bytes to go out
PENDING = 1 << 18          # bytes for the client that may wait while the
                           # gRPC server is read on
AGE_MARGIN = 10.0          # seconds past the age and its grace before the
                           # listener resets a connection that gRPC kept
_PLAIN = re.compile(r"[A-Za-z0-9/._-]+")


def _bump(table: dict, key: str, by: int) -> None:
    n = table.get(key, 0) + by
    if n > 0:
        table[key] = n
    else:
        table.pop(key, None)


class _Waiter:
    """A connection that waits for a place."""

    __slots__ = ("key", "prefix", "seq", "future")

    def __init__(self, key: str, prefix: str, seq: int, future) -> None:
        self.key, self.prefix, self.seq, self.future = key, prefix, seq, future


def peer_key(host: str) -> str:
    """The key of the cap per address: an IPv4 address, or the /64 of an
    IPv6 address; an IPv4-mapped IPv6 address counts as its IPv4 address
    (as `_Server.peer_key` of the HTTP listener)."""
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return host
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def prefix_key(host: str) -> str:
    """The key of the cap per prefix: the /24 of an IPv4 address (also
    IPv4-mapped), the /48 of an IPv6 address."""
    try:
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return host
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    bits = 24 if ip.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))


class Connection:
    """An accepted connection: the address of the client, and whether a
    call on it has reached the gateway (or was refused by gRPC)."""

    __slots__ = ("host", "called", "client", "backend")

    def __init__(self, host: str) -> None:
        self.host = host
        self.called = False
        self.client: Any = None
        self.backend: Any = None


class _Recorder:
    """Records refusals for the front listener in a thread of its own:
    the relay thread only counts them, so a busy audit log or a held lock
    of the gateway never stops the relay. Memory is one counter per kind
    of refusal."""

    def __init__(self, gw) -> None:
        self.gw = gw
        self.lock = threading.Lock()
        self.pending: dict[tuple[str, str], int] = {}
        self.wake = threading.Event()
        self.done = False
        self.thread = threading.Thread(target=self._run, name="cadl-grpc-refusals",
                                       daemon=True)
        self.thread.start()

    def refused(self, event: str, why: str) -> None:
        with self.lock:
            self.pending[(event, why)] = self.pending.get((event, why), 0) + 1
        self.wake.set()

    def _flush(self) -> None:
        with self.lock:
            pending, self.pending = self.pending, {}
        for (event, why), n in pending.items():
            for _ in range(n):
                try:
                    self.gw.refused(event, why)
                except Exception:       # noqa: BLE001 - the relay goes on
                    pass

    def _run(self) -> None:
        while not self.done:
            self.wake.wait(1.0)
            self.wake.clear()
            self._flush()

    def close(self) -> None:
        self.done = True
        self.wake.set()
        self.thread.join(5)
        self._flush()


def _listen_socket(family: int, addr: tuple) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # SO_REUSEADDR on Windows lets another socket take the port.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            # [::] takes IPv4 clients too, as the listener of gRPC did; any
            # other IPv6 address only IPv6 ones.
            unspecified = ipaddress.ip_address(addr[0].split("%", 1)[0]).is_unspecified
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if unspecified else 1)
        sock.bind(addr)
        sock.listen(128)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def _listen_sockets(host: str, port: int, attempts: int = 8) -> list[socket.socket]:
    """Listening sockets for the addresses of `host` (as gRPC binds
    `localhost` to 127.0.0.1 and ::1), all on the same port. As with
    gRPC, an address that cannot be bound (::1 where IPv6 is off) is left
    out and logged, as long as one is bound; with port 0, a port that the
    first address got but a later one cannot take is given up for another
    one."""
    infos, seen = [], set()
    for family, _, _, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                                    flags=socket.AI_PASSIVE):
        if family in (socket.AF_INET, socket.AF_INET6) and (family, addr[0]) not in seen:
            seen.add((family, addr[0]))
            infos.append((family, addr))
    if not infos:
        raise OSError(f"no address for {host}")
    for attempt in range(attempts):
        socks: list[socket.socket] = []
        skipped: list[str] = []
        retry = False
        try:
            for family, addr in infos:
                if socks:
                    addr = (addr[0], socks[0].getsockname()[1], *addr[2:])
                try:
                    socks.append(_listen_socket(family, addr))
                except OSError as e:
                    if socks and port == 0 and e.errno == errno.EADDRINUSE \
                            and attempt + 1 < attempts:
                        retry = True
                        break
                    skipped.append(f"{addr[0]}: {e}")
        except BaseException:
            for sock in socks:
                sock.close()
            raise
        if retry:
            for sock in socks:
                sock.close()
            continue
        if not socks:
            raise OSError(f"cannot listen on {host}: {'; '.join(skipped)}")
        for why in skipped:
            log("cadl a2a-gateway", f"--grpc-listen: {why}; the other addresses are used")
        return socks
    raise OSError(f"cannot find a free port for every address of {host}")


class Front:
    """The listener of `--grpc-listen` in front of the gRPC server.

    `gw` needs only `refused(event, why)`. Create it (it binds `listen`,
    so that an error comes before anything starts), give `backend` to the
    gRPC server, then `start()`; `stop_accepting()` closes the listening
    sockets, `stop()` resets every connection, and `cleanup()` removes the
    directory of the Unix socket."""

    def __init__(self, gw, listen: tuple[str, int], deadline: float = 10.0,
                 max_connections: int = 64, max_per_address: int = 4,
                 max_per_prefix: int = 16, unix: bool | None = None,
                 drain_timeout: float = DRAIN_TIMEOUT, max_age: float | None = None,
                 age_grace: float = 0.0, wait: float = 0.0) -> None:
        self.gw = gw
        self.deadline = deadline
        self.max_age = max_age
        self.age_grace = age_grace
        self.wait = wait
        self.waiters: list[_Waiter] = []
        self.waiting_address: dict[str, int] = {}
        self.waiting_prefix: dict[str, int] = {}
        self.seq = itertools.count()
        self.drain_timeout = drain_timeout
        self.max_connections = max_connections
        self.max_per_address = max_per_address
        self.max_per_prefix = max_per_prefix
        self.unix = hasattr(socket, "AF_UNIX") if unix is None else unix
        self.lock = threading.Lock()
        self.total = 0
        self.address_count: dict[str, int] = {}
        self.prefix_count: dict[str, int] = {}
        self.peers: dict[str, Connection] = {}
        self.ids = itertools.count(1)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.stopping: asyncio.Event | None = None
        self.servers: list = []
        self.recorder: _Recorder | None = None
        self.dir: str | None = None
        host, port = listen
        self.socks = _listen_sockets(host, port)
        self.port = self.socks[0].getsockname()[1]
        try:
            if self.unix:
                self.dir = tempfile.mkdtemp(prefix="cadl-grpc-")    # mode 0700
                if (len(self.dir) > 80 or not _PLAIN.fullmatch(self.dir)) \
                        and os.path.isdir("/tmp"):
                    # sun_path holds about 100 bytes, and gRPC reports the
                    # peer as "unix:" and the path, which has to compare as
                    # it is.
                    os.rmdir(self.dir)
                    self.dir = tempfile.mkdtemp(prefix="cadl-grpc-", dir="/tmp")
                self.path = os.path.join(self.dir, "grpc")
                self.backend = "unix:" + self.path
            else:
                self.backend_port = 0
                self.backend = "127.0.0.1:0"
        except BaseException:
            for sock in self.socks:
                sock.close()
            self.cleanup()
            raise

    def set_backend_port(self, port: int) -> None:
        """Without Unix sockets: the port that the gRPC server took."""
        self.backend_port = port

    # --- the peers of the gRPC server -------------------------------------------

    def client(self, peer: str) -> Connection | None:
        """The accepted connection whose calls gRPC reports with `peer`,
        marked as having carried a call; None for any other peer."""
        with self.lock:
            conn = self.peers.get(peer)
        if conn is not None:
            conn.called = True
        return conn

    # --- the thread -------------------------------------------------------------

    def start(self) -> None:
        """Start the relay thread; raises what keeps it from listening."""
        ready = threading.Event()
        failure: list[BaseException] = []
        self.recorder = _Recorder(self.gw)

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            try:
                self.loop.run_until_complete(self._main(ready))
            except BaseException as e:      # noqa: BLE001 - given to start()
                failure.append(e)
            finally:
                ready.set()
                self.loop.close()
        self.thread = threading.Thread(target=run, name="cadl-grpc-front", daemon=True)
        self.thread.start()
        if not ready.wait(10):
            self.stop()
            raise OSError("the gRPC front listener did not start")
        if failure:
            self.stop()
            raise failure[0]

    def _call(self, fn) -> None:
        loop = self.loop
        if loop is not None and self.thread is not None and self.thread.is_alive():
            try:
                loop.call_soon_threadsafe(fn)
            except RuntimeError:
                pass                    # the loop has ended

    def stop_accepting(self) -> None:
        """Close the listening sockets; the connections go on."""
        def close() -> None:
            for server in self.servers:
                server.close()
        self._call(close)

    def stop(self) -> None:
        """Stop accepting and reset every connection."""
        if self.stopping is not None:
            self._call(self.stopping.set)
        if self.thread is not None:
            self.thread.join(10)
        for sock in self.socks:
            sock.close()
        if self.recorder is not None:
            self.recorder.close()

    def cleanup(self) -> None:
        """Remove the directory of the Unix socket (after the gRPC server
        has stopped)."""
        if self.dir is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None

    def _refused(self, event: str, why: str) -> None:
        if self.recorder is not None:
            self.recorder.refused(event, why)

    async def _main(self, ready: threading.Event) -> None:
        self.stopping = asyncio.Event()
        for sock in self.socks:
            self.servers.append(await asyncio.start_server(
                self._handle, sock=sock, backlog=128, limit=CHUNK))
        ready.set()
        try:
            await self.stopping.wait()
        finally:
            for server in self.servers:
                server.close()
            with self.lock:
                conns = list(self.peers.values())
            for conn in conns:
                for writer in (conn.client, conn.backend):
                    if writer is not None:
                        writer.transport.abort()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- one connection ---------------------------------------------------------

    def _take(self, key: str, prefix: str) -> str:
        """Take a place: "ok", or the cap that is full ("address",
        "prefix", "total"). Waiting connections count against the caps of
        their address and prefix."""
        with self.lock:
            if self.address_count.get(key, 0) + self.waiting_address.get(key, 0) \
                    >= self.max_per_address:
                return "address"
            if self.prefix_count.get(prefix, 0) + self.waiting_prefix.get(prefix, 0) \
                    >= self.max_per_prefix:
                return "prefix"
            if self.total >= self.max_connections or self.waiters:
                return "total"
            self._count(key, prefix)
            return "ok"

    def _count(self, key: str, prefix: str) -> None:
        self.total += 1
        self.address_count[key] = self.address_count.get(key, 0) + 1
        self.prefix_count[prefix] = self.prefix_count.get(prefix, 0) + 1

    async def _queue(self, key: str, prefix: str) -> bool:
        """Wait up to `wait` seconds for a place when all are taken: True
        when one was given. At most `max_connections` connections wait;
        when a place comes back it goes to the waiting connection whose
        address, then whose prefix, holds the fewest connections (then the
        one that came first), before any new connection."""
        with self.lock:
            if len(self.waiters) >= self.max_connections:
                return False
            w = _Waiter(key, prefix, next(self.seq),
                        asyncio.get_running_loop().create_future())
            self.waiters.append(w)
            _bump(self.waiting_address, key, 1)
            _bump(self.waiting_prefix, prefix, 1)
        try:
            await asyncio.wait({w.future}, timeout=self.wait)
        finally:
            with self.lock:
                if w in self.waiters:
                    self._unwait(w)
        if w.future.done() and not w.future.cancelled():
            return True
        w.future.cancel()
        return False

    def _unwait(self, w: "_Waiter") -> None:
        self.waiters.remove(w)
        _bump(self.waiting_address, w.key, -1)
        _bump(self.waiting_prefix, w.prefix, -1)

    def _grant(self) -> None:
        """Give the free places to waiting connections (in the loop
        thread, so that no new connection comes in between)."""
        with self.lock:
            while self.total < self.max_connections and self.waiters:
                w = min(self.waiters, key=lambda w: (self.address_count.get(w.key, 0),
                                                     self.prefix_count.get(w.prefix, 0),
                                                     w.seq))
                self._unwait(w)
                if w.future.done():
                    continue
                self._count(w.key, w.prefix)
                w.future.set_result(True)

    def _release(self, key: str, prefix: str) -> None:
        with self.lock:
            self.total -= 1
            _bump(self.address_count, key, -1)
            _bump(self.prefix_count, prefix, -1)
        self._grant()

    def lifetime(self) -> float | None:
        """The longest time from the accept (or the end of its wait) that a
        connection may stay after its first call: the deadline (the
        handshake), the age at which gRPC sends GOAWAY (with its jitter of
        10 %), the grace for the calls under way, and `AGE_MARGIN`; None
        without `max_age`."""
        if self.max_age is None:
            return None
        return self.deadline + 1.1 * self.max_age + self.age_grace + AGE_MARGIN

    async def _connect(self):
        """A connection to the gRPC server from an address of its own:
        (reader, writer, the peer string that gRPC reports for it)."""
        loop = asyncio.get_running_loop()
        if self.unix:
            path = os.path.join(self.dir, f"c{next(self.ids)}")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.setblocking(False)
                s.bind(path)
                await loop.sock_connect(s, self.path)
            except BaseException:
                s.close()
                raise
            finally:
                try:
                    os.unlink(path)     # the bound name stays with the socket
                except OSError:
                    pass
            name = "unix:" + path
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setblocking(False)
                await loop.sock_connect(s, ("127.0.0.1", self.backend_port))
            except BaseException:
                s.close()
                raise
            name = f"ipv4:127.0.0.1:{s.getsockname()[1]}"
        reader, writer = await asyncio.open_connection(sock=s, limit=CHUNK)
        return reader, writer, name

    async def _pump(self, reader, writer, pending: int = 0) -> str:
        """Pass the bytes of one direction: "eof" at the end of the input
        or an error, "stalled" when written bytes did not go out within
        `drain_timeout`. Up to `pending` bytes may wait to go out while the
        input is read on, so that its end (the gRPC server closing) is
        seen also when the client does not read."""
        try:
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    return "eof"
                writer.write(data)
                if writer.transport.get_write_buffer_size() <= pending:
                    continue
                try:
                    await asyncio.wait_for(writer.drain(), self.drain_timeout)
                except asyncio.TimeoutError:
                    return "stalled"
        except (OSError, asyncio.IncompleteReadError):
            return "eof"

    async def _close(self, conn: Connection, now: bool) -> None:
        """Close both sides: at once, or after what has been written (at
        most `CLOSE_GRACE` seconds); returns when the sockets are closed."""
        writers = [w for w in (conn.client, conn.backend) if w is not None]
        for w in writers:
            if now:
                w.transport.abort()
            elif not w.transport.is_closing():
                w.close()
        if now:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(w.wait_closed() for w in writers), return_exceptions=True),
                CLOSE_GRACE)
        except asyncio.TimeoutError:
            pass
        finally:
            for w in writers:
                w.transport.abort()     # a peer that reads nothing

    async def _handle(self, reader, writer) -> None:
        peername = writer.get_extra_info("peername")
        host = str(peername[0]) if peername else ""
        key, prefix = peer_key(host), prefix_key(host)
        verdict = self._take(key, prefix)
        if verdict == "total" and self.wait > 0:
            try:
                if await self._queue(key, prefix):
                    verdict = "ok"
            except BaseException:
                writer.transport.abort()
                raise
        if verdict != "ok":
            writer.transport.abort()
            self._refused("overloaded", "connections")
            return
        start = asyncio.get_running_loop().time()
        conn = Connection(host)
        conn.client = writer
        name = None
        tasks: set = set()
        now = True
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except OSError:
                    pass
            try:
                breader, bwriter, name = await self._connect()
            except OSError:
                return
            conn.backend = bwriter
            with self.lock:
                self.peers[name] = conn
            up = asyncio.ensure_future(self._pump(reader, bwriter))
            down = asyncio.ensure_future(self._pump(breader, writer, PENDING))
            # The end of either pump ends the relay: `down` reads the gRPC
            # server on while up to PENDING bytes wait for the client, so it
            # sees the server close (EOF) or reset (an error) also when the
            # client reads nothing; above that, the drain deadline holds.
            tasks = {up, down}
            done, _ = await asyncio.wait(tasks, timeout=self.deadline,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                if not conn.called:
                    self._refused("read_timeout", "grpc handshake")
                    return
                lifetime = self.lifetime()
                left = None if lifetime is None \
                    else max(0.0, start + lifetime - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait(tasks, timeout=left,
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    # gRPC did not close the connection at its age.
                    self._refused("read_timeout", "grpc age")
                    return
            if any(t.done() and not t.cancelled() and t.exception() is None
                   and t.result() == "stalled" for t in (up, down)):
                self._refused("read_timeout", "grpc stalled")
                return
            now = False
        finally:
            for t in tasks:
                t.cancel()
            if name is not None:
                with self.lock:
                    self.peers.pop(name, None)
            try:
                await self._close(conn, now)
            finally:
                # Released when the sockets are closed, so that every open
                # socket counts against the caps.
                self._release(key, prefix)
