# ASR server — local_scribe.asr.asr_server
#
# IMPORTANT: this is the Linux-portable subset. It uses the
# ``faster-whisper`` backend, NOT the default ``parakeet-mlx`` (which
# requires Apple Silicon + MLX framework). The container is suitable for
# a dedicated transcription worker box; it is NOT a drop-in replacement
# for the full macOS deployment, which depends on macOS-only security
# facilities (Keychain, sandbox-exec, hdiutil sparse bundles, Touch ID).
#
# Build:
#   docker build -f containers/asr.Dockerfile -t local-scribe-asr .
# Run:
#   docker run --rm -p 8000:8000 \
#     -e ASR_BACKEND=faster-whisper \
#     -e LOCAL_SCRIBE_DISABLE_AUTH=1 \
#     local-scribe-asr

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install just the deps needed for the Linux subset. We deliberately skip
# parakeet-mlx (Apple-Silicon-only) and the macOS-specific security stack.
COPY pyproject.toml .
RUN pip install \
        fastapi 'uvicorn[standard]' python-multipart \
        faster-whisper \
        sherpa-onnx \
        numpy soundfile librosa \
        requests huggingface_hub

# Package source.
COPY local_scribe/ ./local_scribe/

EXPOSE 8000

ENV ASR_BACKEND=faster-whisper

CMD ["python", "-m", "uvicorn", \
     "local_scribe.asr.asr_server:app", \
     "--host", "0.0.0.0", "--port", "8000"]
