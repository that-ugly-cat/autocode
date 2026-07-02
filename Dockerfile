FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# spaCy models for sentence segmentation (en/de/fr/it)
RUN python -m spacy download en_core_web_md && \
    python -m spacy download de_core_news_md && \
    python -m spacy download fr_core_news_md && \
    python -m spacy download it_core_news_md

COPY . .

RUN mkdir -p data/uploads

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8007"]
