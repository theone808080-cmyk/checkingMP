FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./
COPY collector ./collector
COPY config ./config
COPY db ./db
COPY web ./web
COPY worker ./worker
CMD ["python", "main.py", "web"]
