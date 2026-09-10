# nastemp-2 / admin Dockerfile — neon control center on :6767
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.30.* pyyaml requests paramiko>=3.0
COPY app.py index.html ./
ENV NASTEMP_CONFIG=/config/config.yaml PYTHONUNBUFFERED=1
EXPOSE 6767
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "6767"]
