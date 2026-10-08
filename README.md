# BHTP/1: HTTP, in binary

| Hand-in item | File |
|---|---|
| 1. The spec (two pages) | `SPEC.md`, printed as `SPEC.pdf` |
| 2. The programs | `bserve` (Track 1, server) and `bcurl` (Track 2, client) |
| 3. Annotated hexdump of one complete request and response | `HEXDUMP.md`, printed as `HEXDUMP.pdf` |

Both programs are single-file Python 3 (3.8+), standard library only. They share **no
code**: each was written against `SPEC.md` alone, so either one can be paired with a
partner's implementation.

## Run

```sh
chmod +x bserve bcurl            # once, on Linux/macOS
./bserve ./www 9000              # add -v to hexdump every frame on the server too
./bcurl -v localhost:9000/index.html
./bcurl localhost:9000/ localhost:9000/hello.txt localhost:9000/docs/   # one connection
./bcurl -I localhost:9000/hello.txt                                     # HEAD, headers to stdout
./bcurl localhost:9000/missing; echo $?                                 # 4
```

On Windows, use `bserve.cmd` / `bcurl.cmd`, or `python bserve ./www 9000`.

**bcurl** writes the body to stdout byte-exact and puts `-v` hexdumps on stderr, so
`bcurl -v url > file` still yields a clean file. Its exit status is 0 for 1xx–3xx, 4 for a
4xx, 5 for a 5xx, 1 when it cannot connect or the connection is lost, 2 for bad usage and
3 for a protocol violation by the server. It never opens a second connection, and it
refuses URLs for different host:port pairs.

**bserve** serves files under ROOT. A directory maps to its `index.html`. A path that
escapes the root through `..`, `%2e%2e`, backslashes or symlinks gets a 404. A malformed
frame gets a 400. Each connection stays open across requests and has its own thread.

## Test

```sh
python -m unittest -v tests.test_bhtp
```

The tests contain 34 cases. They drive `bserve` with hand-built raw bytes and drive
`bcurl` against a fake server, using a third encoder written from the spec. Among other
things, they check:

- the exact bytes of the example in SPEC §7;
- unknown frame types skipped in both directions, including between HEADERS and DATA;
- a 400 for every malformed-block case, and the same connection still working afterwards;
- keep-alive, pipelining, and 100 KB files chunked at 16384 octets or less;
- path traversal, HEAD, 405, a request delivered one byte at a time, and concurrent clients;
- plain HTTP/1 text answered with a connection-level 400 and a close;
- bcurl's exit codes and its single connection for several URLs.
