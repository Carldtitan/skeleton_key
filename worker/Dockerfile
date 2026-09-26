FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.54.0 docker==7.2.0
COPY agentd.py /app/agentd.py
WORKDIR /app
CMD ["sh", "-c", "uvicorn agentd:app --host ${SK_BIND_IP} --port 7000"]
