# Playwright's official image ships Chromium + all OS deps pre-installed —
# avoids hand-rolling the headless-Chrome dependency list.
FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

ENTRYPOINT ["zerodom-mcp"]
