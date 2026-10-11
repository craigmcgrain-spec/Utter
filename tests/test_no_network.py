"""The air-gap guard: the runtime package must contain no HTTP client (spec §9).

All download code lives in ``install.sh``; ``src/utter`` must never reach the network. This
grep is the structural half of the guarantee -- the dynamic half (zero non-unix socket
syscalls under ``strace``) is Phase 3's.
"""

import re
from pathlib import Path


def test_no_http_client_in_the_runtime_package():
    pattern = re.compile(
        r"\b(requests|httpx|urllib\.request|http\.client|socket\.create_connection)\b"
    )
    offenders = [
        f"{path}:{number}"
        for path in (Path("src/utter").rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], f"runtime package must not touch the network: {offenders}"
