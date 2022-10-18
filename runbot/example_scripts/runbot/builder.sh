#!/bin/bash
workdir=/home/$USER/odoo
exec python3 $workdir/runbot/runbot_builder/builder.py --odoo-path $workdir/odoo -d runbot --logfile $workdir/logs/runbot_builder.txt --forced-host-name runbot.domain.com
