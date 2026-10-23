# The server image: this backend with lp (the tutor core) next to it, as the path dependency expects.
# Built from the folder holding both repos (ops/deploy.sh syncs them to /opt/langlab on the server):
#   docker compose -f lang-lab-backend/ops/compose.yml build
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng libsndfile1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.12.23

ENV UV_PROJECT_ENVIRONMENT=/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_NO_CACHE=1 PATH=/venv/bin:$PATH \
    HF_HOME=/root/.cache/huggingface PYTHONUNBUFFERED=1

# dependencies first, so a code change doesn't reinstall them
COPY learning-lang-platform/pyproject.toml learning-lang-platform/README.md /srv/learning-lang-platform/
COPY learning-lang-platform/lp/__init__.py /srv/learning-lang-platform/lp/__init__.py
COPY lang-lab-backend/pyproject.toml lang-lab-backend/uv.lock /srv/lang-lab-backend/
WORKDIR /srv/lang-lab-backend
RUN uv sync --frozen --no-dev --no-install-project

# models the server can't fetch lazily or shouldn't make the first lesson wait for: Kokoro voices (no download
# in lp), the pronunciation models, the Kazakh Piper voice (Azure's fallback)
RUN mkdir -p /root/.cache/lp/piper \
    && K=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0 \
    && curl -fsSL -o /root/.cache/lp/kokoro-v1.0.onnx $K/kokoro-v1.0.onnx \
    && curl -fsSL -o /root/.cache/lp/voices-v1.0.bin $K/voices-v1.0.bin \
    && P=https://huggingface.co/rhasspy/piper-voices/resolve/main/kk/kk_KZ/issai/high/kk_KZ-issai-high.onnx \
    && curl -fsSL -o /root/.cache/lp/piper/kk_KZ-issai-high.onnx $P \
    && curl -fsSL -o /root/.cache/lp/piper/kk_KZ-issai-high.onnx.json $P.json \
    && python -c "from transformers import AutoProcessor, AutoModelForCTC as M; \
[(AutoProcessor.from_pretrained(m), M.from_pretrained(m)) for m in ('facebook/wav2vec2-lv-60-espeak-cv-ft', 'aismlv/wav2vec2-large-xlsr-kazakh')]"

COPY learning-lang-platform /srv/learning-lang-platform
COPY lang-lab-backend /srv/lang-lab-backend
RUN uv sync --frozen --no-dev

EXPOSE 8000
# one process: live tutor sessions are held in memory
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
