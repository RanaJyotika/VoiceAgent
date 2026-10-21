"""Run a full scripted conversation through the WebSocket using typed customer replies.
Usage: .venv\\Scripts\\python.exe backend\scripts\\convo_test.py [contact_id] [voice]
"""
import asyncio
import json
import sys

import websockets

CONTACT = int(sys.argv[1]) if len(sys.argv) > 1 else 1
VOICE = sys.argv[2] if len(sys.argv) > 2 else "Aoede"
REPLIES = [
    "Haan ji boliye, time hai.",
    "Haan order sahi hai.",
    "Address mein ek change hai, flat number 12 nahi 21 hai. Baaki sab same.",
    "Haan bas itna hi. Thank you.",
    "Theek hai, bye.",
    "Nhi hai.",
]
SILENCE = b"\x00" * 3200


async def main() -> None:
    replies = list(REPLIES)
    agent_buf = ""
    async with websockets.connect(f"ws://127.0.0.1:8000/ws/call/{CONTACT}?voice={VOICE}", max_size=None) as ws:
        async def silence():
            while True:
                await ws.send(SILENCE)
                await asyncio.sleep(0.1)

        bg = asyncio.create_task(silence())
        try:
            async with asyncio.timeout(150):
                async for msg in ws:
                    if isinstance(msg, bytes):
                        continue
                    evt = json.loads(msg)
                    t = evt.get("type")
                    if t == "transcript" and evt["role"] == "agent":
                        agent_buf += evt["text"]
                    elif t == "turn_complete":
                        if agent_buf.strip():
                            print("AGENT:   ", agent_buf.strip())
                        agent_buf = ""
                        if replies:
                            r = replies.pop(0)
                            print("CUSTOMER:", r)
                            await asyncio.sleep(0.5)
                            await ws.send(json.dumps({"type": "text", "text": r}))
                    elif t in ("outcome", "error", "ended"):
                        print("EVENT:   ", evt)
                        if t == "ended":
                            break
        finally:
            bg.cancel()


asyncio.run(main())
