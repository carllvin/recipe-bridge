FROM python:3.12-slim

WORKDIR /app

# System dependencies:
# - libglib2.0-0: required by PyMuPDF (image processing)
# - tesseract-ocr + language packs: OCR for scanned PDF pages and directly
#   uploaded photos. Add more "tesseract-ocr-<lang>" packages here and to
#   OCR_LANGUAGES in .env if your cookbooks are in other languages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    tzdata \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-deu \
    tesseract-ocr-fra \
    tesseract-ocr-ita \
    tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Set by the image workflow (the release tag, e.g. 1.0.0-beta.1) - shown in the app.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Persistent storage for uploaded PDFs / extracted images
RUN mkdir -p /app/data

EXPOSE 8000

# No curl/wget in this slim image - a tiny Python one-liner works without extra deps.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/ping', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
