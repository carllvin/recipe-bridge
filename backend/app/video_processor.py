"""Recipes from video links - YouTube, Instagram, TikTok. The recipe is in
what the creator wrote (description / caption) and often in what they say:
for YouTube the subtitles (the creator's or the automatic ones) are read
too. The text then runs through the normal extraction like a web page.

Best effort: these sites change and sometimes answer bots with a consent
or login page - then only what's reachable (often the caption) is used."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("recipe-bridge")

MAX_TRANSCRIPT_CHARS = 25000
# EU consent pages: pretend the cookie banner was answered
YOUTUBE_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}


def platform(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host in ("youtube.com", "youtu.be", "music.youtube.com") or host.endswith(".youtube.com"):
        return "youtube"
    if host in ("instagram.com", "instagr.am"):
        return "instagram"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    return None


def youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0] or None
    if "v" in parse_qs(parsed.query):
        return parse_qs(parsed.query)["v"][0]
    m = re.match(r"^/(?:shorts|live|embed)/([\w-]{6,})", parsed.path)
    return m.group(1) if m else None


def _json_after(marker: str, html: str):
    """The JSON object assigned right after `marker` in a page's script."""
    start = html.find(marker)
    if start < 0:
        return None
    start = html.find("{", start)
    try:
        return json.JSONDecoder().raw_decode(html[start:])[0]
    except (ValueError, IndexError):
        return None


def _meta(html: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(prop)}"[^>]+content="([^"]*)"', html) or \
        re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(prop)}"', html)
    if not m:
        return ""
    import html as html_lib
    return html_lib.unescape(m.group(1)).strip()


def _transcript(client, tracks, language: str | None) -> str:
    """Subtitle text: the creator's own before automatic ones, the output
    language first."""
    if not tracks:
        return ""
    def rank(t):
        return (t.get("kind") == "asr", not (language and (t.get("languageCode") or "").startswith(language)))
    for track in sorted(tracks, key=rank):
        url = track.get("baseUrl")
        if not url:
            continue
        try:
            resp = client.get(url + ("&" if "?" in url else "?") + "fmt=json3")
            resp.raise_for_status()
            events = resp.json().get("events") or []
        except Exception as exc:  # noqa: BLE001
            log.info("YouTube subtitles not readable: %s", exc)
            continue
        words = "".join(seg.get("utf8", "") for e in events for seg in e.get("segs") or [])
        text = re.sub(r"\s+", " ", words).strip()
        if text:
            return text[:MAX_TRANSCRIPT_CHARS]
    return ""


def youtube(client, url: str, language: str | None) -> dict:
    video_id = youtube_id(url)
    page = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    html = client.get(page, cookies=YOUTUBE_COOKIES).text
    player = _json_after("ytInitialPlayerResponse", html) or {}
    details = player.get("videoDetails") or {}
    tracks = ((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or []
    return {
        "title": details.get("title") or _meta(html, "og:title"),
        "author": details.get("author") or "",
        "description": details.get("shortDescription") or _meta(html, "og:description"),
        "transcript": _transcript(client, tracks, language),
        "image": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else _meta(html, "og:image"),
    }


def tiktok(client, url: str, language: str | None) -> dict:
    info = {}
    try:  # oEmbed: title = the caption, no login needed
        resp = client.get("https://www.tiktok.com/oembed", params={"url": url})
        if resp.status_code == 200:
            info = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.info("TikTok oEmbed failed: %s", exc)
    caption = info.get("title") or ""
    image = info.get("thumbnail_url") or ""
    if not caption:
        html = client.get(url).text
        data = _json_after('id="__UNIVERSAL_DATA_FOR_REHYDRATION__"', html) or {}
        item = (((data.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}).get("itemInfo") or {}).get("itemStruct") or {}
        caption = item.get("desc") or _meta(html, "og:description")
        image = image or _meta(html, "og:image")
    return {"title": "", "author": info.get("author_name") or "", "description": caption, "transcript": "", "image": image}


def instagram(client, url: str, language: str | None) -> dict:
    html = client.get(url).text
    caption = _meta(html, "og:description") or _meta(html, "description")
    # "123 likes, 4 comments - user on May 1, 2026: "caption"" -> the caption
    m = re.search(r':\s*["“](.*)["”]\s*\.?\s*$', caption, re.S)
    if m:
        caption = m.group(1)
    if len(caption) < 80:  # the embed page usually carries the whole caption
        m = re.match(r"^/(?:[\w.]+/)?(p|reel|reels|tv)/([\w-]+)", urlparse(url).path)
        if m:
            embed = client.get(f"https://www.instagram.com/p/{m.group(2)}/embed/captioned/").text
            found = re.search(r'class="Caption"[^>]*>(.*?)</div>', embed, re.S)
            if found:
                from bs4 import BeautifulSoup
                text = BeautifulSoup(found.group(1), "html.parser").get_text("\n", strip=True)
                caption = text if len(text) > len(caption) else caption
    return {"title": _meta(html, "og:title"), "author": "", "description": caption, "transcript": "",
            "image": _meta(html, "og:image")}


READERS = {"youtube": youtube, "tiktok": tiktok, "instagram": instagram}


def read(client, url: str, language: str | None = None) -> dict:
    """{"title", "author", "description", "transcript", "image", "text"} -
    "text" is what goes to the extraction."""
    kind = platform(url)
    info = READERS[kind](client, url, language)
    parts = [f"Video recipe ({kind}): {info['title']}".rstrip(": "), f"Source: {url}"]
    if info.get("author"):
        parts.append(f"Creator: {info['author']}")
    if info.get("description"):
        parts.append("\nWhat the creator wrote:\n" + info["description"].strip())
    if info.get("transcript"):
        parts.append("\nWhat is said in the video (subtitles - amounts and steps may only be mentioned here):\n"
                     + info["transcript"])
    info["text"] = "\n".join(parts)
    info["kind"] = kind
    return info
