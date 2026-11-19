# The app UI. Next's standalone output: a small Node server, no dev dependencies.
FROM node:26-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Rewrites are fixed at build time, so the API address is a build argument.
ARG API_URL=http://dashboard:8000
ENV API_URL=${API_URL} NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:26-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
