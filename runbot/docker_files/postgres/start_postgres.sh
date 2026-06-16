mkdir -p "$PGDATA" "$PGHOST" "$ODOO_FILESTORE"
mkdir -p /data/build/logs/
NEEDS_INIT=false

if [ -n "$REPLACE_PG_DATA" ]; then
    echo "Force removing exising cluster"
   rm -rf $PGDATA
fi

if [ -z "$(ls -A $PGDATA)" ]; then
    if [ -n "$RESTORE_ZIP_URL" ]; then
        echo "Found RESTORE_ZIP_URL. Downloading cluster backup..."
        . /restore_cluster.sh $RESTORE_ZIP_URL
    else
        echo "Initializing fresh PostgreSQL database..."
        /usr/lib/postgresql/$PG_VERSION/bin/initdb -U postgres --auth-local=trust --lc-collate=C --lc-ctype=en_US.UTF8 -D $PGDATA
        NEEDS_INIT=true
    fi
fi

echo "Starting PostgreSQL in the background..."
PG_OPTS=(
    "-k $PGHOST"
    "-c fsync=off"
    "-c synchronous_commit=off"
    "-c full_page_writes=off"
    "-c shared_buffers=512"
    "-c work_mem=64MB"
    #"-c maintenance_work_mem=1GB"
    "-c autovacuum=off"
)
/usr/lib/postgresql/$PG_VERSION/bin/pg_ctl start -D $PGDATA -l /data/build/logs/postgres.log -o "${PG_OPTS[*]}"

echo "Waiting for PostgreSQL to be ready..."
until /usr/lib/postgresql/$PG_VERSION/bin/pg_isready -h $PGHOST > /dev/null 2>&1; do
  sleep 1
done
echo "PostgreSQL is ready!"

if [ "$NEEDS_INIT" = true ]; then
    echo "Creating non-superuser role $USER..."
    psql -U postgres -h $PGHOST -c "CREATE USER $USER WITH NOSUPERUSER CREATEDB;"
    psql -U postgres -h $PGHOST -c "CREATE DATABASE $USER OWNER $USER;"

    echo "Setting up template_runbot database..."
    createdb -U postgres -h $PGHOST -O $USER template_runbot

    echo "Configuring template_runbot and installing extensions..."
    psql -U postgres -h $PGHOST -d template_runbot -f /db_template.sql

    echo "Marking template_runbot as a template..."
    psql -U postgres -h $PGHOST -c "UPDATE pg_database SET datistemplate = true WHERE datname = 'template_runbot';"
fi


