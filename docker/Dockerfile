# numogram demo: Hermes, ESP-IDF and an emulated ESP32-C3, so you can try the whole
# loop without a board. Build from the repo root:
#   docker build -f docker/Dockerfile -t numogram-demo .
#   docker run -it -v numogram-demo:/opt/data numogram-demo
# The first start installs the plugin into /opt/data and copies the example project
# to /opt/data/projects/sensor-hub. For a real board of another chip, add it to the
# targets, e.g. --build-arg IDF_TARGETS=esp32c3,esp32p4, and set NUMOGRAM_PORT at run time.
FROM nousresearch/hermes-agent:latest
USER root
ARG IDF_VERSION=v5.5.5
ARG IDF_TARGETS=esp32c3
# Build tools for ESP-IDF, plus the shared libraries Espressif's QEMU needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git wget flex bison gperf python3 python3-pip python3-venv cmake ninja-build ccache \
        libffi-dev libssl-dev libusb-1.0-0 ca-certificates \
        libgcrypt20 libglib2.0-0t64 libpixman-1-0 libsdl2-2.0-0 libslirp0 \
    && rm -rf /var/lib/apt/lists/*
ENV IDF_PATH=/opt/esp/idf IDF_TOOLS_PATH=/opt/esp/tools
RUN git clone --depth 1 --branch ${IDF_VERSION} --recursive --shallow-submodules \
        https://github.com/espressif/esp-idf.git ${IDF_PATH} \
    && git config --system --add safe.directory '*'
# Hermes puts its own venv first on PATH. ESP-IDF needs the system Python to make its own.
RUN env -u VIRTUAL_ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        sh -c "${IDF_PATH}/install.sh ${IDF_TARGETS} && python3 ${IDF_PATH}/tools/idf_tools.py install qemu-riscv32" \
    && rm -rf ${IDF_TOOLS_PATH}/dist \
    && chmod -R a+rX /opt/esp
# The plugin is installed from this local git copy on first start, the same way
# `hermes plugins install owner/repo` would fetch it.
# The repo root is the plugin. .dockerignore leaves out .git, so this is a fresh one.
COPY . /opt/numogram/plugin
RUN cd /opt/numogram/plugin \
    && git init -q && git add -A && git -c user.name=numogram -c user.email=numogram@localhost commit -qm demo \
    && chown -R 10000:10000 /opt/numogram/plugin && chmod -R a+rX /opt/numogram
COPY docker/numogram-demo-init /etc/cont-init.d/03-numogram-demo
RUN chmod 755 /etc/cont-init.d/03-numogram-demo
ENV NUMOGRAM_PORT=virtual
