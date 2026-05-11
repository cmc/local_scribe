# Egress proxy — local_scribe.egress.egress_proxy
#
# Pure asyncio HTTP CONNECT proxy. Standalone in a container. To make
# Char (or any client) actually route through it you need a host-level
# rule that sends the client's outbound traffic here — that work is
# OUT OF SCOPE for this Dockerfile and varies per Linux distro
# (iptables, nftables, etc.). On macOS the equivalent is the
# ``sandbox-exec`` profile in ``local_scribe.egress.char_sandbox``.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install requests

COPY local_scribe/ ./local_scribe/

EXPOSE 8889

CMD ["python", "-m", "local_scribe.egress.egress_proxy", \
     "start", "--port", "8889"]
