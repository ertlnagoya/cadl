"""The front listener of `cadl a2a-gateway --grpc-listen` (G.13): caps on
connections per address, per prefix and in all, the deadline to the first
call, the real client address carried to the gRPC server, and TLS that
passes through it.

In process against a small gRPC server (`Front` alone), and through the
gateway subprocess."""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cadl.a2a_grpc_front import Front, peer_key, prefix_key

from .a2a_certs import PKI
from .test_a2a_bindings import GrpcClient, events, grpc_message
from .test_a2a_gateway import Run, wait_for


@pytest.fixture
def grpc():
    return pytest.importorskip("grpc")


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return PKI(tmp_path_factory.mktemp("pki"), clients={"planner": "PLANNER"}, foreign={})


class Recorder:
    """The `refused` of a gateway."""

    def __init__(self) -> None:
        self.got: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def refused(self, event: str, why: str) -> None:
        with self.lock:
            self.got.append((event, why))

    def count(self, event: str, why: str) -> int:
        with self.lock:
            return self.got.count((event, why))


class Backend:
    """A Front in front of a gRPC server whose one method answers with the
    address that the Front reports for the peer of the call."""

    def __init__(self, grpc, unix: bool = True, deadline: float = 1.5, options=(),
                 **caps) -> None:
        self.grpc = grpc
        self.gw = Recorder()
        self.front = Front(self.gw, ("127.0.0.1", 0), deadline=deadline, unix=unix, **caps)

        def who(request, context):
            conn = self.front.client(context.peer())
            return (conn.host if conn is not None else "unknown").encode()

        def hang(request, context):
            self.front.client(context.peer())
            yield b"started"
            while context.is_active():
                time.sleep(0.05)
        self.server = grpc.server(ThreadPoolExecutor(8), handlers=[
            grpc.method_handlers_generic_handler(
                "t.T", {"Who": grpc.unary_unary_rpc_method_handler(who),
                        "Hang": grpc.unary_stream_rpc_method_handler(hang)})],
            options=list(options))
        actual = self.server.add_insecure_port(self.front.backend)
        assert actual
        if not unix:
            self.front.set_backend_port(actual)
        self.server.start()
        self.front.start()
        self.port = self.front.port

    def channel(self):
        return self.grpc.insecure_channel(f"127.0.0.1:{self.port}",
                                          options=[("grpc.use_local_subchannel_pool", 1)])

    def who(self, channel) -> str:
        return channel.unary_unary("/t.T/Who")(b"", timeout=10).decode()

    def raw(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=10)

    def close(self) -> None:
        self.front.stop()
        self.server.stop(None).wait(5)
        self.front.cleanup()


def closed(s: socket.socket, timeout: float = 10.0) -> bool:
    """Whether the gateway closes or resets `s` within `timeout`."""
    s.settimeout(timeout)
    try:
        while True:
            data = s.recv(4096)
            if not data:
                return True
    except (ConnectionResetError, BrokenPipeError):
        return True
    except socket.timeout:
        return False


def test_keys():
    assert peer_key("192.0.2.7") == "192.0.2.7"
    assert peer_key("::ffff:192.0.2.7") == "192.0.2.7"
    assert peer_key("2001:db8:1:2:3::4") == "2001:db8:1:2::/64"
    assert prefix_key("192.0.2.7") == "192.0.2.0/24"
    assert prefix_key("::ffff:192.0.2.7") == "192.0.2.0/24"
    assert prefix_key("2001:db8:1:2:3::4") == "2001:db8:1::/48"


@pytest.mark.parametrize("unix", [True, False], ids=["unix", "loopback"])
def test_client_address_is_carried(grpc, unix):
    """The gRPC server sees the backend connection; the Front maps it back
    to the accepted client. Over loopback, a connection that does not come
    through the Front is not one of its peers."""
    if unix and not hasattr(socket, "AF_UNIX"):
        pytest.skip("no Unix sockets")
    b = Backend(grpc, unix=unix)
    try:
        chans = [b.channel() for _ in range(3)]
        assert [b.who(c) for c in chans] == ["127.0.0.1"] * 3
        assert b.front.total == 3
        if not unix:
            direct = grpc.insecure_channel(f"127.0.0.1:{b.front.backend_port}")
            assert b.who(direct) == "unknown"
            direct.close()
        for c in chans:
            c.close()
        wait_for(lambda: b.front.total == 0, "the connections to end")
        assert not b.front.peers
    finally:
        b.close()


