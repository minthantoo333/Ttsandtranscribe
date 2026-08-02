# Use Python 3.10 slim image
FROM python:3.10-slim

# Install system dependencies (FFmpeg and Fonts are crucial here)
RUN apt-get update && \
    apt-get install -y \
    ffmpeg \
    git \
    fontconfig \
    fonts-noto \
    fonts-noto-cjk \
    fonts-sil-padauk && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    fc-cache -fv

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY . .

# Expose the port
EXPOSE 10000

# Run the bot
CMD ["python", "bot.py"]
