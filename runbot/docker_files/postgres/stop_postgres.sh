#!/bin/bash
if [ "$PGHOST" = "/var/run/postgresql" ]; then
    echo "Skipping postgres server stop, using /var/run/postgresql socket"
elif [ -n "${PGHOST:-}" ] && [ -n "${PGDATA:-}" ]; then
    echo "Shutting down PostgreSQL safely..."
    /usr/lib/postgresql/$PG_VERSION/bin/pg_ctl stop -D $PGDATA -m fast

    if [ -n "$CLUSTER_BACKUP_NAME" ]; then
        echo "Creating backup cluster"
        ZIP_PATH="/data/build/logs/$CLUSTER_BACKUP_NAME"
        cd /data/build

        zip -q -r "$ZIP_PATH" "${PGDATA#/data/build/}"

        if [ -d "$ODOO_FILESTORE" ]; then
            zip -q -r "$ZIP_PATH" "${ODOO_FILESTORE#/data/build/}"
        else
            echo "Warning: Filestore directory not found, skipping in backup."
        fi

        echo "Backup completed successfully at $ZIP_PATH!"
    fi
fi