def test_flood_from_one_address(grpc):
    """Beyond 4 connections of one address every new one is reset at once
    and recorded (`overloaded`, `connections`); the four places come back
    when their holders are closed at the deadline."""
    b = Backend(grpc, deadline=2.0, max_per_address=4)
    try:
        held = [b.raw() for _ in range(4)]
        wait_for(lambda: b.front.total == 4, "the four connections")
        t0 = time.monotonic()
        flood = [b.raw() for _ in range(100)]
        assert all(closed(s, 5) for s in flood)
        assert time.monotonic() - t0 < 5
        assert b.gw.count("overloaded", "connections") == 100
        ch = b.channel()
        with pytest.raises(grpc.RpcError) as e:
            ch.unary_unary("/t.T/Who")(b"", timeout=1)
        assert e.value.code() in (grpc.StatusCode.UNAVAILABLE,
                                  grpc.StatusCode.DEADLINE_EXCEEDED)
        assert all(closed(s, 10) for s in held)
        assert b.gw.count("read_timeout", "grpc handshake") == 4
        wait_for(lambda: b.front.total == 0, "the places to come back")
        assert b.who(b.channel()) == "127.0.0.1"
        for s in held + flood:
            s.close()
        ch.close()
    finally:
        b.close()


def test_caps_per_prefix_and_in_all(grpc):
    """With a large cap per address, the caps per prefix and in all
    still hold (here 127.0.0.1 stands for many addresses of one /24)."""
    for caps, n in (({"max_per_address": 100, "max_per_prefix": 3}, 3),
                    ({"max_per_address": 100, "max_connections": 5}, 5)):
        b = Backend(grpc, deadline=5.0, **caps)
        try:
            held = [b.raw() for _ in range(n)]
            wait_for(lambda: b.front.total == n, "the connections")
            extra = [b.raw() for _ in range(10)]
            assert all(closed(s, 5) for s in extra)
            assert b.front.total == n
            assert b.gw.count("overloaded", "connections") == 10
            for s in held + extra:
                s.close()
        finally:
            b.close()


def test_slowloris_and_half_closed(grpc):
    """A connection that sends its first bytes one at a time, one that
    sends nothing, and one that never sends a whole HTTP/2 preface are
    reset at the deadline; one that half-closes at once is closed at
    once. None of them reaches a call."""
    b = Backend(grpc, deadline=1.5)
    try:
        slow, silent, partial = b.raw(), b.raw(), b.raw()
        partial.sendall(b"PRI * HTTP/2.0\r\n")
        stop = threading.Event()

        def drip():
            for byte in b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n":
                if stop.wait(0.2):
                    return
                try:
                    slow.send(bytes([byte]))
                except OSError:
                    return
        threading.Thread(target=drip, daemon=True).start()
        half = b.raw()
        half.shutdown(socket.SHUT_WR)
        t0 = time.monotonic()
        assert closed(half, 1.0)
        assert all(closed(s, 5) for s in (slow, silent, partial))
        assert 1.0 < time.monotonic() - t0 < 5
        stop.set()
        assert b.gw.count("read_timeout", "grpc handshake") == 3
        wait_for(lambda: b.front.total == 0, "the places to come back")
        for s in (slow, silent, partial, half):
            s.close()
    finally:
        b.close()


def test_established_connection_outlives_the_deadline(grpc):
    """A connection that has carried a call is not closed at the deadline."""
    b = Backend(grpc, deadline=1.0)
    try:
        ch = b.channel()
        assert b.who(ch) == "127.0.0.1"
        time.sleep(2.0)
        assert b.front.total == 1
        assert b.who(ch) == "127.0.0.1"
        assert b.gw.got == []
        ch.close()
    finally:
        b.close()


