"""List Gemini models on this API key that look like Live / native-audio models."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai

from app.config import GOOGLE_API_KEY

client = genai.Client(api_key=GOOGLE_API_KEY)
for m in client.models.list():
    name = m.name.replace("models/", "")
    actions = list(m.supported_actions or [])
    if any(k in name for k in ("live", "audio")) or "bidiGenerateContent" in actions:
        print(f"{name:60s} {', '.join(actions)}")
