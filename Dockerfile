FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies for yt-dlp and psycopg2
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    libpq-dev \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always upgrade yt-dlp to latest to avoid YouTube bot-detection breakage
RUN pip install --upgrade yt-dlp

# Copy application code
COPY . .

# Create data and static directories
RUN mkdir -p data static

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
