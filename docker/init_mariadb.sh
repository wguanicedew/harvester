#!/bin/bash

mkdir -p /var/log/panda/mariadb
chmod -R 777 /var/log/panda/mariadb


_datadir() {
    /usr/libexec/mysqld --verbose --help --log-bin-index="$(mktemp -u)" 2>/dev/null | awk '$1 == "datadir" { print $2; exit }'
}


init_db() {
    mysql <<-EOSQL
    CREATE DATABASE IF NOT EXISTS ${MARIADB_DATABASE} ;
    CREATE USER '${MARIADB_USER}'@'%' IDENTIFIED BY '${MARIADB_PASSWORD}' ;
    GRANT ALL ON ${MARIADB_DATABASE}.* TO '${MARIADB_USER}'@'%' ;
    FLUSH PRIVILEGES ;
EOSQL
}


DATADIR="$(_datadir)"
if [ ! -d "$DATADIR/mysql" ]; then
    mysql_install_db --user=mysql
    /usr/bin/mysqld_safe --user=atlpan --datadir='$DATADIR' &
    init_db
else
    /usr/bin/mysqld_safe --user=atlpan --datadir='$DATADIR' &
fi
