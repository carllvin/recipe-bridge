"""Recipes from YouTube, TikTok and Instagram links (against fake pages)."""
import io
import json

import httpx
import pytest
from PIL import Image

from app import url_processor, video_processor


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "JPEG")
    return buf.getvalue()


def serve(monkeypatch, routes):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response(request) if callable(response) else response
        return httpx.Response(404, text="nope")
    monkeypatch.setattr(url_processor, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen


def test_platform_and_youtube_ids():
    assert video_processor.platform("https://youtu.be/abc123XYZ") == "youtube"
    assert video_processor.platform("https://www.youtube.com/shorts/abc123XYZ") == "youtube"
    assert video_processor.platform("https://www.tiktok.com/@koch/video/1") == "tiktok"
    assert video_processor.platform("https://www.instagram.com/reel/Cx1/") == "instagram"
    assert video_processor.platform("https://www.chefkoch.de/rezepte/1") is None
    assert video_processor.youtube_id("https://youtu.be/abc123XYZ?t=4") == "abc123XYZ"
    assert video_processor.youtube_id("https://www.youtube.com/watch?v=abc123XYZ&list=x") == "abc123XYZ"
    assert video_processor.youtube_id("https://www.youtube.com/shorts/abc123XYZ") == "abc123XYZ"


def test_youtube_description_and_subtitles(monkeypatch, tmp_path):
    player = {"videoDetails": {"title": "Die beste Lasagne", "author": "Kochkanal",
                               "shortDescription": "Zutaten: 500 g Hack, 1 Zwiebel, Lasagneplatten"},
              "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
                  {"baseUrl": "https://www.youtube.com/api/timedtext?v=abc&lang=de&kind=asr", "languageCode": "de", "kind": "asr"},
                  {"baseUrl": "https://www.youtube.com/api/timedtext?v=abc&lang=de", "languageCode": "de"}]}}}
    page = f"<html><script>var ytInitialPlayerResponse = {json.dumps(player)};var x=1;</script></html>"
    subtitles = {"events": [{"segs": [{"utf8": "Jetzt die Soße "}, {"utf8": "20 Minuten köcheln."}]}]}
    seen = serve(monkeypatch, {
        "https://www.youtube.com/watch?v=abc123XYZ": httpx.Response(200, text=page),
        "https://www.youtube.com/api/timedtext?v=abc&lang=de&fmt": httpx.Response(200, json=subtitles),
        "https://i.ytimg.com/": httpx.Response(200, content=jpeg()),
    })
    result = url_processor.process_url("https://youtu.be/abc123XYZ", str(tmp_path))
    text = result["pages"][0]["text"]
    assert "Die beste Lasagne" in text and "500 g Hack" in text and "20 Minuten köcheln" in text
    assert result["metadata_title"] == "Die beste Lasagne" and len(result["images"]) == 1
    # the creator's own subtitles before the automatic ones
    assert any("lang=de&fmt=json3" in u for u in seen) and not any("kind=asr&fmt" in u for u in seen)


def test_tiktok_caption_via_oembed(monkeypatch, tmp_path):
    serve(monkeypatch, {
        "https://www.tiktok.com/oembed": httpx.Response(200, json={
            "title": "Schnelle Pasta 🍝 200 g Spaghetti, 2 Knoblauchzehen, Chili, Olivenöl – 10 Minuten!",
            "author_name": "pastaqueen", "thumbnail_url": "https://p16.tiktokcdn.com/x.jpg"}),
        "https://p16.tiktokcdn.com/": httpx.Response(200, content=jpeg()),
    })
    result = url_processor.process_url("https://www.tiktok.com/@pastaqueen/video/123", str(tmp_path))
    assert "200 g Spaghetti" in result["pages"][0]["text"] and "pastaqueen" in result["pages"][0]["text"]


def test_instagram_caption_from_the_embed_page(monkeypatch, tmp_path):
    page = '<meta property="og:description" content="12 likes - koch on May 1: &quot;Kuchen&quot;">'
    embed = '<div class="Caption"><a>koch</a> Apfelkuchen<br>200 g Mehl<br>3 Äpfel<br>180 °C, 40 Minuten backen</div>'
    serve(monkeypatch, {
        "https://www.instagram.com/p/Cx1/embed/captioned/": httpx.Response(200, text=embed),
        "https://www.instagram.com/reel/Cx1/": httpx.Response(200, text=page),
    })
    result = url_processor.process_url("https://www.instagram.com/reel/Cx1/", str(tmp_path))
    assert "200 g Mehl" in result["pages"][0]["text"]


def test_login_wall_gives_a_clear_message(monkeypatch, tmp_path):
    serve(monkeypatch, {"https://www.instagram.com/": httpx.Response(200, text="<html>Log in</html>")})
    with pytest.raises(url_processor.UrlImportError, match="paste it as text"):
        url_processor.process_url("https://www.instagram.com/p/Cx1/", str(tmp_path))
