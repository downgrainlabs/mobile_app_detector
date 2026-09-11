FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY golfapps ./golfapps

RUN pip install --no-cache-dir .

CMD ["golfapps", "monthly"]
