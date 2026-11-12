"""The gRPC binding of A2A 1.0.0 at `cadl a2a-gateway` (Appendix G, G.13),
an optional extra: `pip install cadl-lang[grpc]`, which installs the
official `a2a-sdk` with its gRPC dependencies. The messages and the service
are those of the normative `a2a.proto` (package `lf.a2a.v1`, service
`A2AService`), as generated in `a2a.types.a2a_pb2` of `a2a-sdk`; cadl adds
no definitions of its own.

A2A 1.0.0 (https://a2a-protocol.org/v1.0.0/specification/):

- 10.1: gRPC over HTTP/2 with TLS; the service `A2AService` of
  `specification/a2a.proto`.
- 10.2: the service parameters (`a2a-version`) are gRPC metadata, with
  lowercase keys.
- 10.6: an error is a `google.rpc.Status` with the gRPC status of 5.4 and
  a `google.rpc.ErrorInfo` (reason as in 11.6, domain `a2a-protocol.org`)
  in its details, sent as the trailing metadata `grpc-status-details-bin`.
- 10.7: `SendStreamingMessage` and `SubscribeToTask` are server-streaming
  RPCs of `StreamResponse` messages.

A request is authenticated as one of the other bindings: the metadata
`authorization` carries `CADL-HMAC`, whose MAC covers the method `POST`,
the path of the RPC (`/lf.a2a.v1.A2AService/SendMessage`), and the
SHA-256 of the serialized request message as it arrived. The message is
then parsed strictly (no unknown fields), turned into the JSON of the
binding (ProtoJSON, as the JSON-RPC `params`), and decided, held,
recorded and passed on as a JSON-RPC request; the answer is turned back
into the response message of the RPC.
"""

from __future__ import annotations

import itertools
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import a2a_rest as A2ARest
from .gateway_core import INTERNAL_ERROR, INVALID_PARAMS, GatewayError

SERVICE = "lf.a2a.v1.A2AService"
# RPC -> (request message, response message, server streaming)
METHODS = {
    "SendMessage": ("SendMessageRequest", "SendMessageResponse", False),
    "SendStreamingMessage": ("SendMessageRequest", "StreamResponse", True),
    "GetTask": ("GetTaskRequest", "Task", False),
    "ListTasks": ("ListTasksRequest", "ListTasksResponse", False),
    "CancelTask": ("CancelTaskRequest", "Task", False),
    "SubscribeToTask": ("SubscribeToTaskRequest", "StreamResponse", True),
    "CreateTaskPushNotificationConfig": ("TaskPushNotificationConfig",
                                         "TaskPushNotificationConfig", False),
    "GetTaskPushNotificationConfig": ("GetTaskPushNotificationConfigRequest",
                                      "TaskPushNotificationConfig", False),
    "ListTaskPushNotificationConfigs": ("ListTaskPushNotificationConfigsRequest",
                                        "ListTaskPushNotificationConfigsResponse", False),
    "GetExtendedAgentCard": ("GetExtendedAgentCardRequest", "AgentCard", False),
    "DeleteTaskPushNotificationConfig": ("DeleteTaskPushNotificationConfigRequest",
                                         "Empty", False),
}
MAX_MESSAGE = 1 << 20
EXTRA = "pip install 'cadl-lang[grpc]'"


def modules():
    """(grpc, a2a_pb2, json_format, empty_pb2, status_pb2, error_details_pb2,
    unknown_fields), or GatewayError when the extra is not installed."""
    try:
        import grpc
        from a2a.types import a2a_pb2
        from google.protobuf import empty_pb2, json_format, unknown_fields
        from google.rpc import error_details_pb2, status_pb2
    except ImportError as e:
        raise GatewayError(f"--grpc-listen needs the gRPC extra ({EXTRA}): {e}") from None
    return grpc, a2a_pb2, json_format, empty_pb2, status_pb2, error_details_pb2, \
        unknown_fields


def _unknown(msg, unknown_fields) -> bool:
    """Whether a message or one of its submessages has fields that the
    definitions do not have."""
    if len(unknown_fields.UnknownFieldSet(msg)):
        return True
    for fd, value in msg.ListFields():
        if fd.message_type is None:
            continue
        if fd.message_type.GetOptions().map_entry:
            values = value.values() if fd.message_type.fields_by_name["value"].message_type \
                else []
        elif (fd.is_repeated if hasattr(fd, "is_repeated")
              else fd.label == fd.LABEL_REPEATED):
            values = value
        else:
            values = [value]
        if any(_unknown(v, unknown_fields) for v in values):
            return True
    return False


