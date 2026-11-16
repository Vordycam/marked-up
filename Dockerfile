FROM python:3.12-slim

WORKDIR /app
COPY markedup ./markedup
COPY static ./static
COPY server.py ./

ENV HOST=0.0.0.0 \
    PORT=8080 \
    MARKEDUP_DB=/data/markedup.sqlite3

RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "server.py"]
