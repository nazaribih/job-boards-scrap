FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=Europe/Warsaw
RUN pip install --no-cache-dir requests==2.32.3 tzdata
COPY . .
CMD ["python", "service.py"]
