FROM node:24.15.0-bookworm-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d

WORKDIR /app
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund && mkdir -p /app/dist /data/sessions \
    && chown node:node /app/dist /data/sessions
COPY --chown=node:node tsconfig.json ./
COPY --chown=node:node src ./src
COPY --chown=node:node tests ./tests
USER node
RUN npm run build

# 首批仅运行 SDK 合同验收；CLI 入口单独交付。
CMD ["node", "--test", "dist/tests/integration/pi-runtime.test.js"]
