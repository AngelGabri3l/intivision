"""
video_buffer.py — Pre-buffer: mantiene en memoria los últimos N segundos y, al alertar,
vuelca un clip .mp4 liviano con los segundos previos al evento.
"""
import time
import logging
from collections import deque

import cv2

log = logging.getLogger("video_buffer")


class VideoBuffer:
    def __init__(self, seconds=15, clip_fps=8, max_width=640):
        self.seconds = seconds
        self.clip_fps = clip_fps
        self.max_width = max_width
        self.buffer = deque(maxlen=seconds * clip_fps)
        self._last_store = 0.0
        self._interval = 1.0 / clip_fps

    def push(self, frame):
        now = time.time()
        if now - self._last_store < self._interval:
            return
        self._last_store = now
        f = frame
        h, w = frame.shape[:2]
        if w > self.max_width:
            scale = self.max_width / w
            f = cv2.resize(frame, (self.max_width, int(h * scale)))
        self.buffer.append((now, f.copy()))

    def save_clip(self, path):
        if not self.buffer:
            return None
        frames = [f for _, f in list(self.buffer)]
        h, w = frames[0].shape[:2]
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"),
                                 self.clip_fps, (w, h))
        if not writer.isOpened():
            log.error("No se pudo crear el clip (codec mp4v no disponible).")
            return None
        for f in frames:
            if f.shape[:2] != (h, w):
                f = cv2.resize(f, (w, h))
            writer.write(f)
        writer.release()
        log.info("🎬 Clip guardado: %s (%d frames)", path, len(frames))
        return path
