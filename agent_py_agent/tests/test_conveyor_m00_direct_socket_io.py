# LLM: Conveyor M00 / R3-G0：DirectSocketIO 只去掉 SocketIO.readinto 里两次 C 层检查（_checkClosed/_checkReadable
#   会触发类型模块查找），读语义必须与标准 socket.makefile("rb") 一致：EOF、超时后下一次读抛错、非阻塞无数据返回
#   None、关闭后读抛 ValueError、_io_refs 计数、PrefetchedSocket 部分前缀回放、TLS 回环；同时钉住依赖的私有属性，
#   并给 G1 锁等待门禁提供 socketpair 读用例工厂。
# 模块用途: 固定 agent/common/direct_socket_io.py 的读合同（C1.4），不触外网、不读 git 历史。
from __future__ import annotations

import datetime
import errno
import http.client
import http.server
import io
import ipaddress
import socket
import ssl
import threading

import pytest

from agent_py_agent.agent.common.direct_socket_io import (
    DirectReadHTTPResponse,
    DirectSocketIO,
    makefile_rb,
)
from agent_py_agent.agent.gateway_parts.http_body_readiness import PrefetchedSocket

# 读超时用例的套接字超时（秒）：足够短让用例快，又足够长不受调度抖动影响。
READ_TIMEOUT_SECONDS = 0.05
# 所有阻塞等待（线程 join、服务端握手）的上限秒数。
WAIT_SECONDS = 10
# G1 用例每次往返的字节数（一个典型 SSE 小块）。
G1_CHUNK_BYTES = 64


# 函数用途: 建立一对本机流式套接字，用例结束后统一关闭。
@pytest.fixture
def pair():
    left, right = socket.socketpair()
    yield left, right
    left.close()
    right.close()


# 函数用途: 返回调用结果或异常的可比较形态（类型 + 文本）。
def _outcome(fn, *args):
    try:
        return ("ok", fn(*args))
    except Exception as exc:  # noqa: BLE001 - 差分比较需要捕获任意异常类型
        return ("raise", type(exc), str(exc))


def test_private_attributes_and_types_are_pinned(pair):
    left, _right = pair
    stock = socket.SocketIO(left, "r")
    for name in ("_sock", "_timeout_occurred", "_reading", "_writing", "_mode"):
        assert hasattr(stock, name), name
    assert hasattr(left, "_io_refs") and hasattr(left, "_decref_socketios")
    assert set(socket._blocking_errnos) <= {errno.EAGAIN, errno.EWOULDBLOCK}
    assert issubclass(DirectSocketIO, socket.SocketIO)
    assert isinstance(DirectReadHTTPResponse, type) and issubclass(DirectReadHTTPResponse, http.client.HTTPResponse)
    stock.close()


def test_makefile_rb_mirrors_socket_makefile(pair):
    left, right = pair
    before = left._io_refs
    reader = makefile_rb(left)
    assert isinstance(reader, io.BufferedReader) and type(reader.raw) is DirectSocketIO
    assert reader.raw.mode == "rb" and reader.mode == "rb" and reader.readable()
    assert left._io_refs == before + 1
    right.sendall(b"hello\nworld")
    right.shutdown(socket.SHUT_WR)
    assert reader.readline() == b"hello\n"
    assert reader.read() == b"world"
    assert reader.read1(10) == b""
    reader.close()
    assert left._io_refs == before
    raw = makefile_rb(left, 0)
    assert type(raw) is DirectSocketIO
    raw.close()
    sized = makefile_rb(left, 7)
    assert type(sized.raw) is DirectSocketIO
    sized.close()
    assert left._io_refs == before


def test_eof_and_close_parity(pair):
    left, right = pair
    stock, direct = left.makefile("rb", 0), makefile_rb(left, 0)
    right.close()
    buffer = bytearray(8)
    assert _outcome(direct.readinto, buffer) == _outcome(stock.readinto, buffer) == ("ok", 0)
    stock.close()
    direct.close()
    assert _outcome(direct.readinto, buffer) == _outcome(stock.readinto, buffer)
    assert _outcome(direct.readinto, buffer)[1] is ValueError


def test_timeout_then_next_read_raises_parity():
    results = []
    for factory in (lambda sock: sock.makefile("rb", 0), lambda sock: makefile_rb(sock, 0)):
        left, right = socket.socketpair()
        try:
            left.settimeout(READ_TIMEOUT_SECONDS)
            reader = factory(left)
            buffer = bytearray(8)
            first = _outcome(reader.readinto, buffer)
            right.sendall(b"late")
            second = _outcome(reader.readinto, buffer)
            results.append((first[:2], second))
            reader.close()
        finally:
            left.close()
            right.close()
    assert results[0] == results[1]
    assert issubclass(results[1][0][1], TimeoutError)
    assert results[1][1] == ("raise", OSError, "cannot read from timed out object")


