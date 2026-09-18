"""Connect to Gemini Live, send one text turn, confirm we get audio + transcription back.
Usage: .venv\\Scripts\\python.exe scripts\\smoke_test.py [model]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai
from google.genai import types

from app.config import GEMINI_LIVE_MODEL, GEMINI_VOICE, GOOGLE_API_KEY

MODEL = sys.argv[1] if len(sys.argv) > 1 else GEMINI_LIVE_MODEL


async def main() -> None:
    client = genai.Client(api_key=GOOGLE_API_KEY)
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=(
            "You are Priya, a polite calling assistant for Vedic Vaibhav. Reply in Hinglish, two short sentences."
        ))]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=GEMINI_VOICE))
        ),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
    )
    print(f"Connecting to {MODEL} ...")
    audio_bytes = 0
    text_out = []
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text="Namaste, kaun bol raha hai?")]),
            turn_complete=True,
        )
        async for msg in session.receive():
            sc = msg.server_content
            if sc is not None:
                if sc.model_turn is not None:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data is not None and part.inline_data.data:
                            audio_bytes += len(part.inline_data.data)
                if sc.output_transcription is not None and sc.output_transcription.text:
                    text_out.append(sc.output_transcription.text)
                if sc.turn_complete:
                    break
            if msg.usage_metadata is not None:
                print("usage:", msg.usage_metadata)
    print(f"audio bytes received: {audio_bytes}  (~{audio_bytes / 2 / 24000:.1f} s at 24 kHz)")
    print("transcript:", "".join(text_out))


asyncio.run(main())
