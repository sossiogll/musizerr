# ------------------------------------------------------------------
# Stage: development — pnpm dev server con HMR
# ------------------------------------------------------------------
FROM node:24-alpine AS development

# pnpm via corepack: usa esattamente la versione dichiarata in package.json
# ("packageManager"). Un `npm install -g pnpm` senza versione prende l'ultima
# release, che puo' non avere il binario nativo per alpine/arm64 e rompe la build.
RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml* ./
RUN pnpm install

COPY . .

EXPOSE 5173

CMD ["pnpm", "dev", "--host", "0.0.0.0"]


# ------------------------------------------------------------------
# Stage: builder — compila il frontend per la produzione
# ------------------------------------------------------------------
FROM node:24-alpine AS builder

# pnpm via corepack: usa esattamente la versione dichiarata in package.json
# ("packageManager"). Un `npm install -g pnpm` senza versione prende l'ultima
# release, che puo' non avere il binario nativo per alpine/arm64 e rompe la build.
RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile

COPY . .

# The version of the web app (ADR-0007 §2), a constant of the build (lib/version.ts).
ARG MUSIZEER_VERSION=dev
ENV VITE_MUSIZEER_VERSION=$MUSIZEER_VERSION

RUN pnpm build


# ------------------------------------------------------------------
# Stage: production — nginx serve i file statici e proxia /api/
#
# The nginx configuration is a TEMPLATE the official image renders at every start (envsubst): only the
# variables below are replaced, the `$host`-style variables of nginx are not. MUSIZEER_BACKEND_URL is where the API
# is (scheme://host:port), MUSIZEER_DNS_RESOLVER the name server nginx asks for its address; the name is resolved
# again every few seconds, so a recreated API container is found. The image declares NO default for either: they are
# read, checked and defaulted by 15-backend-url.envsh, the one reader of this image's environment (ADR-0009), so an
# unset variable is told from a chosen one. docs/deployment.md.
# ------------------------------------------------------------------
FROM nginx:alpine AS production

ENV NGINX_ENVSUBST_FILTER="^(MUSIZEER_BACKEND_URL|MUSIZEER_DNS_RESOLVER)$"

COPY --from=builder /app/dist /usr/share/nginx/html
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
# Sourced by the entrypoint before the templates are rendered (an executable .envsh is sourced, a .sh run), and the
# IPv6 listener the official image would add to a default.conf that is not rendered yet when it looks.
COPY nginx/15-backend-url.envsh /docker-entrypoint.d/15-backend-url.envsh
COPY nginx/25-listen-on-ipv6.sh /docker-entrypoint.d/25-listen-on-ipv6.sh
RUN chmod 755 /docker-entrypoint.d/15-backend-url.envsh /docker-entrypoint.d/25-listen-on-ipv6.sh

# A template that does not render to a valid configuration fails the build, not the first start.
RUN . /docker-entrypoint.d/15-backend-url.envsh \
    && envsubst '${MUSIZEER_BACKEND_URL} ${MUSIZEER_DNS_RESOLVER}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf \
    && nginx -t \
    && rm /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