@pytest.mark.parametrize("streaming", [False, True], ids=["idle", "streaming"])
def test_half_open_after_a_call(grpc, streaming):
    """A client that vanishes after a call (here a relay that stops passing
    bytes without closing) loses its connection: without a call by the
    gRPC server's idle limit, and with a call open (a stream) when its
    keepalive ping is not answered; the Front then closes its side and the
    place comes back. The gateway uses 120 s, and 60 s and 20 s; this
    test 1 s and 0.5 s (grpcio 1.85 times the answer to a keepalive ping
    with `grpc.http2.ping_timeout_ms`)."""
    b = Backend(grpc, deadline=30.0, options=[("grpc.max_connection_idle_ms", 1000),
                                              ("grpc.keepalive_time_ms", 500),
                                              ("grpc.keepalive_timeout_ms", 500),
                                              ("grpc.http2.ping_timeout_ms", 500)])
    relay = socket.socket()
    relay.bind(("127.0.0.1", 0))
    relay.listen(1)
    frozen = threading.Event()
    keep = []

    def pump(a, c):
        try:
            while True:
                data = a.recv(65536)
                if not data or frozen.is_set():
                    return
                c.sendall(data)
        except OSError:
            return

    def serve():
        a, _ = relay.accept()
        c = socket.create_connection(("127.0.0.1", b.port))
        keep.extend((a, c))
        threading.Thread(target=pump, args=(a, c), daemon=True).start()
        threading.Thread(target=pump, args=(c, a), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    try:
        ch = grpc.insecure_channel(f"127.0.0.1:{relay.getsockname()[1]}")
        assert b.who(ch) == "127.0.0.1"
        if streaming:
            it = ch.unary_stream("/t.T/Hang")(b"", timeout=60)
            assert next(it) == b"started"
        assert b.front.total == 1
        frozen.set()
        wait_for(lambda: b.front.total == 0, "the dead connection to end", timeout=20)
        if streaming:
            it.cancel()
        ch.close()
    finally:
        for s in keep + [relay]:
            s.close()
        b.close()


# === Through the gateway ===========================================================

def _run(tmp_path, *extra, listen="127.0.0.1:0"):
    pf = tmp_path / "grpc-port"
    r = Run(tmp_path, extra=("--grpc-listen", listen, "--grpc-port-file", str(pf), *extra))
    wait_for(lambda: pf.exists() and pf.read_text().strip(), "the port")
    return r, int(pf.read_text())


def test_gateway_connection_flood(tmp_path, grpc, capsys):
    """Through the gateway: one address holds its 4 connections with
    nothing sent; a flood of 300 more is reset and recorded at the capped
    rate; a call of the same address is refused until the deadline resets
    the four, then served; the replay is byte-identical."""
    pb = pytest.importorskip("a2a.types.a2a_pb2")
    r, port = _run(tmp_path, "--read-timeout", "3")
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(4)]
    time.sleep(0.3)
    flood = [socket.create_connection(("127.0.0.1", port)) for _ in range(300)]
    assert all(closed(s, 5) for s in flood)
    g = GrpcClient(r, grpc, pb, port)
    with pytest.raises(grpc.RpcError):
        g.channel.unary_unary("/lf.a2a.v1.A2AService/SendMessage")(b"", timeout=1)
    assert all(closed(s, 10) for s in held)
    g.channel.close()
    g = GrpcClient(r, grpc, pb, port)
    got = wait_for(lambda: _try(g, pb), "a call after the deadline")
    assert got.task.id == "up-task-1"
    g.channel.close()
    for s in held + flood:
        s.close()
    r.stop()
    refused = [why for _, why in events(r.audit, "overloaded")]
    assert refused and all(w.split(";")[0] == "connections" for w in refused)
    # At most 20 records a second; the next record counts those left out.
    left_out = sum(int(w.split("=")[1]) for w in refused if ";" in w)
    assert len(refused) < 300 and len(refused) + left_out <= 302
    assert [why for _, why in events(r.audit, "read_timeout")] == ["grpc handshake"] * 4
    assert r.agent.sent() == ["summarize"]
    r.replay_check()
    capsys.readouterr()