class GrpcBinding:
    """The gRPC service of a gateway (`A2AGateway`)."""

    def __init__(self, gw, mtls: bool) -> None:
        (self.grpc, self.pb, self.json_format, self.empty, self.status_pb,
         self.details_pb, self.unknown_fields) = modules()
        self.gw, self.mtls = gw, mtls
        self.ids = itertools.count(1)

    def _class(self, name: str):
        return self.empty.Empty if name == "Empty" else getattr(self.pb, name)

    def handler(self):
        grpc = self.grpc
        handlers = {}
        for name, (_, _, streaming) in METHODS.items():
            if streaming:
                handlers[name] = grpc.unary_stream_rpc_method_handler(
                    self._streaming(name), request_deserializer=None,
                    response_serializer=None)
            else:
                handlers[name] = grpc.unary_unary_rpc_method_handler(
                    self._unary(name), request_deserializer=None, response_serializer=None)
        return grpc.method_handlers_generic_handler(SERVICE, handlers)

    # --- errors -------------------------------------------------------------------

    def _abort(self, context, code: int | None, message: str, plain: str | None = None):
        """End the RPC with the error of the JSON-RPC code `code` (10.6), or
        with the gRPC status `plain` and no A2A reason."""
        grpc = self.grpc
        if plain is not None:
            context.abort(getattr(grpc.StatusCode, plain), message)
        _, status_name, reason = A2ARest.CODES.get(code, (500, "INTERNAL", "INTERNAL_ERROR"))
        status_code = getattr(grpc.StatusCode, status_name)
        info = self.details_pb.ErrorInfo(reason=reason, domain=A2ARest.DOMAIN)
        if code not in A2ARest.CODES:
            info.metadata["code"] = str(code)
        st = self.status_pb.Status(code=status_code.value[0], message=message)
        st.details.add().Pack(info)
        context.set_trailing_metadata((("grpc-status-details-bin", st.SerializeToString()),))
        context.abort(status_code, message)

    def _reply_error(self, context, reply) -> None:
        body = reply.body if isinstance(reply.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        if reply.status == 401:
            self._abort(context, None, "Unauthorized", "UNAUTHENTICATED")
        if reply.status == 503:
            self._abort(context, None, str(err.get("message") or "unavailable"),
                        "UNAVAILABLE")
        self._abort(context, err.get("code"), str(err.get("message")))

    # --- requests -----------------------------------------------------------------

    def _metadata(self, context, key: str) -> list[str]:
        out = []
        for k, v in context.invocation_metadata() or ():
            if k.lower() == key:
                out.append(v if isinstance(v, str) else "")
        return out

    def _peer(self, context) -> str | None:
        if not self.mtls:
            return None
        from .a2a_gateway import certificate_actor
        names = (context.auth_context() or {}).get("x509_common_name") or []
        if len(names) != 1:
            return ""
        try:
            cn = names[0].decode("utf-8")
        except UnicodeDecodeError:
            return ""
        actor = certificate_actor({"subject": ((("commonName", cn),),)})
        return actor if actor is not None and actor in self.gw.cert_actors() else ""

    def _call(self, name: str, raw: bytes, context):
        """Authenticate, parse and dispatch one RPC: the Reply of the
        gateway."""
        gw = self.gw
        if gw.stopped:
            self._abort(context, None, "the gateway has stopped", "UNAVAILABLE")
        peer = self._peer(context)
        if peer == "":
            gw.refused("handshake_failed", "unknown_certificate")
            self._abort(context, None, "Unauthorized", "UNAUTHENTICATED")
        actor, why = gw._authenticate(self._metadata(context, "authorization"),
                                      f"/{SERVICE}/{name}", raw, peer, "POST")
        if actor is None:
            with gw.lock:
                gw._unauthenticated(why)
            self._abort(context, None, "Unauthorized", "UNAUTHENTICATED")

        def handle():
            from .a2a_gateway import _VERSION_RE, VERSION_NOT_SUPPORTED
            version = self._metadata(context, "a2a-version")
            if len(version) != 1 or not _VERSION_RE.fullmatch(version[0].strip()):
                return gw._rpc_error(None, VERSION_NOT_SUPPORTED,
                                     "VersionNotSupportedError: the gateway speaks A2A 1.0 "
                                     "only (metadata a2a-version: 1.0)")
            cls = self._class(METHODS[name][0])
            try:
                msg = cls.FromString(raw)
            except Exception:       # noqa: BLE001 - DecodeError of either runtime
                return gw._rpc_error(None, INVALID_PARAMS, "Invalid params: the request "
                                                           "is not a " + cls.__name__)
            if _unknown(msg, self.unknown_fields):
                return gw._rpc_error(None, INVALID_PARAMS, "Invalid params: the request has "
                                                           "fields that a2a.proto does not "
                                                           "define")
            params = self.json_format.MessageToDict(msg)
            id_ = f"grpc-{next(self.ids)}"
            return gw._dispatch(actor, name, id_, params,
                                lambda: not context.is_active())
        return gw._in_slot(actor, handle)

    def _response(self, name: str, obj: Any, cls) -> bytes | None:
        try:
            return self.json_format.ParseDict(obj, cls()).SerializeToString()
        except (self.json_format.ParseError, TypeError, ValueError):
            return None

    def _unary(self, name: str):
        def rpc(raw: bytes, context) -> bytes:
            reply = self._call(name, raw, context)
            if reply is None:
                self._abort(context, None, "cancelled", "CANCELLED")
            body = reply.body
            if reply.status != 200 or not isinstance(body, dict) or "result" not in body:
                self._reply_error(context, reply)
            data = self._response(name, body["result"], self._class(METHODS[name][1]))
            if data is None:
                self._invalid(context, f"{name}: the result is not a "
                                       f"{METHODS[name][1]} of a2a.proto")
            return data
        return rpc

    def _invalid(self, context, why: str) -> None:
        self.gw._invalid(why[:200], None)
        self._abort(context, -32006, "InvalidAgentResponseError: the agent gave no valid "
                                     "response")

    def _streaming(self, name: str):
        def rpc(raw: bytes, context):
            reply = self._call(name, raw, context)
            if reply is None:
                self._abort(context, None, "cancelled", "CANCELLED")
            if reply.stream is None:
                body = reply.body
                if reply.status == 200 and isinstance(body, dict) and "result" in body:
                    # A stream of one event (a denial, a submitted task).
                    data = self._response(name, body["result"], self.pb.StreamResponse)
                    if data is None:
                        self._invalid(context, f"{name}: not a StreamResponse")
                    yield data
                    return
                self._reply_error(context, reply)
            # A client that cancels ends the stream: the gateway's loops
            # watch `closed`, and the generator is closed when gRPC drops it.
            stream = reply.stream
            try:
                for chunk in stream:
                    error = chunk.startswith(b"event: error\n")
                    head = b"event: error\ndata: " if error else b"data: "
                    if not chunk.startswith(head):
                        continue                    # a comment of a held stream
                    obj = json.loads(chunk[len(head):])
                    if "result" in obj:
                        data = self._response(name, obj["result"], self.pb.StreamResponse)
                        if data is None:
                            self._invalid(context, f"{name}: an event that is not a "
                                                   f"StreamResponse")
                        yield data
                        continue
                    err = obj.get("error") or {}
                    self._abort(context, err.get("code"), str(err.get("message")))
            finally:
                stream.close()
        return rpc


def start(gw, listen: tuple[str, int], tls_cert: Path | None, tls_key: Path | None,
          client_ca: Path | None, max_workers: int):
    """Start the gRPC service of `gw` on `listen`: (server, port)."""
    grpc = modules()[0]
    binding = GrpcBinding(gw, mtls=client_ca is not None)
    server = grpc.server(
        ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cadl-grpc"),
        handlers=[binding.handler()], maximum_concurrent_rpcs=max_workers,
        options=[("grpc.max_receive_message_length", MAX_MESSAGE),
                 ("grpc.max_send_message_length", 16 << 20),
                 ("grpc.so_reuseport", 0)])
    host, port = listen
    address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    if tls_cert is not None:
        try:
            creds = grpc.ssl_server_credentials(
                [(Path(tls_key).read_bytes(), Path(tls_cert).read_bytes())],
                root_certificates=Path(client_ca).read_bytes() if client_ca else None,
                require_client_auth=client_ca is not None)
        except OSError as e:
            raise GatewayError(f"--grpc-listen: cannot read the TLS files: {e}") from None
        actual = server.add_secure_port(address, creds)
    else:
        actual = server.add_insecure_port(address)
    if not actual:
        raise GatewayError(f"--grpc-listen: cannot listen on {address}")
    server.start()
    return server, actual
