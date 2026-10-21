"""Exercise the browser WebSocket path without a microphone: stream silence, collect events.
Usage: .venv\\Scripts\\python.exe backend\scripts\\ws_test.py [contact_id] [seconds]
"""
import asyncio
import json
import sys

import websockets

CONTACT = int(sys.argv[1]) if len(sys.argv) > 1 else 1
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 12
CHUNK = b"\x00" * (16000 * 2 // 10)  # 100 ms of 16 kHz 16-bit silence


async def main() -> None:
    audio = 0
    voice = sys.argv[3] if len(sys.argv) > 3 else "Aoede"
    async with websockets.connect(f"ws://127.0.0.1:8000/ws/call/{CONTACT}?voice={voice}", max_size=None) as ws:
        async def sender():
            end = asyncio.get_event_loop().time() + SECONDS
            while asyncio.get_event_loop().time() < end:
                await ws.send(CHUNK)
                await asyncio.sleep(0.1)
            await ws.send(json.dumps({"type": "hangup"}))

        send_task = asyncio.create_task(sender())
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    audio += len(msg)
                    continue
                evt = json.loads(msg)
                if evt.get("type") == "transcript":
                    print(f"  {evt['role']}: {evt['text']}", end="\n" if evt['role'] == 'x' else "\n")
                else:
                    print("event:", evt)
                if evt.get("type") == "ended":
                    break
        except websockets.ConnectionClosed:
            pass
        send_task.cancel()
    print(f"audio bytes: {audio} (~{audio / 2 / 24000:.1f}s)")


asyncio.run(main())
