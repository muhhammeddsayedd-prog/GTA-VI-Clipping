#!/bin/sh
# Container entry point: keep yt-dlp and streamlink current (both break when Twitch or YouTube change their
# players), then hand over to the sixstars CLI. tini is PID 1 (see ENTRYPOINT in the Dockerfile).
#
# The upgrade is best effort: with PyPI unreachable, slow or broken, the service starts with the versions
# baked into the image. Set SIXSTARS_SKIP_TOOL_UPGRADE=1 to skip it (one-off commands, offline runs).
set -eu

if [ "${SIXSTARS_SKIP_TOOL_UPGRADE:-0}" != "1" ]; then
  # One retry only: with no network uv gives up in a few seconds instead of half a minute.
  if UV_HTTP_RETRIES="${UV_HTTP_RETRIES:-1}" timeout "${SIXSTARS_TOOL_UPGRADE_TIMEOUT_S:-90}" \
    uv pip install --quiet --python /app/.venv/bin/python --upgrade 'yt-dlp[default]' streamlink; then
    echo '{"event":"tool_upgrade_done","level":"info","service":"entrypoint"}'
  else
    echo '{"event":"tool_upgrade_failed","level":"warning","service":"entrypoint","message":"starting with the installed yt-dlp and streamlink"}' >&2
  fi
fi

exec sixstars "$@"
