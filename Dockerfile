FROM node:24.15.0-bookworm-slim@sha256:4e6b70dd6cbfc88c8157ba19aa3d9f9cce6ba4703576d55459e45efcbc9c5f5d

WORKDIR /app
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund && mkdir -p /app/dist /data/sessions \
    && chown node:node /app/dist /data/sessions \
    && ln -s /app/dist/src/bootstrap/main.js /usr/local/bin/teyvatnotes
COPY --chown=node:node tsconfig.json ./
COPY --chown=node:node src ./src
COPY --chown=node:node tests ./tests
USER node
RUN npm run build && chmod +x dist/src/bootstrap/main.js

CMD ["teyvatnotes"]
