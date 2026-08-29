FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=hamromentor.settings

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput

EXPOSE 8080

# --threads only has any effect with the gthread worker class (gunicorn's
# default is plain "sync", which ignores it entirely) — this was silently
# a no-op before, meaning each instance handled 2 real concurrent requests,
# not 2 workers x 4 threads = 8, as the flag implied.
CMD exec gunicorn hamromentor.wsgi:application --bind 0.0.0.0:${PORT:-8080} --worker-class gthread --workers 2 --threads 4 --timeout 60
