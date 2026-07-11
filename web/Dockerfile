FROM node:22-slim

RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

CMD ["pnpm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
