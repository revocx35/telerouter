FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY telerouter ./telerouter
COPY client ./client
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin telerouter \
 && mkdir -p /data && chown 1000:1000 /data && chmod 700 /data \
 && python -m compileall -q telerouter

ENV TELEROUTER_DATA_DIR=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
EXPOSE 8443/tcp 5080/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import ssl,urllib.request,os; urllib.request.urlopen('https://127.0.0.1:%s/api/health' % os.environ.get('TELEROUTER_HTTPS_PORT','8443'), context=ssl._create_unverified_context(), timeout=4)"
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "telerouter"]
