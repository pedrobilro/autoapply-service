# Usa a imagem oficial do Playwright com browsers já instalados
FROM mcr.microsoft.com/playwright/python:v1.46.0

WORKDIR /app

# Dependências Python (copiadas a partir do monorepo)
COPY python-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copia apenas o serviço Python
COPY python-service/ /app

# Ambiente
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Arranque FastAPI - Railway define $PORT dinamicamente
CMD uvicorn main_v2:app --host 0.0.0.0 --port ${PORT:-8080}
