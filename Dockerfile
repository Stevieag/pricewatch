FROM python:3.12-slim

# No dependencies to install: PriceWatch is standard-library only.
RUN useradd --create-home --uid 10001 pricewatch
WORKDIR /app
COPY pricewatch/ /app/pricewatch/

USER pricewatch
ENV PW_HOST=0.0.0.0 \
    PW_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=4)" || exit 1
CMD ["python", "-m", "pricewatch", "serve"]
