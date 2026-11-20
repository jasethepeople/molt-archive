FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /app

COPY package.json pnpm-lock.yaml .npmrc ./
RUN corepack enable && pnpm install --frozen-lockfile --prod && pnpm store prune

COPY --chown=node:node src ./src
COPY --chown=node:node migrations ./migrations
COPY --chown=node:node public ./public

USER node
EXPOSE 3000
CMD ["node", "src/server.mjs"]
