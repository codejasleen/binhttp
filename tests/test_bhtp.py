"""Conformance and interop tests for BHTP/1.

Run from the project root:   python -m unittest -v tests.test_bhtp

The encoder/decoder below is a THIRD implementation, written from SPEC.md
alone. bserve is tested by raw bytes on a socket; bcurl is tested against a
hand-rolled fake server. Neither program is tested only against the other.
"""
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BSERVE = os.path.join(ROOT, "bserve")
BCURL = os.path.join(ROOT, "bcurl")

HEADERS, DATA, END = 0x01, 0x02, 0x01
TABLE = [None, ":method", ":path", ":status", "host", "user-agent",
         "accept", "content-type", "content-length", "server", "date"]


# ------------------------------------------------- spec-only reference codec
def frame(ftype, flags, rid, payload=b"", reserved=0):
    return (len(payload).to_bytes(3, "big") + bytes([ftype, flags, reserved])
            + rid.to_bytes(2, "big") + payload)


def field(name, value):
    value = value.encode() if isinstance(value, str) else value
    if name in TABLE:
        head = bytes([TABLE.index(name)])
    else:
        head = bytes([0, len(name)]) + name.encode()
    return head + len(value).to_bytes(2, "big") + value


def block(*pairs):
    return b"".join(field(n, v) for n, v in pairs)


def get(rid, path, method="GET", end=True):
    return frame(HEADERS, END if end else 0, rid,
                 block((":method", method), (":path", path)))


def parse_block(p):
    out, i = [], 0
    while i < len(p):
        idx = p[i]; i += 1
        if idx == 0:
            n = p[i]; name = p[i + 1:i + 1 + n].decode(); i += 1 + n
        else:
            name = TABLE[idx]
        vl = int.from_bytes(p[i:i + 2], "big"); i += 2
        out.append((name, p[i:i + vl])); i += vl
    return out


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"EOF after {len(buf)} of {n} bytes")
        buf += chunk
    return buf


def read_frame(sock):
    h = recv_exact(sock, 8)
    length = int.from_bytes(h[:3], "big")
    return h[3], h[4], int.from_bytes(h[6:8], "big"), recv_exact(sock, length)


