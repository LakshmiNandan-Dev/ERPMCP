FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines VITE_* vars into the built JS at `npm run build` time, same
# as running `vite build` outside Docker (.env.example) — there's no
# runtime config step. A deployment pointing at a different management-api
# origin needs to rebuild this image with these ARGs overridden (see
# docker-compose.yml), not just restart the container with a new env var.
ARG VITE_API_BASE_URL=http://localhost:8010
ARG VITE_LOCAL_ADMIN_AUTH_ENABLED=false
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_LOCAL_ADMIN_AUTH_ENABLED=$VITE_LOCAL_ADMIN_AUTH_ENABLED

RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
