FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY app ./app
COPY static ./static
COPY migrations ./migrations
COPY alembic.ini .
RUN useradd --create-home careerpilot && mkdir /app/data && chown careerpilot:careerpilot /app/data
USER careerpilot
EXPOSE 8765
CMD ["python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8765","--no-access-log"]