def read_response(sock):
    """-> (rid, status, headers dict, body, list of frame payload sizes)."""
    ftype, flags, rid, payload = read_frame(sock)
    assert ftype == HEADERS, f"expected HEADERS, got type {ftype}"
    headers = dict(parse_block(payload))
    body, sizes = b"", []
    while not flags & END:
        ftype, flags, frid, payload = read_frame(sock)
        assert ftype == DATA and frid == rid
        body += payload
        sizes.append(len(payload))
    return rid, int(headers[":status"]), headers, body, sizes


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------- server tests
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.www = os.path.join(cls.tmp, "www")
        os.makedirs(os.path.join(cls.www, "sub"))
        with open(os.path.join(cls.tmp, "secret.txt"), "w") as f:
            f.write("outside the root")
        cls.files = {
            "index.html": b"<h1>hi</h1>\n",
            "hello.txt": b"Hello, binary world!\n",
            "empty.txt": b"",
            "sub/index.html": b"sub index\n",
            "big.bin": random.Random(1).randbytes(100_000),
        }
        for name, data in cls.files.items():
            with open(os.path.join(cls.www, name), "wb") as f:
                f.write(data)
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, BSERVE, cls.www, str(cls.port)],
                                    stderr=subprocess.DEVNULL)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("bserve did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        return s

    def test_spec_example_bytes(self):
        """The exact request bytes printed in SPEC.md section 7."""
        wire = bytes.fromhex("00000a01010000010100034745540200012f")
        self.assertEqual(wire, get(1, "/"))
        s = self.connect()
        s.sendall(wire)
        rid, status, headers, body, _ = read_response(s)
        self.assertEqual((rid, status, body), (1, 200, self.files["index.html"]))
        self.assertEqual(headers["content-length"], b"12")

    def test_get_file_and_headers(self):
        s = self.connect()
        s.sendall(get(7, "/hello.txt"))
        rid, status, headers, body, _ = read_response(s)
        self.assertEqual((rid, status, body), (7, 200, self.files["hello.txt"]))
        self.assertTrue(headers["content-type"].startswith(b"text/plain"))
        self.assertIn("server", headers)
        self.assertIn("date", headers)

    def test_keep_alive_many_requests(self):
        s = self.connect()
        for rid, name in enumerate(["index.html", "hello.txt", "big.bin", "hello.txt"], 1):
            s.sendall(get(rid, "/" + name))
            r = read_response(s)
            self.assertEqual((r[0], r[1], r[3]), (rid, 200, self.files[name]))

    def test_pipelined_requests_answered_in_order(self):
        s = self.connect()
        s.sendall(get(1, "/hello.txt") + get(2, "/nope") + get(3, "/index.html"))
        self.assertEqual([read_response(s)[:2] for _ in range(3)],
                         [(1, 200), (2, 404), (3, 200)])

    def test_large_file_is_chunked(self):
        s = self.connect()
        s.sendall(get(1, "/big.bin"))
        _, status, headers, body, sizes = read_response(s)
        self.assertEqual(body, self.files["big.bin"])
        self.assertEqual(headers["content-length"], b"100000")
        self.assertTrue(all(n <= 16384 for n in sizes))
        self.assertGreater(len(sizes), 1)

    def test_directory_maps_to_index(self):
        s = self.connect()
        s.sendall(get(1, "/") + get(2, "/sub/") + get(3, "/sub"))
        for want in ("index.html", "sub/index.html", "sub/index.html"):
            self.assertEqual(read_response(s)[3], self.files[want])

    def test_query_string_and_percent_encoding(self):
        s = self.connect()
        s.sendall(get(1, "/hello%2Etxt?x=1"))
        self.assertEqual(read_response(s)[1], 200)

    def test_empty_file_has_no_data_frame(self):
        s = self.connect()
        s.sendall(get(1, "/empty.txt"))
        ftype, flags, _, payload = read_frame(s)
        self.assertEqual((ftype, flags & END), (HEADERS, END))
        self.assertEqual(dict(parse_block(payload))["content-length"], b"0")

    def test_404_keeps_connection_open(self):
        s = self.connect()
        s.sendall(get(1, "/does-not-exist"))
        self.assertEqual(read_response(s)[1], 404)
        s.sendall(get(2, "/hello.txt"))
        self.assertEqual(read_response(s)[:2], (2, 200))

    def test_path_traversal_is_404(self):
        s = self.connect()
        for rid, p in enumerate(["/../secret.txt", "/sub/../../secret.txt",
                                 "/%2e%2e/secret.txt", "/..\\secret.txt"], 1):
            s.sendall(get(rid, p))
            self.assertEqual(read_response(s)[1], 404, p)

    def test_head_has_no_body(self):
        s = self.connect()
        s.sendall(get(1, "/big.bin", method="HEAD"))
        ftype, flags, _, payload = read_frame(s)
        self.assertEqual((ftype, flags & END), (HEADERS, END))
        self.assertEqual(dict(parse_block(payload))["content-length"], b"100000")

    def test_unsupported_method_is_405(self):
        s = self.connect()
        s.sendall(get(1, "/", method="DELETE"))
        _, status, headers, _, _ = read_response(s)
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], b"GET, HEAD")

    def test_unknown_frame_types_are_skipped(self):
        s = self.connect()
        junk = frame(0x7F, 0xFF, 1, b"\x00\x01future-extension") + frame(0xEE, 0, 0)
        s.sendall(junk + get(1, "/hello.txt") + frame(0x03, END, 1, b"zz"))
        self.assertEqual(read_response(s)[:2], (1, 200))
        # unknown frame between HEADERS and the END-carrying DATA frame
        s.sendall(get(2, "/hello.txt", end=False) + frame(0x99, 0, 2, b"x" * 40)
                  + frame(DATA, END, 2))
        self.assertEqual(read_response(s)[:2], (2, 200))

    def test_reserved_byte_and_unknown_flags_ignored(self):
        s = self.connect()
        payload = block((":method", "GET"), (":path", "/hello.txt"))
        s.sendall(frame(HEADERS, END | 0xF0, 1, payload, reserved=0xAB))
        self.assertEqual(read_response(s)[1], 200)

    def test_literal_names_and_request_body(self):
        s = self.connect()
        hdrs = block((":method", "GET"), (":path", "/hello.txt"),
                     ("x-custom", "1"), ("host", "h"))
        literal_path = b"\x00\x05:path" + (10).to_bytes(2, "big") + b"/hello.txt"
        s.sendall(frame(HEADERS, 0, 1, hdrs) + frame(DATA, 0, 1, b"body")
                  + frame(DATA, END, 1, b"more"))
        self.assertEqual(read_response(s)[1], 200)
        s.sendall(frame(HEADERS, END, 2, field(":method", "GET") + literal_path))
        self.assertEqual(read_response(s)[1], 200)

    def test_byte_at_a_time_delivery(self):
        s = self.connect()
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for b in get(1, "/hello.txt"):
            s.send(bytes([b]))
            time.sleep(0.002)
        self.assertEqual(read_response(s)[1], 200)

    def test_malformed_blocks_get_400_and_connection_survives(self):
        bad = {
            "unknown index": b"\x63\x00\x00",
            "truncated value": block((":method", "GET")) + b"\x02\x00\x10/x",
            "truncated length": block((":method", "GET")) + b"\x02\x00",
            "zero name length": b"\x00\x00\x00\x00",
            "missing :path": block((":method", "GET")),
            "missing :method": block((":path", "/")),
            "duplicate :path": block((":method", "GET"), (":path", "/"), (":path", "/")),
            ":status in request": block((":method", "GET"), (":path", "/"),
                                        (":status", "200")),
            "relative path": block((":method", "GET"), (":path", "hello.txt")),
            "empty block": b"",
        }
        s = self.connect()
        for rid, (why, payload) in enumerate(bad.items(), 1):
            s.sendall(frame(HEADERS, END, rid, payload))
            self.assertEqual(read_response(s)[:2], (rid, 400), why)
        s.sendall(get(99, "/hello.txt"))
        self.assertEqual(read_response(s)[:2], (99, 200))

    def test_stray_data_frame_is_400(self):
        s = self.connect()
        s.sendall(frame(DATA, END, 5, b"hi"))
        self.assertEqual(read_response(s)[:2], (5, 400))
        s.sendall(get(6, "/"))
        self.assertEqual(read_response(s)[:2], (6, 200))

    def test_http1_text_is_connection_error(self):
        s = self.connect()
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        rid, status, *_ = read_response(s)
        self.assertEqual((rid, status), (0, 400))
        self.assertEqual(s.recv(1), b"")          # server closed

    def test_oversized_frame_is_connection_error(self):
        s = self.connect()
        s.sendall((16385).to_bytes(3, "big") + b"\x02\x00\x00\x00\x01")
        self.assertEqual(read_response(s)[:2], (0, 400))
        self.assertEqual(s.recv(1), b"")

    def test_headers_on_request_id_zero_is_connection_error(self):
        s = self.connect()
        s.sendall(get(0, "/"))
        self.assertEqual(read_response(s)[:2], (0, 400))

    def test_concurrent_connections(self):
        a, b = self.connect(), self.connect()
        a.sendall(get(1, "/hello.txt", end=False))     # a's request left open
        b.sendall(get(1, "/hello.txt"))
        self.assertEqual(read_response(b)[1], 200)     # b is not blocked by a
        a.sendall(frame(DATA, END, 1))
        self.assertEqual(read_response(a)[1], 200)

    # ----------------------------------------------- bcurl against real bserve
    def run_bcurl(self, *args):
        return subprocess.run([sys.executable, BCURL, *args], capture_output=True,
                              timeout=20)

    def test_bcurl_body_to_stdout(self):
        r = self.run_bcurl(f"localhost:{self.port}/big.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, self.files["big.bin"])

    def test_bcurl_exit_code_on_404(self):
        r = self.run_bcurl(f"127.0.0.1:{self.port}/missing")
        self.assertEqual(r.returncode, 4)
        self.assertIn(b"404", r.stdout)

    def test_bcurl_verbose_hexdumps_every_frame(self):
        r = self.run_bcurl("-v", f"bhttp://127.0.0.1:{self.port}/hello.txt")
        self.assertEqual(r.returncode, 0)
        err = r.stderr.decode()
        self.assertIn("> HEADERS", err)
        self.assertIn("< HEADERS", err)
        self.assertIn("< DATA", err)
        self.assertIn("00000000  ", err)
        self.assertEqual(r.stdout, self.files["hello.txt"])

    def test_bcurl_head(self):
        r = self.run_bcurl("-I", f"127.0.0.1:{self.port}/hello.txt")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b":status: 200", r.stdout)
        self.assertIn(b"content-length: 21", r.stdout)

    def test_bcurl_rejects_two_hosts(self):
        r = self.run_bcurl(f"127.0.0.1:{self.port}/", f"localhost:{self.port + 1}/")
        self.assertEqual(r.returncode, 2)


