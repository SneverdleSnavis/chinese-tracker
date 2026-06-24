import re
import html

# Matches both SRT (00:00:01,000) and VTT (00:00:01.000) timestamps.
TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")
CUE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})"
)
TAG_RE = re.compile(r"<[^>]+>")


def _ts_to_ms(ts: str) -> int:
    m = TIME_RE.match(ts)
    if not m:
        return 0
    h, mm, ss, ms = m.groups()
    ms = ms.ljust(3, "0")  # VTT may use 2-digit fractions
    return int(h) * 3600000 + int(mm) * 60000 + int(ss) * 1000 + int(ms)


def _clean(text: str) -> str:
    text = TAG_RE.sub("", text)          # strip <c>, <00:00:00.000> inline tags
    text = html.unescape(text)
    return text.strip()


def parse_subtitles(raw: str):
    """Parse SRT or VTT into a list of {start_ms, end_ms, text} cues.
    Tolerant of both formats; ignores cue numbers, WEBVTT header, and styling."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues = []
    current = None
    text_buffer = []

    def flush():
        nonlocal current, text_buffer
        if current is not None:
            text = _clean(" ".join(text_buffer)).strip()
            if text:
                current["text"] = text
                cues.append(current)
        current = None
        text_buffer = []

    for line in lines:
        cue_match = CUE_RE.search(line)
        if cue_match:
            flush()
            start, end = cue_match.groups()
            current = {"start_ms": _ts_to_ms(start), "end_ms": _ts_to_ms(end), "text": ""}
        elif current is not None:
            if line.strip():
                text_buffer.append(line.strip())
            else:
                flush()
    flush()

    # Deduplicate consecutive identical lines (common in auto-generated VTT).
    deduped = []
    for c in cues:
        if deduped and deduped[-1]["text"] == c["text"]:
            deduped[-1]["end_ms"] = c["end_ms"]
        else:
            deduped.append(c)
    return deduped


YOUTUBE_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})")


def normalize_youtube_url(url: str):
    m = YOUTUBE_RE.search(url)
    if not m:
        return None, None
    vid = m.group(1)
    return vid, f"https://www.youtube.com/watch?v={vid}"


def fetch_youtube_subtitles(url: str):
    """Extract Chinese captions from a YouTube video via yt-dlp without
    downloading the video. Returns (title, video_url, cues). Prefers manual
    Chinese subs, falls back to auto-generated. Raises ValueError if none."""
    import yt_dlp
    import urllib.request

    vid, clean_url = normalize_youtube_url(url)
    if not vid:
        raise ValueError("Could not parse a YouTube video ID from that URL.")

    opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(clean_url, download=False)

    title = info.get("title", clean_url)
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def pick(track_dict):
        # Prefer simplified-script tracks, and within a track prefer formats our
        # parser understands (vtt/srt) over YouTube's XML formats (srv3/ttml/json3).
        for lang in ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW"):
            for key in track_dict:
                if key == lang or key.startswith(lang):
                    formats = {f.get("ext"): f.get("url") for f in track_dict[key]}
                    for ext in ("vtt", "srt"):
                        if formats.get(ext):
                            return formats[ext]
        return None

    sub_url = pick(subs) or pick(auto)
    if not sub_url:
        raise ValueError("No Chinese captions found for this video.")

    req = urllib.request.Request(sub_url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")
    cues = parse_subtitles(raw)
    if not cues:
        raise ValueError("Found a caption track but could not parse any lines from it.")
    return title, clean_url, cues


BILIBILI_RE = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]+|av\d+)", re.IGNORECASE)


def is_bilibili(url: str) -> bool:
    return bool(BILIBILI_RE.search(url or "")) or "b23.tv" in (url or "")


def _parse_json_subs(raw: str):
    """Parse a JSON caption payload — Bilibili 'bcc' (body[{from,to,content}]) or
    YouTube json3 (events[{tStartMs,dDurationMs,segs[{utf8}]}])."""
    import json
    data = json.loads(raw)
    cues = []
    if isinstance(data, dict) and data.get("body"):
        for it in data["body"]:
            text = (it.get("content") or "").strip()
            if text:
                cues.append({
                    "start_ms": int(float(it.get("from", 0)) * 1000),
                    "end_ms": int(float(it.get("to", 0)) * 1000),
                    "text": text,
                })
    elif isinstance(data, dict) and data.get("events"):
        for ev in data["events"]:
            text = "".join(s.get("utf8", "") for s in (ev.get("segs") or [])).strip()
            if text:
                start = int(ev.get("tStartMs", 0))
                cues.append({"start_ms": start, "end_ms": start + int(ev.get("dDurationMs", 0)), "text": text})
    return cues


def fetch_bilibili_subtitles(url: str):
    """Extract Chinese captions from a Bilibili video via yt-dlp. Returns
    (title, video_url, cues). Many Bilibili videos have no CC (or need a login),
    in which case this raises ValueError with a clear message."""
    import yt_dlp
    import urllib.request

    opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title", url)
    webpage = info.get("webpage_url") or url
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    def pick(track_dict):
        for lang in ("zh-Hans", "zh-CN", "zh", "ai-zh", "zh-Hant", "zh-TW"):
            for key in track_dict:
                if key == lang or key.startswith(lang):
                    formats = {f.get("ext"): f.get("url") for f in track_dict[key]}
                    for ext in ("vtt", "srt", "json3", "json"):
                        if formats.get(ext):
                            return formats[ext], ext
        return None, None

    sub_url, ext = pick(subs)
    if not sub_url:
        sub_url, ext = pick(auto)
    if not sub_url:
        raise ValueError("No Chinese captions found for this Bilibili video (many have none, or require a login).")

    req = urllib.request.Request(
        sub_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    )
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    cues = parse_subtitles(raw) if ext in ("vtt", "srt") else _parse_json_subs(raw)
    if not cues:
        raise ValueError("Found a caption track but could not parse any lines from it.")
    return title, webpage, cues


def fetch_video_subtitles(url: str):
    """Route a video URL to the right caption fetcher (YouTube or Bilibili)."""
    if normalize_youtube_url(url)[0]:
        return fetch_youtube_subtitles(url)
    if is_bilibili(url):
        return fetch_bilibili_subtitles(url)
    raise ValueError("Unsupported video URL — paste a YouTube or Bilibili link.")
