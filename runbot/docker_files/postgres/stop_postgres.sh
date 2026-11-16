#!/bin/bash
#
# We expect the PG cluster has no active client connections at this point.

if [ "$PGHOST" = "/var/run/postgresql" ]; then
    echo "Skipping postgres server stop, using /var/run/postgresql socket"
elif [ -n "${PGHOST:-}" ] && [ -n "${PGDATA:-}" ]; then
    PG_BIN="/usr/lib/postgresql/$PG_VERSION/bin"
    if [ -n "$CLUSTER_ZSTD_NAME" ]; then
        echo "Compacting PostgreSQL databases before backup..."
        "$PG_BIN/vacuumdb" -U postgres -h "$PGHOST" --all --full --analyze --quiet || echo "Warning: vacuumdb failed, continuing backup."
    fi
    echo "Shutting down PostgreSQL safely..."
    /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl stop -D $PGDATA -m fast

    if [ -n "$CLUSTER_ZSTD_NAME" ]; then
        echo "Trimming PostgreSQL WAL and transient state..."
        "$PG_BIN/pg_resetwal" -D "$PGDATA" || echo "Warning: pg_resetwal failed, archiving untrimmed WAL."

        echo "Creating backup cluster"
        BACKUP_DEST="/data/build/logs/$CLUSTER_ZSTD_NAME"
        cd /data/build

        BACKUP_PATHS=("${PGDATA#/data/build/}")
        if [ -d "$ODOO_FILESTORE" ]; then
            BACKUP_PATHS+=("${ODOO_FILESTORE#/data/build/}")
        else
            echo "Warning: Filestore directory not found, skipping in backup."
        fi

        # zstd accepts multiple values for the same option definition, last declaration wins.
        tar -I "zstd -T1 -3 ${ZSTD_ARGS:-}" -cf "$BACKUP_DEST" "${BACKUP_PATHS[@]}"

        echo "Backup completed successfully at $BACKUP_DEST!"
    fi
fi
