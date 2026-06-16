#!/bin/bash
set -e
mkdir -p "/data/build/logs"
. /start_postgres.sh

echo "Executing test command: $@"
set +e
"$@"
TEST_EXIT_CODE=$?
set -e
. /stop_postgres.sh
exit $TEST_EXIT_CODE
