"""Pasted text, Markdown/.txt recipe text and Word documents as import sources."""
import io
import zipfile

from PIL import Image

from app import docx_processor, text_processor


def test_txt_is_a_link_list_only_when_it_is_mostly_links():
    assert text_processor.looks_like_link_list("https://a.example/1\nhttps://b.example/2\n")
    assert text_processor.looks_like_link_list("Soup: https://a.example/1\nCake – https://b.example/2")
    recipe = ("Kürbissuppe\n\nZutaten: 1 Kürbis, 1 Zwiebel, 500 ml Brühe\n\n"
              "Zubereitung: Kürbis würfeln, mit der Zwiebel andünsten, Brühe dazu, 20 Minuten köcheln "
              "und fein pürieren. Mit Salz und Pfeffer abschmecken. Idee von https://a.example/soup")
    assert not text_processor.looks_like_link_list(recipe)
    assert not text_processor.looks_like_link_list("Just a note without links")


def test_long_text_is_split_at_paragraphs():
    text = "\n\n".join(f"Rezept {i}\n" + "x" * 2500 for i in range(5))
    pages = text_processor.split_pages(text)
    assert len(pages) >= 3 and all(len(p["text"]) <= text_processor.MAX_PAGE_CHARS for p in pages)
    assert pages[0]["text"].startswith("Rezept 0") and [p["page"] for p in pages] == list(range(1, len(pages) + 1))


def test_title_from_text():
    assert text_processor.title_from_text("\n# Omas Kürbissuppe mit Ingwer und viel Liebe gekocht\nZutaten") == \
        "Omas Kürbissuppe mit Ingwer und viel Li…"


def _docx(tmp_path):
    img = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 120, 40)).save(img, "PNG")
    doc = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
<w:p><w:r><w:t>Kürbissuppe</w:t></w:r></w:p>
<w:p><w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>1 Kürbis</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>500 ml Brühe</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:r><w:t>Alles 20 Minuten </w:t></w:r><w:r><w:t>köcheln.</w:t></w:r></w:p>
</w:body></w:document>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId5" Type="image" Target="media/image1.png"/></Relationships>"""
    path = tmp_path / "rezepte.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/media/image1.png", img.getvalue())
    return path


def test_docx_text_tables_and_pictures(tmp_path):
    result = docx_processor.process_docx(str(_docx(tmp_path)), str(tmp_path / "images"))
    assert result["pages"] == [{"page": 1, "text": "Kürbissuppe\n1 Kürbis | 500 ml Brühe\nAlles 20 Minuten köcheln."}]
    assert len(result["images"]) == 1 and next(iter(result["images"].values()))["page"] == 1
