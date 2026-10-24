FROM node:22-bookworm-slim AS build

RUN corepack enable && corepack prepare pnpm@10.12.4 --activate

WORKDIR /workspace

COPY examples/real-world-attempt.example.json ./examples/real-world-attempt.example.json
COPY docs/media/nexttime-demo.webm ./docs/media/nexttime-demo.webm
COPY ui/tokens.json ./ui/tokens.json
COPY apps/web/package.json apps/web/pnpm-lock.yaml apps/web/tsconfig.json apps/web/vite.config.ts apps/web/index.html ./apps/web/
COPY apps/web/public ./apps/web/public
COPY apps/web/src ./apps/web/src

RUN pnpm --dir apps/web install --frozen-lockfile \
    && pnpm --dir apps/web build

FROM nginx:1.27-alpine

COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /workspace/apps/web/dist /usr/share/nginx/html/nexttime

EXPOSE 3000

CMD ["nginx", "-g", "daemon off;"]
