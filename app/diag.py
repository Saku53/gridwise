import os
from google import genai
from google.genai import types

key = os.environ.get("GEMINI_API_KEY", "")
print("key prefix:", key[:8], "len:", len(key))

client = genai.Client(api_key=key)
try:
    resp = client.models.generate_content(
      model="gemini-3.6-flash",
        contents="Say hello in one word.",
        config=types.GenerateContentConfig(temperature=0),
    )
    print("SUCCESS:", resp.text)
except Exception as e:
    print("FAILED:", type(e).__name__, "-", str(e)[:500])