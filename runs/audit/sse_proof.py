"""Prove the SSE delivery path works end to end.

Opens a stream to the live bridge, appends a change record to changes.jsonl as the
pipeline would, and reports whether the record arrives over SSE.

Usage: python runs/audit/sse_proof.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from live import CHANGES_PATH  # noqa: E402

LOG = ROOT / "runs" / "audit" / "sse_proof.log"
ORG = "923609016"
received: list[str] = []


def reader() -> None:
    """Hold the SSE connection open and collect whatever arrives."""
    url = f"http://127.0.0.1:8799/api/stream/{ORG}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=20) as h:
            for raw in h:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    received.append(line)
                if any(l.startswith("data:") for l in received):
                    break
    except Exception as e:  # noqa: BLE001
        received.append(f"__ERR__ {type(e).__name__}: {e}")


def main() -> int:
    lines: list[str] = []

    def rec(s: str = "") -> None:
        lines.append(s)

    rec("=== SSE delivery proof ===")
    rec(f"stream URL : http://127.0.0.1:8799/api/stream/{ORG}")
    rec(f"tailing    : {CHANGES_PATH}")
    before = CHANGES_PATH.read_text(encoding="utf-8") if CHANGES_PATH.exists() else ""
    rec(f"lines before: {len(before.splitlines())}")

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(2.0)  # let the subscription attach before we write

    # Append exactly what the pipeline appends.
    record = {
        "orgnr": ORG,
        "field": "legal_name",
        "old": "",
        "new": "EQUINOR ASA",
        "detected_at": "2026-10-10T11:40:00Z",
        "source": "sse-proof",
    }
    with CHANGES_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    rec("appended    : " + json.dumps(record, ensure_ascii=False))

    t.join(timeout=14)

    rec("")
    rec("received over SSE:")
    for line in received:
        rec("   " + line[:220])

    ok = any("sse-proof" in l for l in received)
    rec("")
    rec(f"RESULT: {'PASS - change delivered over SSE' if ok else 'FAIL - nothing streamed'}")

    # Restore the file so the proof leaves no fake data behind.
    CHANGES_PATH.write_text(before, encoding="utf-8")
    rec(f"restored changes.jsonl to {len(before.splitlines())} lines (no fake data left)")

    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-6:]))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
