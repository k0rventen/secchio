FROM alpine:3.24

RUN apk add --no-cache jq ca-certificates tzdata
COPY --from=restic/restic:0.19.1 /usr/bin/restic /usr/bin/restic

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
