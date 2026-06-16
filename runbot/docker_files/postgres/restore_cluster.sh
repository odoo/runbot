curl -sSL "$RESTORE_ZIP_URL" -o /tmp/cluster_backup.zip
echo "Extracting backup into /data/build..."
unzip -q /tmp/cluster_backup.zip -d /data/build/
rm /tmp/cluster_backup.zip
mv /data/build/filestore $(dirname "$ODOO_FILESTORE")
chmod 0700 "$PGDATA"