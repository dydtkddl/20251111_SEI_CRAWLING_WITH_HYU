#!/usr/bin/env bash
# watch.sh: Tail the last 30 lines of a file every second.
#
# Usage: ./watch.sh <file_to_tail>
#
# Example:
#   ./watch.sh simulation.input.out

if [ $# -lt 1 ]; then
  echo "Usage: $0 <file_to_tail>"
  exit 1
fi

watch -n 1 "tail -n 30 $1"
