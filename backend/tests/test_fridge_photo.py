"""Photo of the fridge -> ingredients for 'what can I cook today?'."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from app import cook_today, llm_provider, main, usage_log
from app.schemas import TokenUsage


def jpeg(size=(3000, 2000)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "JPEG")
    return buf.getvalue()


def test_ingredients_are_merged_across_photos(monkeypatch):
    answers = iter(['```json\n["Zucchini", "Feta", "Eier"]\n```', '["feta", "Paprika"]'])
    sizes = []

    def fake(data, prompt, max_tokens=800):
        sizes.append(Image.open(io.BytesIO(data)).size)
        assert "Deutsch" in prompt
        return next(answers), TokenUsage(input_tokens=1000, output_tokens=20)
    monkeypatch.setattr(llm_provider, "transcribe_image", fake)
    names, tokens_in, tokens_out = cook_today.ingredients_from_photos([jpeg(), jpeg()])
    assert names == ["Zucchini", "Feta", "Eier", "Paprika"]
    assert (tokens_in, tokens_out) == (2000, 40)
    assert max(sizes[0]) == cook_today.PHOTO_MAX_SIDE  # sent smaller


def test_endpoint(monkeypatch):
    monkeypatch.setattr(llm_provider, "is_configured", lambda: True)
    monkeypatch.setattr(llm_provider, "transcribe_image",
                        lambda data, prompt, max_tokens=800: ('["Tomate"]', TokenUsage(input_tokens=5, output_tokens=1)))
    recorded = []
    monkeypatch.setattr(usage_log, "record", lambda *a: recorded.append(a))
    res = TestClient(main.app).post("/api/cook-today/photo", files=[("files", ("kuehlschrank.jpg", jpeg((200, 100)), "image/jpeg"))])
    assert res.json() == {"ingredients": ["Tomate"]}
    assert recorded == [("cook_today_photo", 5, 1)]
