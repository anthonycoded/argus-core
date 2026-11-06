# Article fetch security and compatibility

Supported and tested together: **HTTPX 0.28.1, httpcore 1.0.9, AnyIO 4.15.1**.
`requirements.txt` constrains these to exact versions. Python 3.11+ is required
for the total download deadline (`asyncio.timeout`). No other version combination
is claimed compatible. Upgrade the trio deliberately and run the focused tests:

```powershell
$env:DATABASE_URL = 'sqlite://'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -B -m pytest -p pytest_asyncio.plugin -p no:cacheprovider -o asyncio_default_fixture_loop_scope=function backend/tests/test_article_extraction.py backend/tests/test_article_fetch_security.py backend/tests/api/test_articles.py -q
```

The transport replaces HTTPX's private `_pool` and subclasses httpcore's private
`_backends.anyio.AnyIOBackend`. HTTPX still maps httpcore errors and wraps response
streams. Inspect those interfaces on upgrades; the exact pins are intentional.

## Connection identity, DNS and pooling

Only the network backend substitutes an address: DNS is resolved once per new TCP
connection, every answer must be public, and AnyIO receives a numeric IP. Its
numeric-address branch does not resolve the hostname again. httpcore retains the
original scheme/host/port origin, sends that hostname to `start_tls`, and AnyIO
passes it to `TLSStream.wrap` for SNI and certificate verification. The SSL context
requires valid certificates and hostname checking. Caller SNI overrides are
discarded. Production clients disable environment proxies.

HTTP/1.1 pooling is explicitly enabled with HTTPX's usual limits (100 connections,
20 idle connections, five-second idle expiry); HTTP/2 is disabled. Reusing an open
same-origin socket cannot change its validated peer, so DNS is not rechecked for
that reuse. Reconnects resolve and validate again. Different origins cannot share
a connection even if DNS yields the same address. Redirects are checked at every
hop, with at most five redirects. Use `extract_articles` as the service entrypoint;
callers injecting a client into lower-level helpers must use `PublicHTTPTransport`.

## Timeouts and decompression

The connect timeout covers DNS and all numeric TCP attempts together. httpcore
still supplies connect timeouts to TLS, read timeouts to reads, write timeouts to
writes, and pool timeouts to queued requests, with HTTPX exception mapping intact.
The 20-second total download deadline additionally spans all redirects and reads;
it maps to the existing `timeout` status. It does not include HTML extraction.

Downloads and decompressed output are independently capped at 2 MiB. Raw streaming
avoids HTTPX's automatic unbounded decompression. Gzip and zlib-wrapped deflate are
decoded once with `max_length=limit+1`; no unbounded flush is called. Invalid
checksums, truncated streams, trailing bytes, concatenated gzip members, raw
deflate and unsupported encodings (including Brotli) fail closed. Clients request
identity encoding. Decompression input/output are bounded, but there is no hard
CPU deadline or process-level memory isolation for zlib or the HTML parser.

## Remaining boundaries

Address validation cannot detect infrastructure that routes a public IP to an
internal service, a public proxy that itself fetches internal resources, or
deployment-specific NAT64 prefixes outside the explicitly blocked ranges. Apply
network egress policy for those environments. Public HTTP/HTTPS services on
nonstandard ports are allowed. HTTPS-to-HTTP redirects remain allowed for article
compatibility. Concurrency is limited per batch, not globally across requests.
The tests use fake sockets/DNS and TLS boundary assertions; they do not establish
live Internet connectivity or perform a real peer certificate handshake.
