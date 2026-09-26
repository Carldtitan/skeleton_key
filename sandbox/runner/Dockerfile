# Throwaway container that executes generated (untrusted) operation code.
FROM python:3.12-slim
RUN pip install --no-cache-dir curl_cffi==0.16.3 \
    && useradd -m -u 1000 sk && mkdir -p /work && chown sk /work
COPY runtime.py runner.py /opt/runner/
USER sk
WORKDIR /work
CMD ["python", "/opt/runner/runner.py"]
