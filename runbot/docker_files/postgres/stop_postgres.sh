echo "Shutting down PostgreSQL safely..."
/usr/lib/postgresql/$PG_VERSION/bin/pg_ctl stop -D $PGDATA -m fast

if [ -n "$CLUSTER_BACKUP_NAME" ]; then
    echo "Creating backup cluster"
    ZIP_PATH="/data/build/logs/$CLUSTER_BACKUP_NAME"
    
    # Move into the parent directory of PGDATA to zip it as "postgres/"
    cd "$(dirname "$PGDATA")"
    zip -q -r "$ZIP_PATH" "$(basename "$PGDATA")"
    
    # Move into the parent directory of ODOO_FILESTORE to zip it as "filestore/"
    if [ -d "$ODOO_FILESTORE" ]; then
        cd "$(dirname "$ODOO_FILESTORE")"
        zip -q -r "$ZIP_PATH" "$(basename "$ODOO_FILESTORE")"
    else
        echo "Warning: Filestore directory not found, skipping in backup."
    fi
    
    echo "Backup completed successfully at $ZIP_PATH!"
fi