FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies needed for matplotlib/fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose state server port
EXPOSE 8765

# Run bot in background
CMD ["python", "main.py"]
