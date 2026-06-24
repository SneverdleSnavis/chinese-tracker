"""Free text-to-speech via edge-tts (Microsoft's online neural voices).
Used to attach pronunciation audio to Anki cards during sync."""
import asyncio
import hashlib

# A natural Mandarin neural voice. Others: zh-CN-YunxiNeural (male),
# zh-CN-XiaoyiNeural, zh-CN-YunyangNeural.
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def media_filename(word: str) -> str:
    """Deterministic media filename so re-syncing a word overwrites its audio
    instead of piling up duplicates in the Anki collection."""
    h = hashlib.md5(word.encode("utf-8")).hexdigest()[:12]
    return f"chinese-tracker-{h}.mp3"


async def _synthesize(text: str, voice: str) -> bytes:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    buf = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.extend(chunk["data"])
    return bytes(buf)


def synth_mp3(text: str, voice: str = DEFAULT_VOICE) -> bytes:
    """Synthesize `text` to MP3 bytes. Runs the async edge-tts client to
    completion; safe to call from FastAPI's threadpool (sync endpoints)."""
    return asyncio.run(_synthesize(text, voice))
