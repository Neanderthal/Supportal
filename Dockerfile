# supportal — embeddable relay "web-chat user ⇄ operator in Telegram".
FROM python:3.12-slim

LABEL org.opencontainers.image.title="supportal" \
      org.opencontainers.image.description="Embeddable relay: web-chat user to support operator in Telegram" \
      org.opencontainers.image.source="https://github.com/Neanderthal/supportal" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies (kept in sync with pyproject.toml).
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        fastapi "uvicorn[standard]" "aiogram>=3.13" "aiohttp-socks>=0.8" httpx "pydantic>=2.8"

COPY supportal/ ./supportal/

# Run as non-root.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8500

# Liveness: hit the health endpoint with the stdlib (no extra deps).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8500/health').status==200 else 1)"

# Ingress from the product is internal; Telegram uses long-polling (egress), no inbound webhook.
CMD ["uvicorn", "supportal.app:app", "--host", "0.0.0.0", "--port", "8500"]