# --------------------------------------------- bcurl against a fake server
class FakeServer:
    """Accepts exactly ONE connection and replays a script of raw frames."""

    def __init__(self, handler):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self.requests = []
        self.handler = handler
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        self.listener.settimeout(10)
        conn, _ = self.listener.accept()
        self.connections += 1
        self.listener.settimeout(0.5)
        with conn:
            conn.settimeout(10)
            try:
                while True:
                    ftype, flags, rid, payload = read_frame(conn)
                    if ftype == HEADERS:
                        req = dict(parse_block(payload))
                        self.requests.append(req)
                        reply = self.handler(rid, req[":path"].decode())
                        if isinstance(reply, tuple):      # (bytes, "close")
                            conn.sendall(reply[0])
                            break
                        conn.sendall(reply)
            except (EOFError, OSError):
                pass
        try:                                  # a second connection would be a bug
            self.listener.accept()
            self.connections += 1
        except OSError:
            pass
        self.listener.close()


def response(rid, status, body=b"", chunks=1):
    head = frame(HEADERS, 0 if body else END, rid,
                 block((":status", str(status)), ("content-length", str(len(body)))))
    if not body:
        return head
    step = max(1, len(body) // chunks)
    parts = [body[i:i + step] for i in range(0, len(body), step)]
    return head + b"".join(frame(DATA, END if i == len(parts) - 1 else 0, rid, p)
                           for i, p in enumerate(parts))


class ClientTests(unittest.TestCase):
    def bcurl(self, *args):
        return subprocess.run([sys.executable, BCURL, *args], capture_output=True,
                              timeout=20)

    def test_skips_unknown_frames_from_server(self):
        def handler(rid, path):
            return (frame(0x42, 0, 0, b"v2 settings?") + frame(HEADERS, 0, rid,
                    block((":status", "200"))) + frame(0xFE, END, rid, b"ignore me")
                    + frame(DATA, 0, rid, b"abc") + frame(0x05, 0, rid)
                    + frame(DATA, END, rid, b"def"))
        srv = FakeServer(handler)
        r = self.bcurl(f"127.0.0.1:{srv.port}/x")
        self.assertEqual((r.returncode, r.stdout), (0, b"abcdef"), r.stderr)

    def test_many_urls_one_connection(self):
        srv = FakeServer(lambda rid, path: response(rid, 200, path.encode(), chunks=2))
        r = self.bcurl(f"127.0.0.1:{srv.port}/a", f"127.0.0.1:{srv.port}/bb",
                       f"127.0.0.1:{srv.port}/ccc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"/a/bb/ccc")
        srv.thread.join(5)
        self.assertEqual(srv.connections, 1)
        self.assertEqual([q[":path"] for q in srv.requests], [b"/a", b"/bb", b"/ccc"])
        self.assertEqual(srv.requests[0]["host"], f"127.0.0.1:{srv.port}".encode())

    def test_exit_codes(self):
        for status, code in ((200, 0), (301, 0), (404, 4), (400, 4), (503, 5)):
            srv = FakeServer(lambda rid, path, s=status: response(rid, s, b"x"))
            r = self.bcurl(f"127.0.0.1:{srv.port}/")
            self.assertEqual(r.returncode, code, status)

    def test_worst_status_wins(self):
        srv = FakeServer(lambda rid, path: response(rid, 500 if path == "/b" else 200))
        r = self.bcurl(f"127.0.0.1:{srv.port}/a", f"127.0.0.1:{srv.port}/b")
        self.assertEqual(r.returncode, 5)

    def test_server_closing_mid_response(self):
        srv = FakeServer(lambda rid, path: (frame(HEADERS, 0, rid,
                                                  block((":status", "200")))
                         + frame(DATA, 0, rid, b"partial")[:10], "close"))
        r = self.bcurl(f"127.0.0.1:{srv.port}/")
        self.assertEqual(r.returncode, 1)          # connection lost

    def test_wrong_request_id_is_protocol_error(self):
        srv = FakeServer(lambda rid, path: response(rid + 1, 200, b"x"))
        r = self.bcurl(f"127.0.0.1:{srv.port}/")
        self.assertEqual(r.returncode, 3)

    def test_request_bytes_match_spec(self):
        srv = FakeServer(lambda rid, path: response(rid, 200))
        r = self.bcurl("-H", "X-Trace: 42", f"127.0.0.1:{srv.port}/p")
        self.assertEqual(r.returncode, 0, r.stderr)
        req = srv.requests[0]
        self.assertEqual(req[":method"], b"GET")
        self.assertEqual(req[":path"], b"/p")
        self.assertEqual(req["x-trace"], b"42")       # literal name, lower-cased


if __name__ == "__main__":
    unittest.main(verbosity=2)
