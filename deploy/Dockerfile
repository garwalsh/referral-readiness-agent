FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt ./
COPY dashboard/requirements.txt dashboard/
COPY deploy/requirements.txt deploy/
RUN pip install --no-cache-dir -r dashboard/requirements.txt -r deploy/requirements.txt

COPY manage.py ./
COPY dashboard dashboard
COPY scripts/run_agent.py scripts/reporting.py scripts/verify_langcare.py scripts/
RUN DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
    DJANGO_USE_WHITENOISE=1 python manage.py collectstatic --noinput

USER 10001:10001
