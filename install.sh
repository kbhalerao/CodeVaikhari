#!/usr/bin/env bash
# Set up the venv, link `say` onto PATH, and (optionally) run the daemon
# under systemd so it is warm from login.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${KOKORO_VENV:-$HOME/.local/share/kokoro-say-venv}"
BIN="$HOME/.local/bin"

echo "==> venv at $VENV"
if [ ! -x "$VENV/bin/python" ]; then
  command -v uv >/dev/null || { echo "need uv: https://docs.astral.sh/uv/"; exit 1; }
  uv venv "$VENV" --python 3.12
  uv pip install --python "$VENV/bin/python" kokoro soundfile numpy
  # misaki's G2P wants this spaCy model; its own downloader shells out to pip,
  # which breaks under a uv shim, so install the wheel directly.
  SV="$("$VENV/bin/python" -c 'import spacy;print(".".join(spacy.__version__.split(".")[:2]))')"
  uv pip install --python "$VENV/bin/python" \
    "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-${SV}.0/en_core_web_sm-${SV}.0-py3-none-any.whl"
else
  echo "    already present, skipping"
fi

echo "==> linking say -> $BIN/say"
mkdir -p "$BIN"
ln -sf "$HERE/say" "$BIN/say"

if [ "${1:-}" = "--systemd" ]; then
  echo "==> installing user service"
  mkdir -p "$HOME/.config/systemd/user"
  sed "s|@HERE@|$HERE|g; s|@VENV@|$VENV|g" "$HERE/kokoro-say.service" \
    > "$HOME/.config/systemd/user/kokoro-say.service"
  systemctl --user daemon-reload
  systemctl --user enable --now kokoro-say.service
  echo "    systemctl --user status kokoro-say"
else
  echo "==> skipping systemd (pass --systemd to keep it warm from login)"
  echo "    without it, the first \`say\` of each boot pays a ~10s model load"
fi

echo
echo "done. try:  say 'hello from kokoro'   then   say --ui"
