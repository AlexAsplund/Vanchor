"""Tests for the NMEA-over-TCP gateway."""

from __future__ import annotations

import asyncio

from vanchor.core import events
from vanchor.core.events import EventBus
from vanchor.nav.nmea_net import NMEA_OUT, NmeaTcpServer


async def _drain(reader: asyncio.StreamReader, timeout: float = 1.0) -> bytes:
    return await asyncio.wait_for(reader.readline(), timeout)


async def test_inbound_line_reaches_bus():
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    server = NmeaTcpServer(bus, host="127.0.0.1", port=0)
    await server.start()
    try:
        assert server.bound_port is not None and server.bound_port > 0
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        # Let the server register the client.
        for _ in range(50):
            if server.client_count == 1:
                break
            await asyncio.sleep(0.01)

        sentence = "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A"
        writer.write((sentence + "\r\n").encode())
        await writer.drain()

        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received == [sentence]

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_non_nmea_line_ignored():
    bus = EventBus()
    received: list[str] = []
    bus.subscribe(events.NMEA_IN, received.append)

    server = NmeaTcpServer(bus, host="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"hello world\r\n")
        writer.write(b"!AIVDM,1,1,,A,foo,0*5C\r\n")  # '!' is NMEA-ish, should pass
        await writer.drain()

        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.01)
        assert received == ["!AIVDM,1,1,,A,foo,0*5C"]

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_broadcast_reaches_client():
    bus = EventBus()
    server = NmeaTcpServer(bus, host="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        for _ in range(50):
            if server.client_count == 1:
                break
            await asyncio.sleep(0.01)

        await server.broadcast("$GPHDM,123.4,M*hh")
        line = await _drain(reader)
        assert line == b"$GPHDM,123.4,M*hh\r\n"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_nmea_out_topic_autobroadcasts():
    bus = EventBus()
    server = NmeaTcpServer(bus, host="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        for _ in range(50):
            if server.client_count == 1:
                break
            await asyncio.sleep(0.01)

        await bus.publish(NMEA_OUT, "$GPHDM,200.0,M*00")
        line = await _drain(reader)
        assert line == b"$GPHDM,200.0,M*00\r\n"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_multiple_clients_all_receive():
    bus = EventBus()
    server = NmeaTcpServer(bus, host="127.0.0.1", port=0)
    await server.start()
    try:
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.bound_port)
        r2, w2 = await asyncio.open_connection("127.0.0.1", server.bound_port)
        for _ in range(50):
            if server.client_count == 2:
                break
            await asyncio.sleep(0.01)
        assert server.client_count == 2

        await server.broadcast("$GPABC,1*00")
        assert await _drain(r1) == b"$GPABC,1*00\r\n"
        assert await _drain(r2) == b"$GPABC,1*00\r\n"

        for w in (w1, w2):
            w.close()
            await w.wait_closed()
    finally:
        await server.stop()


async def test_client_disconnect_is_graceful():
    bus = EventBus()
    server = NmeaTcpServer(bus, host="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        for _ in range(50):
            if server.client_count == 1:
                break
            await asyncio.sleep(0.01)

        writer.close()
        await writer.wait_closed()

        for _ in range(100):
            if server.client_count == 0:
                break
            await asyncio.sleep(0.01)
        assert server.client_count == 0

        # Broadcasting with no clients is a no-op (must not raise).
        await server.broadcast("$GPABC,1*00")
    finally:
        await server.stop()


async def test_stop_is_idempotent_and_clean():
    bus = EventBus()
    server = NmeaTcpServer(bus, host="127.0.0.1", port=0)
    await server.start()
    port = server.bound_port
    await server.stop()
    assert server.bound_port is None
    # Second stop is a no-op.
    await server.stop()

    # Port is released; connecting should now fail.
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=0.5
        )
        w.close()
        await w.wait_closed()
        raised = False
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
        raised = True
    assert raised
