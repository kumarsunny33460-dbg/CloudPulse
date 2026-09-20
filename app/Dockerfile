FROM python:3.11-slim

# Prevent Python from creating .pyc files
# and ensure logs appear immediately.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Application directory
WORKDIR /app

# Install dependencies first for better Docker layer caching
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Application port
EXPOSE 5000

# Run Flask through Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]