def _try(g, pb):
    try:
        return g.call("PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                      pb.SendMessageResponse)
    except Exception:       # noqa: BLE001 - the places are not back yet
        return None


def test_gateway_tls_call_served_while_an_address_holds_its_connections(
        tmp_path, grpc, pki, capsys):
    """Mutual TLS through the front listener on [::] (IPv4 and IPv6):
    127.0.0.1 holds its 4 connections, two never starting TLS, one
    sending a ClientHello a byte at a time, one finishing TLS without a
    call; a client of ::1 with its certificate is served meanwhile, and
    the actor of its certificate is still checked. The four are reset at
    the deadline."""
    pb = pytest.importorskip("a2a.types.a2a_pb2")
    try:
        probe = socket.socket(socket.AF_INET6)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("no IPv6 loopback")
    r, port = _run(tmp_path, *pki.gateway_args(), "--read-timeout", "4",
                   listen="[::]:0")
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    ctx = pki.context("planner")
    ctx.set_alpn_protocols(["h2"])
    tls = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port)),
                          server_hostname="localhost")
    held.append(tls)
    stop = threading.Event()

    def drip():
        for byte in b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03":
            if stop.wait(0.3):
                return
            try:
                held[0].send(bytes([byte]))
            except OSError:
                return
    threading.Thread(target=drip, daemon=True).start()
    flood = [socket.create_connection(("127.0.0.1", port)) for _ in range(20)]
    assert all(closed(s, 5) for s in flood)
    key = (pki.dir / "planner.key").read_bytes()
    chain = (pki.dir / "planner.pem").read_bytes()
    creds = grpc.ssl_channel_credentials(pki.ca.read_bytes(), key, chain)
    g = GrpcClient(r, grpc, pb, port)
    g.channel.close()
    g.channel = grpc.secure_channel(f"[::1]:{port}", creds, options=[
        ("grpc.ssl_target_name_override", "localhost")])
    got = g.call("PLANNER", "SendMessage", grpc_message(pb, "summarize"),
                 pb.SendMessageResponse)
    assert got.task.id == "up-task-1"
    assert g.error(g.call, "OPERATOR", "SendMessage", grpc_message(pb, "summarize"),
                   pb.SendMessageResponse)[0] == "UNAUTHENTICATED"
    # Served while the four are still open.
    assert not closed(held[1], 0.05) and not closed(held[2], 0.05)
    v4 = grpc.secure_channel(f"127.0.0.1:{port}", creds,
                             options=[("grpc.use_local_subchannel_pool", 1)])
    with pytest.raises(grpc.RpcError):
        v4.unary_unary("/lf.a2a.v1.A2AService/SendMessage")(b"", timeout=1)
    v4.close()
    assert all(closed(s, 10) for s in held)
    stop.set()
    g.channel.close()
    for s in held + flood:
        s.close()
    r.stop()
    assert [why for _, why in events(r.audit, "read_timeout")] == ["grpc handshake"] * 4
    assert [why for _, why in events(r.audit, "overloaded")][0] == "connections"
    assert r.agent.sent() == ["summarize"]
    r.replay_check()
    capsys.readouterr()


def test_gateway_options_must_be_positive(tmp_path, capsys):
    from cadl.cli import main

    from .test_a2a_gateway import CADL, CLIENT_KEYS, _ring
    assert main(["a2a-gateway", str(CADL), "--agent-actor", "SUMMARIZER", "--upstream",
                 "http://127.0.0.1:9", "--client-keys", str(_ring(tmp_path, CLIENT_KEYS,
                                                                  "clients")),
                 "--audit", str(tmp_path / "a.jsonl"),
                 "--grpc-listen", "127.0.0.1:0", "--max-grpc-connections-per-address",
                 "0"]) == 2
    assert "must be positive" in capsys.readouterr().err
