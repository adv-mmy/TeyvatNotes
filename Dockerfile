FROM node:24.15.0-bookworm-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d AS base

WORKDIR /app
COPY --chown=node:node package.json package-lock.json ./
RUN mkdir -p /app/dist /data/sessions && chown node:node /app/dist /data/sessions

FROM base AS build
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY --chown=node:node tsconfig.json ./
COPY --chown=node:node src ./src
USER node
RUN npm run build && chmod +x dist/src/bootstrap/main.js

FROM build AS acceptance
COPY --chown=node:node tests ./tests
COPY --chown=node:node resources/terminology/terminology.json ./resources/terminology/terminology.json
RUN npm run build
CMD ["node", "--test", "dist/tests/**/*.test.js"]

FROM base AS runtime
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && ln -s /app/dist/src/bootstrap/main.js /usr/local/bin/teyvatnotes
COPY --chown=node:node --from=build /app/dist/src ./dist/src
COPY --chown=node:node resources/terminology/terminology.json ./resources/terminology/terminology.json
USER node
CMD ["teyvatnotes"]
