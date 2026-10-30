#!/bin/bash
if [ "$PGHOST" = "/var/run/postgresql" ]; then
    echo "Skipping postgres server stop, using /var/run/postgresql socket"
elif [ -n "${PGHOST:-}" ] && [ -n "${PGDATA:-}" ]; then
    echo "Shutting down PostgreSQL safely..."
    /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl stop -D $PGDATA -m fast

    if [ -n "$CLUSTER_ZSTD_NAME" ]; then
        echo "Creating backup cluster"
        BACKUP_DEST="/data/build/logs/$CLUSTER_ZSTD_NAME"
        cd /data/build

        BACKUP_PATHS=("${PGDATA#/data/build/}")
        if [ -d "$ODOO_FILESTORE" ]; then
            BACKUP_PATHS+=("${ODOO_FILESTORE#/data/build/}")
        else
            echo "Warning: Filestore directory not found, skipping in backup."
        fi

        tar -I 'zstd -3' -cf "$BACKUP_DEST" "${BACKUP_PATHS[@]}"

        echo "Backup completed successfully at $BACKUP_DEST!"
    fi
fi
