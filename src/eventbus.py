"""Phase 2 — optional event-stream backbone (FastStream).

This is the natural path if ingestion volume or durability needs outgrow the
single-process daemon: changes flow over a real broker (Redis Streams, Kafka,
RabbitMQ, NATS) and multiple workers consume them. It is a *scaffold* — active
only when ``FASTSTREAM_BROKER`` is configured. The keyless demo never sets it, so
importing this module is a no-op and nothing connects to a broker.

    FASTSTREAM_BROKER=redis://localhost:6379/0  python -m src.eventbus

The handler below mirrors what ``live.process_orgnrs`` already does, so swapping
the in-process scheduler for a brokered worker is a configuration change, not a
rewrite.
"""

from __future__ import annotations

import os
from typing import Any

BROKER_URL = os.environ.get("FASTSTREAM_BROKER")
app: Any = None

if BROKER_URL:
    from faststream import FastStream
    from faststream.redis import RedisBroker

    broker = RedisBroker(BROKER_URL)
    app = FastStream(broker)

    @broker.subscriber("signalpost:changes")
    async def on_change(message: dict[str, Any]) -> None:
        """Consume a change record and re-validate its company.

        In a full deployment this worker would call ``process_orgnrs`` and then
        publish the refreshed envelope to a downstream topic for the dashboard.
        """
        orgnr = message.get("orgnr")
        if not orgnr:
            return
        # Lazy import keeps the core agent free of FastStream at load time.
        from live import process_orgnrs

        process_orgnrs([orgnr], limit=1)


def is_active() -> bool:
    return app is not None
