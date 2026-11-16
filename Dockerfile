FROM python:3.12-slim

WORKDIR /app
COPY asasakyan ./asasakyan
COPY static ./static
COPY server.py ./

ENV HOST=0.0.0.0 \
    PORT=8080 \
    ASASAKYAN_DB=/data/asasakyan.sqlite3

RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "server.py"]
