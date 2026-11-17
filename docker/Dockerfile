# syntax=docker/dockerfile:1.7
# 6 Stars Clips: one image for every service (api, workers, scheduler, live-manager).
# Build from the repository root:  docker build -f docker/Dockerfile -t 6stars-clips:dev .

ARG FFMPEG_VERSION=9.0.2
ARG UV_VERSION=0.13.8

# Static ffmpeg/ffprobe: Debian bookworm ships ffmpeg 5.1 and the project needs >= 6. The build below
# checks that the ass filter (libass) and libx264 are compiled in.
FROM mwader/static-ffmpeg:${FFMPEG_VERSION} AS ffmpeg

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# yt-dlp needs a JavaScript runtime to solve YouTube's signature challenges when cookies are used.
FROM denoland/deno:bin-2.9.8 AS deno

# ── Downloads that need curl/unzip only (kept out of the runtime image) ──────────────────────────────────
FROM debian:bookworm-slim AS fetch
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# TwitchDownloaderCLI (https://github.com/lay295/TwitchDownloader, MIT), self-contained .NET binary.
ARG TWITCH_DOWNLOADER_VERSION=1.56.5
ARG TWITCH_DOWNLOADER_SHA256=f2bcee00d0e6da759d97c23767345634cb9cdb10cdff96cfa96f97983d51bf55
RUN curl -fsSL --retry 5 --retry-all-errors -o /tmp/td.zip \
      "https://github.com/lay295/TwitchDownloader/releases/download/${TWITCH_DOWNLOADER_VERSION}/TwitchDownloaderCLI-${TWITCH_DOWNLOADER_VERSION}-Linux-x64.zip" \
    && echo "${TWITCH_DOWNLOADER_SHA256}  /tmp/td.zip" | sha256sum -c - \
    && mkdir -p /out/bin \
    && unzip -q /tmp/td.zip TwitchDownloaderCLI -d /out/bin \
    && chmod 0755 /out/bin/TwitchDownloaderCLI \
    && rm /tmp/td.zip

# Montserrat static fonts (SIL Open Font License 1.1), pinned to the commit of tag v9.000.
ARG MONTSERRAT_COMMIT=f1f4e9b630b6ac52f1c55c572b29d2a5c3535139
RUN mkdir -p /out/fonts \
    && base="https://raw.githubusercontent.com/JulietaUla/Montserrat/${MONTSERRAT_COMMIT}" \
    && for weight in Regular Medium SemiBold Bold ExtraBold Black; do \
         curl -fsSL --retry 5 --retry-all-errors -o "/out/fonts/Montserrat-${weight}.ttf" \
           "${base}/fonts/ttf/Montserrat-${weight}.ttf"; \
       done \
    && curl -fsSL --retry 5 --retry-all-errors -o /out/fonts/OFL.txt "${base}/OFL.txt"

# PANNs Cnn14 checkpoint and AudioSet label file. panns-inference looks for both in ~/panns_data and would
# otherwise download them with wget when it is imported.
ARG PANNS_CHECKPOINT_SHA256=0dc499e40e9761ef5ea061ffc77697697f277f6a960894903df3ada000e34b31
RUN mkdir -p /out/panns \
    && curl -fsSL --retry 8 --retry-all-errors -o "/out/panns/Cnn14_mAP=0.431.pth" \
      "https://zenodo.org/records/3987831/files/Cnn14_mAP=0.431.pth?download=1" \
    && echo "${PANNS_CHECKPOINT_SHA256}  /out/panns/Cnn14_mAP=0.431.pth" | sha256sum -c - \
    && curl -fsSL --retry 5 --retry-all-errors -o /out/panns/class_labels_indices.csv \
      "http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv" \
    && test "$(wc -l < /out/panns/class_labels_indices.csv)" -eq 528

# ── Runtime image ────────────────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

