ARG ZOLA_VERSION=0.23.6

# Runs on the CI machine's own CPU (no emulation needed)
FROM --platform=$BUILDPLATFORM ghcr.io/getzola/zola:v${ZOLA_VERSION} AS build
WORKDIR /site
COPY . .
RUN ["zola", "build"]

# The final stage only copies files, so it can target any architecture
FROM ghcr.io/static-web-server/static-web-server:2
COPY --from=build /site/public /public
