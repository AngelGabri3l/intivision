# Agente de la mini PC (VIGÍA). Se construye desde la RAÍZ del proyecto:
#   docker build -f edge/Dockerfile -t vigia-agent .
# o, desde edge/:  docker compose build
#
# Con el motor de detección (por defecto) la imagen es GRANDE: lleva PyTorch, ultralytics y los pesos de YOLO.
# Para una imagen liviana, que solo lee cámaras y envía el inventario, sin detección:
#   docker compose build --build-arg MOTOR=0          (o MOTOR=0 en edge/.env)
FROM python:3.11-slim

ARG MOTOR=1

# libglib2.0-0: la necesita OpenCV al importarse en la imagen "slim". tzdata: la hora local (el horario sensible usa la hora de Lima).
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY edge/requirements.txt edge/requirements.txt
RUN pip install --no-cache-dir -r edge/requirements.txt

# El motor: el mismo patrón que la imagen del servidor (PyTorch para CPU, ultralytics, OpenCV sin interfaz gráfica y los pesos
# bajados AHORA, para que el agente no dependa de internet al arrancar). Los pesos quedan en /app, donde el motor los busca.
COPY edge/requirements-motor.txt edge/requirements-motor.txt
RUN if [ "$MOTOR" = "1" ]; then \
        pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
        && pip install --no-cache-dir -r edge/requirements-motor.txt \
        && pip uninstall -y opencv-python \
        && pip install --no-cache-dir --force-reinstall --no-deps "opencv-python-headless>=4.8.0,<6" \
        && python -c "from ultralytics import YOLO; YOLO('yolo11n.pt'); YOLO('yolo11n-pose.pt')"; \
    fi

# La imagen en vivo (cliente WebSocket). En una capa APARTE y después del motor: si cambia, no se reinstala PyTorch.
COPY edge/requirements-live.txt edge/requirements-live.txt
RUN pip install --no-cache-dir -r edge/requirements-live.txt

# Los módulos del motor (los mismos que usa el servidor; el agente no los modifica) y el agente.
COPY engine.py detector.py pose_detector.py threat_scoring.py tracker.py confirmation.py config.yaml ./
COPY edge/agent edge/agent

ENV PYTHONPATH=/app/edge:/app \
    PYTHONUNBUFFERED=1 \
    AGENT_CONFIG=/data/agent.yaml \
    TZ=America/Lima

VOLUME /data

# Se usa así:  docker compose run --rm agent listar
ENTRYPOINT ["python", "-m", "agent"]
CMD ["vigilar"]
