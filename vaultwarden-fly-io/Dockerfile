FROM alpine AS fetcher
RUN apk add wget tar
RUN arch | sed s/aarch64/arm64/ | sed s/x86_64/amd64/ > /tmp/arch

FROM fetcher AS litestream
RUN arch=$(cat /tmp/arch) &&\
    wget https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-${arch}.tar.gz && \
    tar -xf litestream-v0.3.13-linux-${arch}.tar.gz

FROM fetcher AS geesefs
RUN arch=$(cat /tmp/arch) &&\
    wget https://github.com/yandex-cloud/geesefs/releases/download/v0.41.3/geesefs-linux-${arch} -O geesefs && \
    chmod +x geesefs

FROM ghcr.io/dani-garcia/vaultwarden:1.35.2-alpine
RUN apk add age envsubst fuse jq openssl sudo
COPY --from=litestream /litestream /usr/bin/litestream
COPY --from=ghcr.io/niklasrosenstein/headscale-fly-io:0.4.3-headscale-0.23.0 /etc/headscale/litestream-entrypoint.sh /
COPY --from=geesefs /geesefs /usr/bin/geesefs
COPY --from=minio/mc:RELEASE.2024-10-02T08-27-28Z /usr/bin/mc /usr/bin/mc
COPY entrypoint.sh .

# NOTE: I tried to make this work, but mounting a directory with GeeseFS and using the --uid option seems to break
#       filesystem permissions for the mounted directory entirely (not just the vaultwarden user, even root gets
#       permission denied errors). So, we run Vaultwarden as root as it can access the GeeseFS mount properly..
#RUN echo 'vaultwarden ALL=(ALL) NOPASSWD:SETENV: /usr/bin/geesefs' >> /etc/sudoers
#RUN addgroup -S vaultwarden && adduser -S vaultwarden -G vaultwarden
#RUN mkdir /mnt/s3 && \
#    touch /etc/litestream.yml && \
#    chown -R vaultwarden:vaultwarden /data /etc/litestream.yml /mnt/s3
#USER vaultwarden

# Make Vaultwarden listen on port 8080.
ENV ROCKET_PORT=8080

ENTRYPOINT [ "/bin/sh", "/entrypoint.sh" ]
