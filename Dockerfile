FROM node:20-alpine AS builder

WORKDIR /app

ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL

ARG VITE_TUS_THRESHOLD_BYTES
ENV VITE_TUS_THRESHOLD_BYTES=$VITE_TUS_THRESHOLD_BYTES

COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .
RUN npm run build

FROM node:20-alpine AS runner

WORKDIR /app

COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --omit=dev; else npm install --omit=dev; fi

COPY --from=builder /app/dist ./dist

RUN mkdir -p data

EXPOSE 3000

ENV NODE_ENV=production

CMD ["node", "dist/server.cjs"]
