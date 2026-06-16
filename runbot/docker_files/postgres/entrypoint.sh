#!/bin/bash
set -e
mkdir -p "/data/build/logs"
bash /start_postgres.sh

echo "Executing test command: $@"
set +e
"$@"
TEST_EXIT_CODE=$?
set -e
bash /stop_postgres.sh
exit $TEST_EXIT_CODE
