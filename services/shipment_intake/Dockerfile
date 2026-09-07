FROM python:3.11-slim
WORKDIR /app
COPY shared ./shared
COPY services/shipment_intake ./services/shipment_intake
CMD ["python3", "services/shipment_intake/server.py"]
