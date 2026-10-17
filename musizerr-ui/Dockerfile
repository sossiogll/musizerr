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
FROM node:20-alpine AS builder

# pnpm via corepack: usa esattamente la versione dichiarata in package.json
# ("packageManager"). Un `npm install -g pnpm` senza versione prende l'ultima
# release, che puo' non avere il binario nativo per alpine/arm64 e rompe la build.
RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile

COPY . .

# The version of the web app (ADR-0007 §2), a constant of the build (lib/version.ts).
ARG MUSIZERR_VERSION=dev
ENV VITE_MUSIZERR_VERSION=$MUSIZERR_VERSION

RUN pnpm build


# ------------------------------------------------------------------
# Stage: production — nginx serve i file statici e proxia /api/
# ------------------------------------------------------------------
FROM nginx:alpine AS production

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
