# CI runner image: docker CLI + aws-cli + bash -- see INFRASTRUCTURE.md ("CI").
FROM docker:29.5.3
RUN apk add --no-cache aws-cli bash \
    && rm -rf /var/cache/apk/*
