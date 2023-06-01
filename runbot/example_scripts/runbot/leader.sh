#!/bin/bash
workdir=/home/$USER/odoo/
exec python3 $workdir/runbot/runbot_builder/leader.py --odoo-path $workdir/odoo -d runbot --logfile $workdir/logs/runbot_leader.txt --forced-host-name=leader
