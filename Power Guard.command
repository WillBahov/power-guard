#!/bin/sh
# Power Guard - double-click to start the CPU guard in Terminal.
# Runs the self-calibrating guard with the live dashboard. Ctrl-C (or 'q') stops it.
DIR="$(dirname "$0")"
exec python3 "$DIR/power_guard.py" guard --auto --budget-cores 8