def test_nonblocking_without_data_returns_none_parity(pair):
    left, right = pair
    left.setblocking(False)
    stock, direct = left.makefile("rb", 0), makefile_rb(left, 0)
    buffer = bytearray(8)
    assert direct.readinto(buffer) is None
    assert stock.readinto(buffer) is None
    right.sendall(b"ab")
    assert direct.readinto(buffer) == 2 and bytes(buffer[:2]) == b"ab"
    buffered_stock, buffered_direct = left.makefile("rb"), makefile_rb(left)
    assert _outcome(buffered_direct.read1, 4) == _outcome(buffered_stock.read1, 4)
    for item in (stock, direct, buffered_stock, buffered_direct):
        item.close()


def test_write_only_reader_raises_unsupported_operation_like_stock(pair):
    left, _right = pair
    stock, direct = socket.SocketIO(left, "w"), DirectSocketIO(left, "w")
    buffer = bytearray(4)
    assert _outcome(direct.readinto, buffer) == _outcome(stock.readinto, buffer)
    assert _outcome(direct.readinto, buffer)[1] is io.UnsupportedOperation
    stock.close()
    direct.close()


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 16])
def test_prefetched_socket_partial_prefix_replay_parity(chunk):
    outputs = []
    for factory in (lambda sock: sock.makefile("rb"), makefile_rb):
        left, right = socket.socketpair()
        try:
            prefetched = PrefetchedSocket(left, bytearray(b"HEAD"))
            reader = factory(prefetched)
            assert prefetched._io_refs == 1
            right.sendall(b"BODY-REST")
            right.shutdown(socket.SHUT_WR)
            pieces = []
            while True:
                piece = reader.read1(chunk)
                if not piece:
                    break
                pieces.append(piece)
            reader.close()
            assert prefetched._io_refs == 0
            outputs.append(pieces)
        finally:
            left.close()
            right.close()
    assert outputs[0] == outputs[1]
    assert b"".join(outputs[1]) == b"HEADBODY-REST"


def test_direct_read_http_response_parses_like_stock(pair):
    left, right = pair
    payload = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nX-Test: 1\r\n\r\nhello"
    right.sendall(payload)
    stock = http.client.HTTPResponse(left, method="GET")
    stock.begin()
    stock_view = (stock.status, stock.getheader("X-Test"), stock.read())
    stock.close()
    right.sendall(payload)
    direct = DirectReadHTTPResponse(left, method="GET")
    assert type(direct.fp.raw) is DirectSocketIO
    direct.begin()
    assert (direct.status, direct.getheader("X-Test"), direct.read()) == stock_view
    direct.close()


# 类用途: 回环 HTTP 处理器，返回固定正文，供 HTTPConnection.response_class 替换的端到端验证。
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - 标准库回调名
        body = b"chunk-" + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return None


def test_http_connection_with_direct_response_class_keeps_alive():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=WAIT_SECONDS)
        connection.response_class = DirectReadHTTPResponse
        for path in ("/a", "/b", "/c"):
            connection.request("GET", path)
            response = connection.getresponse()
            assert type(response) is DirectReadHTTPResponse
            assert response.fp.peek(0) is not None
            assert response.read() == b"chunk-" + path.encode()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(WAIT_SECONDS)


# 函数用途: 用 cryptography 现场生成 localhost 自签证书（不落入仓库、不触网）。
def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def test_tls_over_loopback_reads_through_sslsocket(tmp_path):
    cert_path, key_path = _self_signed(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    listener = socket.create_server(("127.0.0.1", 0))
    lines = [b"data: one\n", b"data: two\n", b"\n", b"data: three-" + b"x" * 20000 + b"\n"]
    failures = []

    def serve() -> None:
        try:
            raw, _ = listener.accept()
            with server_context.wrap_socket(raw, server_side=True) as tls:
                for line in lines:
                    tls.sendall(line)
        except Exception as exc:  # noqa: BLE001 - 服务端线程的失败交给主线程断言
            failures.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        client_context = ssl.create_default_context(cafile=str(cert_path))
        with socket.create_connection(listener.getsockname(), timeout=WAIT_SECONDS) as plain:
            with client_context.wrap_socket(plain, server_hostname="localhost") as tls:
                reader = makefile_rb(tls)
                assert type(reader.raw) is DirectSocketIO
                received = [reader.readline() for _ in lines]
                assert reader.readline() == b""
                reader.close()
        assert received == lines
    finally:
        thread.join(WAIT_SECONDS)
        listener.close()
    assert not failures


# 函数用途: G1 锁等待门禁用例工厂：每个线程一对 socketpair，每次调用写 64 字节再经 makefile_rb 读回；带 close 供驱动收尾。
def g1_case_direct_socket_io():
    writer, reader_socket = socket.socketpair()
    reader = makefile_rb(reader_socket)
    chunk = b"d" * G1_CHUNK_BYTES
    send, read1 = writer.sendall, reader.read1

    def operation() -> int:
        send(chunk)
        remaining = G1_CHUNK_BYTES
        while remaining:
            remaining -= len(read1(remaining))
        return G1_CHUNK_BYTES

    def close() -> None:
        reader.close()
        reader_socket.close()
        writer.close()

    operation.close = close
    return operation
