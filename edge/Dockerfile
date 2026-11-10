# Agente de la mini PC (VIGÍA). Se construye desde la RAÍZ del proyecto:
#   docker build -f edge/Dockerfile -t vigia-agent .
# o, desde edge/:  docker compose build
FROM python:3.11-slim

# libglib2.0-0: la necesita OpenCV al importarse en la imagen "slim"
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY edge/requirements.txt edge/requirements.txt
RUN pip install --no-cache-dir -r edge/requirements.txt

COPY edge/agent edge/agent

ENV PYTHONPATH=/app/edge \
    PYTHONUNBUFFERED=1 \
    AGENT_CONFIG=/data/agent.yaml

VOLUME /data

# Se usa así:  docker compose run --rm agent listar
ENTRYPOINT ["python", "-m", "agent"]
CMD ["vigilar"]
