FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shopping_optimizer.py .

EXPOSE 8099

CMD ["python", "shopping_optimizer.py"]
