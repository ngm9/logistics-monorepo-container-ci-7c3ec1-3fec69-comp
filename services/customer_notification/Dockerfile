FROM python:3.11-slim
WORKDIR /app
COPY shared ./shared
COPY services/customer_notification ./services/customer_notification
CMD ["python3", "services/customer_notification/server.py"]
