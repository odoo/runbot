#!/bin/bash
workdir=/home/$USER/odoo
exec python3 $workdir/odoo/odoo-bin --workers=2 --without-demo=1 --max-cron-thread=1 --addons-path $workdir/odoo/addons,$workdir/runbot -d runbot --logfile $workdir/logs/runbot.txt
