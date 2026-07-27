FROM python:3.12-slim

WORKDIR /app

COPY . .
RUN pip install --no-cache-dir -e ".[dev]"

# --proxy-headers/--forwarded-allow-ips='*' makes uvicorn trust X-Forwarded-For so
# per-IP rate limiting and Agreement.accepted_ip see the real client, not the load
# balancer. This assumes the container is only reachable via the platform's edge
# (Railway et al.), which sets/strips those headers. Never expose it directly to the
# internet with these flags — callers could then spoof their own IP.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
