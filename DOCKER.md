The docker images generated on runbot can be downloaded but will have a user corresponding to the user of the runbot infrastructure

This can be problematic when trying to connect to postgresql using peer authentication


Let say USER in the docker image is 10000 and name is odoo

1 . If you have a local user with the same name and same id, nothing special to do

2. If your user has a different id and name, here is a procedure to follow:

- Create a user with the same uid and name as the one in the docker
    sudo adduser --system --no-create-home odoo --uid 1337

- Grant access to the user to potgresql (this example give admin access, be carefull)
    sudo -u postgres createuser -d -R -S odoo


3. If your user has the same name but different uid, here is a procedure to follow:

- Create a user with the same uid but a different name, let say runbot
    sudo adduser --system --no-create-home runbot --uid 10000

- Grant access to the user to potgresql (this example give admin access, be carefull)
    sudo -u postgres createuser -d -R -S runbot

- edit  /etc/postgresql/14/main/pg_ident.conf and add thoses lines 
    runbot runbot odoo
    runbot odoo odoo

- edit  /etc/postgresql/14/main/pg_hba.conf and change this line 
    local   all             odoo                                     peer map=runbot
before this line
    local   all             all                                     peer


Finally, add the volume when stating the docker 

docker run -ti --rm -v /var/run/postgresql:/var/run/postgresql <IMAGE_TAG>

