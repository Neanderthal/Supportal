# dali-support — relay «веб-чат ↔ оператор в Telegram». Канонический образ (нормальный DNS, CI/GHCR).
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" "aiogram>=3.13" "aiohttp-socks>=0.8" httpx "pydantic>=2.8"
COPY dali_support/ ./dali_support/
EXPOSE 8500
# Ingress от продукта — по внутренней сети; Telegram — long-polling (egress), публичный inbound-webhook не нужен.
CMD ["uvicorn", "dali_support.app:app", "--host", "0.0.0.0", "--port", "8500"]
