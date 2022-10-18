# Odoo Runbot Repository

This repository contains the source code of Odoo testing bot [runbot.odoo.com](http://runbot.odoo.com/runbot) and related addons.

------------------

## Warnings

**Runbot will delete folders/ drop databases to free some space during usage.** Even if only elements created by runbot are concerned, don't use runbot on a server with sensitive data.

**Runbot changes some default odoo behaviours** Runbot database may work with other modules, but without any guarantee.

**Runbot is not safe by itsefl** This tutorial describes the minimal way to deploy runbot, without too many security considerations. Only trusted code should be executed with this single machine setup. For more security the builder should be deployed separately with minimal access.

## Glossary/models
Runbot use a set of concept in order to cover all the use cases we need

- **Project**: regroups a set of repositories that works together. Usually one project is enough and a default *R&D* project exists.
- **Repository**: A repository name regrouping repo and forks Ex: odoo, enterprise
- **Remote**: A remote for a repository. Example: odoo/odoo, odoo-dev/odoo
- **Build**: A test instance, using a set of commits and parameters to run some code and produce a result.
- **Trigger**: Indicates that a build should be created when a new commit is pushed on a repo. A trigger has both trigger repos, and dependency repo. Ex: new commit on runbot-> build with runbot and a dependency with odoo.
- **Bundle**: A set or branches that work together: all the branches with the same name and all linked pr in the same project.
- **Batch**: A container for builds and commits of a bundle. When a new commit is pushed on a branch, if a trigger exists for the repo of that branch, a new batch is created with this commit. After 60 seconds, if no other commit is added to the batch, a build is created by trigger having a new commit in this batch.

## Processes

Mainly to allow to distribute runbot on multiple machine and avoid cron worker limitations, the runbot is using 2 process besides the main server.

- **runbot process**: the main runbot process, serving the frontend. This is the odoo-bin process.
- **leader process**: this process should only be started once, detect new commits and creates builds for builders.
- **builder process**: this process can run at most once per physical host, will pick unassigned builds and execute them.

## HOW TO

This section give the basic steps to follow to configure the runbot. The configuration may differ from one use to another, this one will describe how to test addons for odoo, needing to fetch odoo core but without testing vanilla odoo. As an example, the runbot odoo addon will be used as a test case. Runbotception. 

### DNS

You may configure a DNS entry for your runbot domain as well as a CNAME for all subdomain.

```
*        IN CNAME  runbot.domain.com.
```
This is mainly usefull to access running build but will also give more freedom for future configurations. 
This is not needed but many features won't work without that.

### nginx

An exemple of config is given in the example_scripts folder.

This may be adapted depending on your setup, mainly for domain names. This can be adapted during the install but serving at least the runbot frontend (proxy pass 80 to 8069) is the minimal config needed.
Note that runbot also has a dynamic nginx config listening on the 8080 port, mainly for running build.

This config is an ir_ui_view (runbot.nginx_config) and can be edited if needed. The config is applied and updated automatically after some time by the builder process.

It is also advised to adapt this config to work in https.

### Requirements

Runbot is an addon for odoo, meaning that both odoo and runbot code are needed to run. Some tips to configure odoo are available in [odoo setup documentation](https://www.odoo.com/documentation/15.0/setup/install.html#setup-install-source) (requirements, postgres, ...) This page will mainly focus on runbot specificities.

You will also need to install docker and other requirements before running runbot.

```bash
sudo apt-get install docker.io python3-unidiff python3-docker python3-matplotlib
```

### Setup

Choose a workspace to clone both repositories and checkout the right branch in both of them.
The directory used in example scripts is `/home/$USER/odoo/` 

Note: It is highly advised to create a user for runbot. This example creates a new user `runbot`

```bash
sudo adduser runbot

# needed access rights, docker, postgress
sudo -u postgres createuser -d runbot
sudo adduser runbot docker
sudo systemctl restart docker

# no sudo power needed for now

su runbot
cd
mkdir odoo
cd odoo
```

You may [add valid ssh key linked to a github account](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account)
 to this user in order to clone the different repositories. You could clone in https but this may be a problem latter to access your ptivate repositories. 
It is important to clone the repo with the runbot user

```bash
git clone --depth=1 --branch=15.0 git@github.com:odoo/odoo.git
git clone git@github.com:odoo/runbot.git

git -C odoo checkout 15.0
git -C runbot checkout 15.0

mkdir logs
```

Note: `--depth=1 --branch=15.0 ` is optionnal but will help to reduce the disc usage for the odoo repo.

Finally, check that you have acess to docker, listing the dockers should work without error (but will be empty).

```bash
docker ps 
```
If it is not working, ensure you have the docker group and logout if needed.

### Install and start runbot

This parts only consist in configuring and starting the 3 services.

Some example scripts are given in `runbot/runbot/example_scripts`

```bash
mkdir ~/bin # if not exist
cp -r ~/odoo/runbot/runbot/example_scripts/runbot ~/bin/runbot
```

Scripts should be adapted, mainly forthe  `--forced-host-name parameter` in builder.sh:

```bash
sed -i "s/runbot.domain.com/runbot.my_real_domain.com/" ~/bin/runbot/builder.sh
```

*The hostname is initally the machine hostname but it should be different per process, having the same hostname for leader and builder is not ideal. This is why the script is using the forced-host-name parameter.*

*The most important one is the builder hostname since it will be used to define running build, zip download and logs urls. We recommand setting your main domain name on this process. The nginx config given in example should be adapted if not.*


Create the corresponding services. You can copy them from the example scripts and adapt them:

```bash
exit # go back to a sudoer user
runbot_user="runbot"
sudo bash -c "cp /home/${runbot_user}/odoo/runbot/runbot/example_scripts/services/* /etc/systemd/system/"
sudo sed -i "s/runbot_user/${runbot_user}/" /etc/systemd/system/runbot.service
sudo sed -i "s/runbot_user/${runbot_user}/" /etc/systemd/system/leader.service
sudo sed -i "s/runbot_user/${runbot_user}/" /etc/systemd/system/builder.service
```

Enable all services and start runbot frontend

```bash
sudo systemctl enable runbot
sudo systemctl enable leader
sudo systemctl enable builder
sudo systemctl daemon-reload
sudo systemctl start runbot
sudo systemctl status runbot
```

Runbot service should be running

You can now connect to your backend and preconfigure runbot. 
- Install runbot module, if it wasn't done before.
- Navigate to `/web` to leave the website configurator.
- Connect as admin (default password: admin).

Check odoo documentation for other needed security configuration (master password). This is mainly needed for production purpose.
You can check that in the `/web/database/manager` page. [More info here](https://www.odoo.com/documentation/15.0/administration/install/deploy.html#security)
Change your admin user login and password
You may want to check the runbot settings (`Runbot > Setting > setting`):
- Default number of workers should be the max number of parallel build, consider having max `#cpu - 1`
- Modify `Default odoorc for builds` to change the running build master password to something unique ([idealy a hashed one](https://github.com/odoo/odoo/blob/15.0/odoo/tools/config.py#L722)).
- Tweak the garbage collection settings if you have limited disk space
- The `number of running build` is the number of parallel running builds.
- `Max commit age (in days)` will limt the max age of commit to detect. Increase this limit to detect older branches.

Finally, start the two other services

```bash
systemctl start leader
systemctl start builder
```

Several log files should have been created in `/home/runbot/odoo/logs/`, one per service.

#### Bootstrap
Once launched, the leader process should start to do basic work and bootstrap will start to setup some directories in static.

```bash
su runbot
ls ~/odoo/runbot/runbot/static
```

>build  docker  nginx  repo  sources  src

- **repo** contains the bare repositories
- **sources** contains the exported sources needed for each build
- **build** contains the different workspaces for dockers, containing logs/ filestore, ...
- **docker** contains DockerFile and docker build logs
- **nginx** contains the nginx config used to access running instances
All of them are empty for now.

A database defined by *runbot.runbot_db_template* icp will be created. By default, runbot use template0. This database will be used as a template for testing builds. You can change this database for more customisation.

Other cron operations are still disabled for now.

#### DOCKER images
A default docker image is present in the database and should automatically be build (this may take some time, check builder logs). 
Depending on your version it may not be enough.
You can modify it to fit your needs or ask us for the latest version of the Dockerfile waiting for an official link.

#### Add remotes and repositories
Access runbot app and go to the `Runbot>Setting>Repositories` menu

Create a new repo for odoo
![Odoo repo configuration](runbot/documentation/images/repo_odoo.png "Odoo repo configuration")

- **Name**: `odoo` It will be used as the directory name to export the sources
- **Identityfile** is only usefull if you want to use another ssh key to access a repo
- **Project**: `R&D` by default.
- **Modules to install**: `-*` in order to remove them from the default `-i`. This will speed up installation. To install and test all modules, leave this space empty or use `*`. Some modules may be blacklisted individually, by using `*-module,-other_module, l10n_*`.
- **Server files**: `odoo-bin` will allow runbot to know the possible file to use to launch odoo. odoo-bin is the one to use for the last version, but you may want to add other server files for older versions (comma separated list). The same logic is used for manifest files.
- **Manifest files**: `__manifest__.py`. This field is only usefull to configure old versions of odoo.  
- **Addons path**: `addons,odoo/addons`. The paths where addons are stored in this repository.
- **Mode**: `poll` since github won't hook your runbot instance. Poll mode is limited to one update every 5 minutes. *It is advised to set it in hook mode later and hook it manually of from a cron or automated action to have more control*.
- **Remotes**: `git@github.com:odoo/odoo.git` A single remote is added, the base odoo repo. Only branches will be fetched to limit disk usage and branches will be created in the backend. It is possible to add multiple remotes for forks.

Create another project for your repositories `Runbot>Setting>Project`

This is optionnal you could use the R&D one, but this may be more noisy since every update in odoo/odoo will be displayed on the same page as your own repo one. Splitting by project also allows to manage access rights. 

Create a repo for your custom addons repo
![Odoo repo configuration](runbot/documentation/images/repo_runbot.png "Odoo repo configuration")
- **Name**: `runbot`
- **Project**: `runbot`.
- **Modules to install**: `-*,runbot` ton only install the runbot module.
- No addons_path given to use repo root as default.
- (optionnal) For your custom repo, it is advised to configure the repo in `hook` mode if possible, adding a webhook on `/runbot/hook`. Use `/runbot/hook/<repo_id>` to do it manually.
- **Remotes**: `git@github.com:odoo/runbot.git` 
- The remote *PR* option can be checked if needed to fetch pull request too . Will only work if a github token is given for this repo.

A config file with your remotes should be created for each repo. You can check the content in `/runbot/static/repo/(runbot|odoo)/config`. The repo will be fetched, this operation may take some time too. After that, you should start seeing empty batches in both projects on the frontend (`/` or `/runbot`)

#### Triggers and config
At this point, runbot will discover new branches, new commits, create bundle, but no build will be created.

When a new commit is discovered, the branch is updated with a new commit. Then this commit is added in a batch, a container for new builds when they arrive, but only if a trigger corresponding to this repo exists. After one minute without a new commit update in the batch, the different triggers will create one build each.
In this example, we want to create a new build when a new commit is pushed on runbot, and this build needs a commit in odoo as a dependency.

By default the basic config will use the step `all` to test all addons. The installed addons will depends on the repo configuration, but all dependencies tests will be executed too.
This may not be wanted because some `base` or `web` test may be broken. This is the case with runbot addons. Also, selecting only the test for the addons
we are interested in will speedup the build a lot.

Even if it would be better to create new Config and steps, we will modify the curent `all` config step.

`Runbot > Configs > Build Config Steps`

Edit the `all` config step and set `/runbot` as **Test tags**

We can also check the config were going to use:

`Runbot > Configs > Build Config`

Optionnaly, edit `Default no run` config and remove the `base` step. It will only test the module base.

Config and steps can be usefull to create custom test behaviour but this is out of the scope of this tutorial.

Create a new trigger like this:

`Runbot>Triggers`

- *Name*: `Runbot` Just for display 
- *Project id*: `runbot` This is important since you can only chose repo triggering a new build in this project.
- *Triggers*: `runbot` A new build will be created int the project when pushing on this repo.
- *Dependencies*: `odoo` Runbot needs odoo to run
- *Config*: `Default no run` Will start a build but dont make it running at the end. You can still wake up a build.

When a branch is pushed, a new batch will be created, and after one minute the new build will be created if no other change is detected.

CI options will only be used to send status on remotes of trigger repositories having a valid token.

You can either push, or go on the frontend bundle page and use the `Force new batch` button (refresh icon) to test this new trigger.

#### Bundles

Bundles can be marked as `no_build`, so that new commit won't create batch creation and the bundle won't be displayed on the main page.

#### Hosts
Runbot is able to share pending builds across multiple hosts. In the present case, there is only one. A new host will never assign a pending build to himself by default.
Go in the Build Hosts menu and choose yours. Uncheck *Only accept assigned build*. You can also tweak the number of parallel builds for this host.

### Modules filters
Modules to install can be filtered by repo, and by config step. The first filter to be applied is the repo one, creating the default list for a config step.
Addon -module on a repo will remove the module from the default, it is advised to reflect the default case on repo. To test only a custom module, adding `-*` on odoo repo will disable all odoo addons. Only dependencies of custom modules will be installed. Some specific modules can also be filtered using `-module1,-module1` or somme specific modules can be kept using `-*,module1,module2`.
Module can also be filtered on a config step with the same logic as repo filter, except that repo's blacklist can be disabled to allow all modules by starting the list with `*` (all available modules)
It is also possible to add test-tags to config step to allow more module to be installed but only testing some specific one. Test tags: `/module1,/module2`

### db template
Db creation will use template0 by default. It is possible to specify a specific template to use in runbot config *Postgresql template*. It is mainly used to add extensions. This will also avoid having issue if template0 is used when creating a new database.

It is recommended to generate a `template_runbot`  database based on template0 and set this value in the runbot settings

```
createdb template_runbot -T template0
```

## Dockerfiles

Runbot is using a Dockerfile Odoo model to define the Dockerfile used for builds and is shipped with a default one. This default Dockerfile is based on Ubuntu Bionic and is intended to build recent supported versions of Odoo.

The model is using Odoo QWeb views as templates.

A new Dockerfile can be created as needed either by duplicating the default one and adapt parameters in the view. e.g.: changing the key `'from': 'ubuntu:bionic'` to `'from': 'debian:buster'` will create a new Dockerfile based on Debian instead of ubuntu.
Or by providing a plain Dockerfile in the template.

Once the Dockerfile is created and the `to_build` field is checked, the Dockerfile will be built (pay attention that no other operations will occur during the build).

A version or a bundle can be assigned a specific Dockerfile.