ENV TZ=UTC \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/sixstars \
    PATH=/app/.venv/bin:$PATH \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    DATA_DIR=/data \
    TWITCH_DOWNLOADER_CLI=/usr/local/bin/TwitchDownloaderCLI \
    HF_HOME=/models/huggingface \
    PANNS_DATA_DIR=/models/panns \
    PANNS_CHECKPOINT_PATH="/models/panns/Cnn14_mAP=0.431.pth"

# libicu72: TwitchDownloaderCLI (.NET globalization). fontconfig: font lookup for libass and drawtext.
# The service user has uid/gid 1000, the same as `deploy` on the servers, so the bind-mounted /data and
# /secrets directories are readable and writable without extra permissions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl fontconfig libicu72 tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 sixstars \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/sixstars --shell /usr/sbin/nologin sixstars \
    && install -d -o sixstars -g sixstars /app /data /models /models/panns /models/huggingface

COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=fetch /out/bin/TwitchDownloaderCLI /usr/local/bin/TwitchDownloaderCLI
COPY --from=fetch /out/fonts/ /usr/share/fonts/truetype/montserrat/
RUN fc-cache -f \
    && fc-list | grep -qi "Montserrat Black" \
    && ffmpeg -hide_banner -filters | grep -Eq ' ass +V->V' \
    && ffmpeg -hide_banner -encoders | grep -q libx264

COPY --from=fetch --chown=sixstars:sixstars /out/panns/ /models/panns/

# Everything below runs as the service user, so the virtualenv stays writable for the start-up upgrade of
# yt-dlp and streamlink without a recursive chown layer.
USER sixstars
WORKDIR /app
RUN ln -s /models/panns /home/sixstars/panns_data

# Dependencies first (cached until pyproject.toml or uv.lock change).
COPY --chown=sixstars:sixstars pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --compile-bytecode

# PyTorch, CPU build. panns-inference and torchlibrosa import torch but do not declare it, so uv.lock has no
# torch at all. Until pyproject.toml declares it with the CPU index (see PROGRESS.md, M11), install it here.
# When the lock file provides torch, this step finds it installed and only runs the checks.
ARG TORCH_VERSION=2.15.0
RUN if python -c "import torch" 2>/dev/null; then \
      echo "torch already provided by uv.lock"; \
    else \
      uv pip install --python /app/.venv/bin/python --compile-bytecode \
        --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_VERSION}+cpu"; \
    fi \
    && python -c "import torch; assert not torch.cuda.is_available(); print('torch', torch.__version__)" \
    && if uv pip list --python /app/.venv/bin/python | grep -i '^nvidia'; then echo "CUDA wheels found" >&2; exit 1; fi

# faster-whisper model, stored in the Hugging Face cache layout so WhisperModel("medium") resolves offline.
ARG WHISPER_MODEL=medium
RUN python -c "import sys; from faster_whisper import download_model; print(download_model(sys.argv[1]))" "${WHISPER_MODEL}"

# From here on the libraries never contact the Hugging Face hub: a model that is not baked in fails fast.
ENV HF_HUB_OFFLINE=1

# Self-check with the offline flag set: both models load from /models.
RUN python -c "import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1], device='cpu', compute_type='int8'); print('whisper ok')" "${WHISPER_MODEL}" \
    && python -c "from panns_inference import AudioTagging; AudioTagging(checkpoint_path=None, device='cpu'); print('panns ok')"

# Application code last. --inexact keeps packages installed above that are not in uv.lock (torch).
COPY --chown=sixstars:sixstars src ./src
COPY --chown=sixstars:sixstars assets ./assets
RUN uv sync --frozen --no-dev --inexact --compile-bytecode \
    && sixstars version

# sed: a Windows checkout with autocrlf must not break the shebang.
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
USER root
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh
USER sixstars

ARG GIT_SHA=unknown
ENV SIXSTARS_GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.title="6stars-clips" \
      org.opencontainers.image.source="https://github.com/muhhammeddsayedd-prog/GTA-VI-Clipping" \
      org.opencontainers.image.revision="${GIT_SHA}"

VOLUME ["/data"]
EXPOSE 8080
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["--help"]
