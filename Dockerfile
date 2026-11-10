FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
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
CMD ["python", "main.py", "web"]
