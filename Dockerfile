FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install ffmpeg and necessary OS libs at build time
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python deps
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip && \
    pip install -r /app/requirements.txt

# Copy app
COPY . /app

ENV PORT=10000

# Start (start.sh must NOT run apt-get)
CMD ["bash", "start.sh"]