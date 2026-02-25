FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# L'orchestrateur tourne en continu (boucle de polling)
CMD ["python", "-u", "main.py"]
