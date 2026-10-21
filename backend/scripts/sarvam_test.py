"""One Sarvam chat call (sarvam-105b-conversations) to check the key works. PAID: ask the owner before running."""
import os
from pathlib import Path

from dotenv import load_dotenv
from sarvamai import SarvamAI

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
key = os.getenv("SARVAM_API") or os.getenv("SARVAM_API_KEY")
if not key:
    raise SystemExit("SARVAM_API is not set in .env")

client = SarvamAI(api_subscription_key=key)
resp = client.chat.completions(
    model="sarvam-105b-conversations",
    messages=[{"role": "user", "content": "Namaste! Ek line mein apna parichay dijiye."}],
    max_tokens=100,
)
print(resp.choices[0].message.content)
