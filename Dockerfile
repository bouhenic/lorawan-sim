FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY lorasim/ lorasim/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Utilisateur non-root ; config.yaml et config.state.json vivent dans /data (volume)
RUN useradd --uid 1000 --create-home lorasim \
    && mkdir /data && chown lorasim:lorasim /data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
USER lorasim
VOLUME /data

# Serveur LoRaWAN utilisé si /data/config.yaml n'existe pas encore
ENV LORASIM_SERVER=host.docker.internal \
    LORASIM_DEVICES=0

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/state', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["run", "--web-host", "0.0.0.0", "--web", "8000"]
