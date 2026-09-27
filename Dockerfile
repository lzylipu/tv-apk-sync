FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates unzip wget \
    && wget -q -O /tmp/platform-tools.zip https://dl.google.com/android/repository/platform-tools-latest-linux.zip \
    && unzip -q /tmp/platform-tools.zip -d /opt \
    && ln -s /opt/platform-tools/adb /usr/local/bin/adb \
    && wget -q -O /tmp/build-tools.zip https://dl.google.com/android/repository/build-tools_r34-linux.zip \
    && unzip -q /tmp/build-tools.zip -d /opt \
    && ln -s /opt/android-14/aapt /usr/local/bin/aapt \
    && rm /tmp/platform-tools.zip /tmp/build-tools.zip \
    && apt-get purge -y unzip wget \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/tv-apk-sync
COPY app.py index.html /opt/tv-apk-sync/

ENV DATA_DIR=/data \
    TZ=Asia/Shanghai

VOLUME ["/data"]
EXPOSE 8080
CMD ["python3", "-u", "/opt/tv-apk-sync/app.py"]
