#!/usr/bin/env bash

usage() {
  echo "Usage: $0 [--verbose|-v] [--help] [key val]"
  echo ""
  echo "Options:"
  echo "  --format, -f   Outputs the information either to JSON or compact"
  echo "  --verbose, -v  Prints all information from the /proc/meminfo (be default only memory, swap, buffers and active parts are collected)."
  echo "  --help         Shows this message"
  echo ""
  echo "The results is in form of JSON dictionary '{...}'. You can pass the results to jq ('$0 | jq' to see pretty print of the results. You can pass the results to jq ('$0 | jq' to see pretty print of the results.."
}

# Defaults
format="compact"
filter="^(Mem\w+|Swap\w+|Buffers\w+|Cached|Active|Committed_AS|Percpu):"

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
    --verbose|-v)
      filter=".*"
      shift
      ;;
    *)
      # The rest will be printed
      break
      ;;
  esac
done

if [[ "$format" = "json" ]]; then
  echo -n "{"
fi

# Join the output
while [[ "$#" -gt 0 ]]; do
  key="$1"
  val="$2"

  if [[ "$format" = "json" ]]; then
    if [[ "$val" =~ ^-?[0-9]+$ ]]; then
      echo -n "\"$key\": $val,"
    else
      echo -n "\"$key\": \"$val\","
    fi
  else
     echo -n "$key:$val#"
  fi

  shift 2
done

# Make a snapshot of the memory
if [[ "$format" = "json" ]]; then
  echo -n $(cat /proc/meminfo | grep -E "$filter" | awk -F: '{print "\"" $1 "\": " $2 ","}' | sed 's/ kB//')
else
  echo -n $(cat /proc/meminfo | grep -E "$filter" | awk -F: '{print $1 ":" $2 "#"}' | sed 's/ kB//') | sed 's/: */:/g;s/# */#/g;s/^ *//;s/ *$//'
fi

# Finish with information about the current timestamp
if [[ "$format" = "json" ]]; then
  echo -n "\"Timestamp\": \"$(date +"%Y-%m-%d-%H-%M-%S")\""
  echo "}"
else
  echo "Timestamp:$(date +"%Y-%m-%d-%H-%M-%S")"
fi
