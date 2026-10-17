# Backend GUARDIAN (FastAPI). Se construye desde la RAÍZ del proyecto:
#   docker build -f backend/Dockerfile -t guardian-backend .
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ultralytics trae opencv-python (con interfaz gráfica); se reemplaza por la versión headless
RUN pip uninstall -y opencv-python && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

# Pre-descarga los modelos de personas y de postura para que funcione sin internet
RUN python -c "from ultralytics import YOLO; YOLO('yolo11n.pt'); YOLO('yolo11n-pose.pt')"

# Copia todo el proyecto (engine.py y los detectores viven en la raíz)
COPY . .

EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]