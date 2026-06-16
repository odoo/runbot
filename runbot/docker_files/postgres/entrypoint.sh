#!/bin/bash
set -e

mkdir -p "/data/build/logs"
# Redirect all output to a log file if we are in a container
if [ ! -t 0 ]; then
    exec &>> /data/build/logs/docker.txt
fi

bash /start_postgres.sh

cd /data/build;
if [ -n "$CONTAINER_NAME"]; then
    touch start-$CONTAINER_NAME
fi
echo "Executing test command: $@"
set +e
"$@"
TEST_EXIT_CODE=$?
set -e
if [ -n "$CONTAINER_NAME"]; then
    touch end-$CONTAINER_NAME
fi
bash /stop_postgres.sh

exit $TEST_EXIT_CODE
