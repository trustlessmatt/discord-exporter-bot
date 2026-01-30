FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

# Create directories for exports and digests
RUN mkdir -p /app/exports /app/digests

# Run the bot
CMD ["python", "-u", "bot.py"]
