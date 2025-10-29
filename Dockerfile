# Usa a imagem oficial do Playwright com browsers já instalados
FROM mcr.microsoft.com/playwright/python:v1.46.0

WORKDIR /app

# Dependências Python
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código
COPY . /app

# Ambiente
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PORT=8080

# Healthcheck simples (Railway)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

# Arranque FastAPI - usando shell form para expandir $PORT
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
