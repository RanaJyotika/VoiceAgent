"""Check which voices work on this key and whether affective dialog is accepted."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai
from google.genai import types

from app.config import GEMINI_LIVE_MODEL, GOOGLE_API_KEY

VOICES = ["Aoede", "Kore", "Leda", "Zephyr", "Callirrhoe", "Autonoe", "Despina", "Sulafat",
          "Achernar", "Vindemiatrix", "Puck", "Charon", "Fenrir", "Orus", "Enceladus", "Iapetus"]


async def try_voice(client, voice: str, affective: bool = False) -> str:
    kwargs = dict(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
    )
    if affective:
        kwargs["enable_affective_dialog"] = True
    try:
        async with client.aio.live.connect(model=GEMINI_LIVE_MODEL, config=types.LiveConnectConfig(**kwargs)) as s:
            await s.send_client_content(turns=types.Content(role="user", parts=[types.Part(text="Namaste")]), turn_complete=True)
            n = 0
            async for msg in s.receive():
                sc = msg.server_content
                if sc and sc.model_turn:
                    for p in sc.model_turn.parts or []:
                        if p.inline_data and p.inline_data.data:
                            n += len(p.inline_data.data)
                if sc and sc.turn_complete:
                    break
            return f"OK ({n} bytes)"
    except Exception as e:  # noqa: BLE001
        return f"FAIL {type(e).__name__}: {str(e)[:120]}"


async def main() -> None:
    client = genai.Client(api_key=GOOGLE_API_KEY)
    results = await asyncio.gather(*(try_voice(client, v) for v in VOICES))
    for v, r in zip(VOICES, results):
        print(f"{v:14s} {r}")
    alpha = genai.Client(api_key=GOOGLE_API_KEY, http_options={"api_version": "v1alpha"})
    print("affective (v1alpha):", await try_voice(alpha, "Aoede", affective=True))
    print("affective (default):", await try_voice(client, "Aoede", affective=True))


asyncio.run(main())
