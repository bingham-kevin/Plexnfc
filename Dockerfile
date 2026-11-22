FROM python:3.12-slim
WORKDIR /app
# ffmpeg: posters inside movie files, and MKV/AVI from folder sources for the Marquee app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY plexnfc plexnfc
EXPOSE 8080
CMD ["python", "-m", "plexnfc.server"]
