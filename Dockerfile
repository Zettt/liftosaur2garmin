FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir .

EXPOSE 8123

ENTRYPOINT ["liftosaur2garmin"]
CMD ["status"]
