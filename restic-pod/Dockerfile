# the restic image already has everything we need (jq / tzdata / ca-certs)
FROM restic/restic:0.20.0@sha256:93999df80d4ac6cb148e82d2ef1fe6a81996f17ff535e772723538211b31555a

# we just copy our script as entrypoint
COPY --chmod=+x entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
