# Playwright base image ships Chromium + system deps preinstalled.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

# Dependencies only — versions mirror requirements.txt. No COPY/ADD: the app
# code, requirements.txt and .env are mounted as a volume at runtime (see
# docker-compose.yml), so nothing from the repo is baked into the image.
RUN pip install --no-cache-dir \
    beautifulsoup4==4.14.3 \
    curl_cffi==0.13.0 \
    openpyxl==3.1.5 \
    pandas==3.0.3 \
    playwright==1.60.0 \
    playwright-stealth==2.0.3 \
    requests==2.34.2

# Run as the host user (ec2-user = 1000:1000) so files written to the mounted
# volume are owned by the host user, not root. uid 1000 resolves to the base
# image's `ubuntu` user, whose /home/ubuntu is a private writable HOME — no need
# to set HOME ourselves.
USER 1000:1000
