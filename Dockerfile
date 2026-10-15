FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1
WORKDIR /opt/rag

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 rag
COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt

COPY app/ ./app/
COPY data/ ./data/
COPY server.py ./
COPY scripts/deploy/ ./scripts/deploy/
# Models and fake seed indexes are built in the image, without credentials.
RUN python scripts/deploy/cloud.py --prepare \
    && mkdir -p runtime && chown -R rag:rag runtime
USER rag
EXPOSE 10000
CMD ["python", "scripts/deploy/cloud.py"]
