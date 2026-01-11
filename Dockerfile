# Use a lightweight Python version
FROM python:3.10-slim

# 1. Install FFmpeg and Fonts (Important for Burmese text support)
RUN apt-get update && \
    apt-get install -y ffmpeg fonts-noto fonts-noto-myanmar && \
    rm -rf /var/lib/apt/lists/*

# 2. Set working directory
WORKDIR /app

# 3. Copy files and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 4. Run the bot
CMD ["python", "bot.py"]
