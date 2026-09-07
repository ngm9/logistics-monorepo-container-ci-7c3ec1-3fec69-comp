FROM python:3.11-slim
WORKDIR /app
COPY shared ./shared
COPY services/carrier_sync ./services/carrier_sync
CMD ["python3", "services/carrier_sync/server.py"]
