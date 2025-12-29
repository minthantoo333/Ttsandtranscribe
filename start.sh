#!/usr/bin/env bash
set -euo pipefail

# Do not run apt here — system packages must be installed during image build.
# Start the bot directly.
python bot.py