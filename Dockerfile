FROM python:3.11-slim

WORKDIR /app

# system deps (tzdata optional)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    tzdata \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# copy only what we need
COPY scripts /app/scripts
COPY standx /app/standx

# runtime
ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

# set container timezone
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# default command: supervisor-style loop runner (see entrypoint)
COPY standx/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
