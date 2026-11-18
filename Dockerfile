FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv
WORKDIR /srv

# Dependencies first and SERVICE arg last: the pip layer is cached and shared by all five images.
COPY requirements-runtime.txt .
RUN pip install --no-cache-dir --retries 10 --default-timeout 100 -r requirements-runtime.txt
RUN useradd --system --uid 10001 app

ARG SERVICE
COPY shared ./shared
COPY services/${SERVICE}/app ./app

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]