# Stage 1: Dependencies
FROM node:20-alpine AS deps
WORKDIR /app

# Copy SDK first (local dependency)
COPY sdk-typescript/ ./sdk-typescript/

# Install SDK deps + build it
WORKDIR /app/sdk-typescript
RUN --mount=type=cache,target=/root/.npm npm ci && npm run build

# Copy portal package files
WORKDIR /app/portal
COPY portal/package.json portal/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

# Stage 2: Build
FROM deps AS builder
WORKDIR /app/portal
COPY portal/ .
RUN npm run build

# Stage 3: Production
FROM node:20-alpine AS runner
LABEL org.opencontainers.image.title="rat-portal"
LABEL org.opencontainers.image.description="RAT portal (Next.js web IDE)"
LABEL org.opencontainers.image.source="https://github.com/squat-collective/rat"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.vendor="RAT Data"
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

RUN adduser -D -u 1001 appuser

# Copy standalone output
COPY --from=builder /app/portal/.next/standalone ./
COPY --from=builder /app/portal/.next/static ./.next/static
COPY --from=builder /app/portal/public ./public

USER appuser
EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=30s \
  CMD ["wget", "-qO-", "http://localhost:3000"]

CMD ["node", "server.js"]
