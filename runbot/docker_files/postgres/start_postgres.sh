#!/bin/bash
if pg_isready -h /var/run/postgresql > /dev/null 2>&1; then
    echo "Skipping postgres server start, using /var/run/postgresql socket"
    PGHOST=/var/run/postgresql
    export PGHOST
elif [ -n "${PGHOST:-}" ] && [ -n "${PGDATA:-}" ]; then
    mkdir -p "$PGDATA" "$PGHOST" "$ODOO_FILESTORE"
    mkdir -p /data/build/logs/
    NEEDS_INIT=false

    if [ -z "$(ls -A $PGDATA)" ]; then
        if [ -n "$RESTORE_CLUSTER_ZIP_URL" ]; then
            echo "Found RESTORE_CLUSTER_ZIP_URL. Downloading cluster backup..."
            curl -sSL "$RESTORE_CLUSTER_ZIP_URL" -o /tmp/cluster_backup.zip
            echo "Extracting backup into /data/build..."
            unzip -q /tmp/cluster_backup.zip -d /data/build
            rm /tmp/cluster_backup.zip
            chmod 0700 "$PGDATA"
        else
            echo "Initializing fresh PostgreSQL database..."
            /usr/lib/postgresql/$PG_VERSION/bin/initdb -U postgres --auth-local=trust --lc-collate=C --lc-ctype=en_US.UTF8 -D $PGDATA
            NEEDS_INIT=true
        fi
    fi

    echo "Starting PostgreSQL in the background..."
    DEFAULT_PG_ARGS=(
        "-k $PGHOST"
        "-c fsync=off"
        "-c synchronous_commit=off"
        "-c full_page_writes=off"
        "-c shared_buffers=128MB"
        "-c work_mem=64MB"
        #"-c maintenance_work_mem=256MB"
        "-c autovacuum=off"
        "-c jit=off"
    )
    /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl start -D $PGDATA -l /data/build/logs/postgres.txt -o "${DEFAULT_PG_ARGS[*]} $PG_ARGS"

    TIMEOUT=30  # seconds
    echo "Waiting for PostgreSQL to be ready ..."
    SECONDS=0
    until /usr/lib/postgresql/$PG_VERSION/bin/pg_isready -h $PGHOST > /dev/null 2>&1; do
        if (( SECONDS >= TIMEOUT )); then
            echo "Timed out after ${TIMEOUT}s waiting for PostgreSQL" >&2
            exit 1
        fi
        sleep 0.1
    done
    echo "PostgreSQL ready"

    if [ "$NEEDS_INIT" = true ]; then
        echo "Creating non-superuser role $(whoami)..."
        psql -U postgres -h $PGHOST -c "CREATE USER $(whoami) WITH NOSUPERUSER CREATEDB;"
        psql -U postgres -h $PGHOST -c "CREATE DATABASE $(whoami) OWNER $(whoami);"

        echo "Setting up template_runbot database..."
        createdb -U postgres -h $PGHOST -O $(whoami) template_runbot

        echo "Configuring template_runbot and installing extensions..."
        psql -U postgres -h $PGHOST -d template_runbot -f /db_template.sql

        echo "Marking template_runbot as a template..."
        psql -U postgres -h $PGHOST -c "UPDATE pg_database SET datistemplate = true WHERE datname = 'template_runbot';"
    fi
fi
