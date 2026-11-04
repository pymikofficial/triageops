# TriageOps container images. Build context is the repository root.
#
#   docker build -f deploy/Dockerfile --target app -t triageops-app .
#   docker build -f deploy/Dockerfile --target web -t triageops-web .
#
# "app" is the FastAPI backend (also the default, last stage).
# "web" is nginx serving the built React frontend.

# ---------- frontend build ----------
FROM node:22-alpine AS web-build
WORKDIR /web
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY . .
RUN npm run build

# ---------- nginx with the static SPA ----------
FROM nginx:1.27-alpine AS web
# The template is rendered by the official entrypoint (envsubst) into
# /etc/nginx/conf.d/default.conf using DOMAIN, APP_UPSTREAM and WEB_ROOT.
COPY deploy/nginx/triageops.conf /etc/nginx/templates/default.conf.template
COPY --from=web-build /web/dist /usr/share/nginx/html
ENV WEB_ROOT=/usr/share/nginx/html \
    APP_UPSTREAM=app:8000

# ---------- backend ----------
FROM python:3.12-slim AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/triageops

# Dependencies first so code changes do not invalidate the pip layer.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt "uvicorn[standard]>=0.30"

# Unprivileged runtime user with no shell and no home directory writes.
RUN groupadd --system --gid 10001 triageops \
 && useradd --system --uid 10001 --gid triageops --no-create-home --shell /usr/sbin/nologin triageops

COPY --chown=root:root triageops/ ./triageops/

USER triageops
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# Binds 0.0.0.0 inside the container only. docker-compose does not publish this
# port, so the app is reachable from nginx on the internal network and nowhere else.
CMD ["uvicorn", "triageops.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
