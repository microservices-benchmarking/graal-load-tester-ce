#!/usr/bin/env bash
set -e

usage() {
  echo "Usage: $0 [--help] [key val]"
  echo ""
  echo "Options:"
  echo "  --format, -f   Outputs the information either to JSON or compact"
  echo "  --help         Shows this message"
  echo ""
  echo "The results is in form of stream of JSON dictionaries '{...}' delimited by ','. You can pass the results to jq ('$0 | jq' to see pretty print of the results. You can pass the results to jq ('$0 | jq' to see pretty print of the results."
}

format="compact"

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --help)
      usage
      exit 0
      ;;
    --format|-f)
      format="$2"
      shift 2
      ;;
    *)
      # The rest will be printed
      break
      ;;
  esac
  shift
done


ppid="$1"
shift

# Get functions recursively
get_childs() {
  parent=$1
  ps --ppid "$parent" -o pid= | while read pid; do
    echo $pid
    get_childs $pid
  done
}

params_to_fields() {
  while [[ "$#" -gt 0 ]]; do
    key="$1"
    val="$2"

    if [[ "$val" =~ ^-?[0-9]+$ ]]; then
      echo -n "$key:$val#"
    else
      echo -n "$key:$val#"
    fi

    shift 2
  done
}

time="Timestamp:$(date +"%Y-%m-%d-%H-%M-%S")"
fields=$(params_to_fields $@)

pids=$(echo $ppid $(get_childs $ppid))
for pid in $pids; do
  if [ -e /proc/$pid/smaps ]; then
    [ "$ppid" != "$pid" ] && echo -n "#,#"
    echo -n $(grep -E '^\s*(Name)' /proc/$pid/status | sed -E 's/^\s*([^:]+):\s*(.*)$/\1:\2#/')
    echo -n "Pid:$pid#"
    echo -n "$(sed '1d;s/ kB/#/g; s/ //g' /proc/$pid/smaps_rollup | xargs)"
    echo -n "PidGroup:$ppid#"
    echo -n $fields
    echo -n $time
  fi
done
echo ""
