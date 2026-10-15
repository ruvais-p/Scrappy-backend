FROM python:3.14.3-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home app && mkdir -p private-storage && chown -R app:app /app
USER app
EXPOSE 8000
