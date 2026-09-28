"""Handwritten recipe photos: the AI transcribes them instead of OCR."""
from PIL import Image

from app import image_processor
from app.schemas import TokenUsage


def photos(tmp_path, n=2):
    paths = []
    for i in range(n):
        p = tmp_path / f"card{i}.jpg"
        Image.new("RGB", (3000, 2000), "white").save(p)
        paths.append(str(p))
    return paths


def test_ai_reads_the_photos(tmp_path, monkeypatch):
    sizes = []

    def fake(jpeg, prompt, max_tokens=4000):
        import io
        sizes.append(Image.open(io.BytesIO(jpeg)).size)
        assert "handwritten" in prompt
        return "Omas Apfelkuchen\n250 g Mehl", TokenUsage(input_tokens=1500, output_tokens=40)
    monkeypatch.setattr(image_processor.llm_provider, "transcribe_image", fake)
    monkeypatch.setattr(image_processor, "ocr_image", lambda img: (_ for _ in ()).throw(AssertionError("no OCR")))
    result = image_processor.process_images(photos(tmp_path), str(tmp_path / "img"), handwriting=True)
    assert [p["text"] for p in result["pages"]] == ["Omas Apfelkuchen\n250 g Mehl"] * 2
    assert result["usage"].input_tokens == 3000 and result["usage"].output_tokens == 80
    assert max(sizes[0]) == image_processor.HANDWRITING_MAX_SIDE  # sent smaller
    assert len(result["images"]) == 2  # the photos still count as recipe images


def test_falls_back_to_ocr(tmp_path, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("no vision")
    monkeypatch.setattr(image_processor.llm_provider, "transcribe_image", broken)
    monkeypatch.setattr(image_processor, "ocr_image", lambda img: "OCR text")
    result = image_processor.process_images(photos(tmp_path, 1), str(tmp_path / "img"), handwriting=True)
    assert result["pages"][0]["text"] == "OCR text" and result["usage"].input_tokens == 0


def test_without_handwriting_ocr_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(image_processor.llm_provider, "transcribe_image",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no AI")))
    monkeypatch.setattr(image_processor, "ocr_image", lambda img: "OCR text")
    result = image_processor.process_images(photos(tmp_path, 1), str(tmp_path / "img"))
    assert result["pages"][0]["text"] == "OCR text"


def test_upload_passes_the_option(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    calls = []
    monkeypatch.setattr(main, "_run_extraction", lambda *args: calls.append(args))
    client = TestClient(main.app)
    files = [("files", ("karte.jpg", b"\xff\xd8x", "image/jpeg"))]
    assert client.post("/api/upload", files=files, data={"handwriting": "true"}).status_code == 200
    assert client.post("/api/upload", files=files).status_code == 200
    assert [c[-1] for c in calls] == [True, False]
