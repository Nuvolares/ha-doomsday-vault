ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG KIWIX_TOOLS_VERSION=3.8.2

RUN apk add --no-cache python3 py3-aiohttp py3-yaml poppler-utils curl

# kiwix-serve / kiwix-manage: static musl builds from the Kiwix project.
RUN ARCH="${BUILD_ARCH:-$(uname -m)}" \
    && case "${ARCH}" in \
        amd64|x86_64) KARCH=x86_64 ;; \
        aarch64|arm64) KARCH=aarch64 ;; \
        *) echo "Unsupported architecture: ${ARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_linux-${KARCH}-musl-${KIWIX_TOOLS_VERSION}.tar.gz" \
        | tar -xz --strip-components=1 -C /usr/local/bin \
    && kiwix-serve --version

# Windows runtime for "Export to drive": portable Python + aiohttp/PyYAML wheels + kiwix-serve.exe.
# Copied onto exported drives so the library runs on a Windows PC with nothing installed.
ARG PYTHON_WIN_VERSION=3.12.10
ARG KIWIX_TOOLS_WIN_VERSION=3.8.1
RUN apk add --no-cache --virtual .portable-build py3-pip unzip \
    && mkdir -p /opt/portable/python/Lib/site-packages /opt/portable/kiwix /tmp/wheels \
    && curl -fsSL -o /tmp/python.zip \
        "https://www.python.org/ftp/python/${PYTHON_WIN_VERSION}/python-${PYTHON_WIN_VERSION}-embed-amd64.zip" \
    && unzip -q /tmp/python.zip -d /opt/portable/python \
    && printf 'python312.zip\r\n.\r\nLib\\site-packages\r\n..\\app\r\nimport site\r\n' \
        > /opt/portable/python/python312._pth \
    && pip download --quiet --only-binary=:all: --platform win_amd64 --python-version 3.12 \
        --implementation cp --dest /tmp/wheels aiohttp pyyaml \
    && for wheel in /tmp/wheels/*.whl; do unzip -q -o "$wheel" -d /opt/portable/python/Lib/site-packages; done \
    && curl -fsSL -o /tmp/kiwix-win.zip \
        "https://download.kiwix.org/release/kiwix-tools/kiwix-tools_win-x86_64-${KIWIX_TOOLS_WIN_VERSION}.zip" \
    && unzip -q -j -o /tmp/kiwix-win.zip -d /opt/portable/kiwix \
    && test -f /opt/portable/kiwix/kiwix-serve.exe \
    && rm -rf /tmp/python.zip /tmp/wheels /tmp/kiwix-win.zip \
    && apk del .portable-build

ARG BUILD_VERSION
ENV BUILD_VERSION=${BUILD_VERSION}

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# PDF.js (Apache-2.0) for the in-app PDF viewer, bundled so it works offline.
ARG PDFJS_VERSION=4.10.38
RUN mkdir -p /app/static/vendor \
    && for f in pdf.min.mjs pdf.worker.min.mjs; do \
        curl -fsSL -o "/app/static/vendor/${f}" \
            "https://cdn.jsdelivr.net/npm/pdfjs-dist@${PDFJS_VERSION}/build/${f}" || exit 1; \
    done

# Retro terminal font (SIL Open Font License). The UI falls back to system monospace without it.
RUN mkdir -p /app/static/fonts \
    && (curl -fsSL -o /app/static/fonts/vt323.woff2 \
        "https://cdn.jsdelivr.net/npm/@fontsource/vt323@5.1.0/files/vt323-latin-400-normal.woff2" \
        || echo "VT323 font download failed - using system monospace")

CMD ["/run.sh"]
