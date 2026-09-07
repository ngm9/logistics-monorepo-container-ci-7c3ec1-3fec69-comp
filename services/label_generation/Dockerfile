FROM python:3.11-slim
WORKDIR /app
COPY shared ./shared
COPY services/label_generation ./services/label_generation
CMD ["python3", "services/label_generation/server.py"]
