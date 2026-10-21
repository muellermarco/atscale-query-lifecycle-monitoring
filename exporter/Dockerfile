# Sample code, provided as is under the MIT License. See DISCLAIMER.md.
# Build: docker build -t atscale-query-exporter:0.1.0 exporter/
FROM python:3.12.7-slim

WORKDIR /app
COPY exporter.py /app/exporter.py

USER 65534:65534
EXPOSE 9101
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-I", "/app/exporter.py"]
