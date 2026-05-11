# Inspector — local_scribe.inspector.inspector_server
#
# Pure Python web UI. Containerises trivially; remember that without the
# rest of the macOS-host security model the data this surfaces ISN'T
# actually private — see ../containers/README.md.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install fastapi 'uvicorn[standard]'

COPY local_scribe/ ./local_scribe/

EXPOSE 8001

CMD ["python", "-m", "uvicorn", \
     "local_scribe.inspector.inspector_server:app", \
     "--host", "0.0.0.0", "--port", "8001"]
