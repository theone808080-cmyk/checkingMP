FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl ca-certificates \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main\n' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends -y postgresql-client-17 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY collector ./collector
COPY config ./config
COPY db ./db
COPY judge ./judge
COPY notifier ./notifier
COPY web ./web
COPY worker ./worker
COPY watchdog ./watchdog
COPY backup ./backup
HEALTHCHECK --interval=2m --timeout=10s --start-period=10m --retries=2 CMD ["python", "main.py", "container-healthcheck"]
CMD ["python", "main.py", "web"]
