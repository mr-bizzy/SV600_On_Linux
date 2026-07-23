# PaddleOCR UVDoc document-unwarping, GPU, containerised so nothing lands on the
# host but the driver. Base image bundles a matching CUDA/cuDNN + PaddlePaddle,
# which is the fragile part to get right by hand.
FROM paddlepaddle/paddle:3.1.0-gpu-cuda12.6-cudnn9.5

# libGL/glib: PaddleX pulls in OpenCV, which needs these shared libs even for
# headless array work (it imports cv2 at module load). Without them the very
# first `from paddleocr import ...` dies on libGL.so.1.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# PaddleOCR 3.x ships the TextImageUnwarping (UVDoc) module.
RUN pip install --no-cache-dir "paddleocr>=3.0" && \
    rm -rf /root/.cache/pip

# Model weights download on first run into here; mount it as a volume so the
# ~100 MB UVDoc download happens once, not every container.
ENV HUB_HOME=/models
WORKDIR /work
COPY unwarp.py /usr/local/bin/unwarp.py
ENTRYPOINT ["python", "/usr/local/bin/unwarp.py"]
