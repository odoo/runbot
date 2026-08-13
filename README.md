# Odoo Runbot Repository

This repository contains the source code of Odoo testing bot [runbot.odoo.com](http://runbot.odoo.com/runbot) and related addons.

------------------

## Warnings

**Runbot will delete folders/drop databases to free some space during usage.** Even if only elements created by runbot are concerned, don't use runbot on a server with sensitive data.

**Runbot changes some default odoo behaviours** Runbot database may work with other modules, but without any guarantee.

**Runbot is not safe by itself** This tutorial describes the minimal way to deploy runbot, without security considerations. Only trusted code should be executed with this single machine setup. For more security the builder should be deployed separately with minimal access.

## Glossary/models
Runbot encodes concepts that cover all the testing and validation use-cases for maintaining Odoo projects:

- **Project**: Logical grouping of repositories that are related to each other. Usually one project is enough and a default *R&D* project is automatically created.
- **Repository**: Logical grouping of remotes. Usually you create *odoo* and *enterprise*.
- **Remote**: A location to find (Git) repository information. Example: odoo/odoo, odoo-dev/odoo
- **Bundle**: A group of matched[^branch-matching] branches across repositories eg, odoo/odoo/19.0 matches odoo/enterprise/19.0. Usually you see one bundle for every discovered branch (including PRs).
- **Batch**: A group of commits, for all branches defined by the parent bundle. These commits build together.[^batches]
- **Trigger**: Logic to automate creation of build instances. At a minimum you need one trigger per project to build new code eg, a new commit on odoo/odoo -[automatic batch creation]-> new batch -[trigger]-> new build to run odoo tests.
- **Build**: Represents the execution of odoo, in practice this is when testing happens (`odoo-bin --tests-enabled <..>` is launched).[^build-start-delay] Builds generally execute code and produce output (logs, build artifacts, running odoo instance for testing).


[^branch-matching]: Matching only links related repositories within the same project.  
[^batches]: Batches are created automatically by the parent bundle after detection of new commits.  
[^build-start-delay]: By default, build creation is delayed until 60 seconds (debounce) after the most recent commit on the linked batch. This debounce value is part of the project configuration.  

## Processes

Mainly to allow to distribute runbot on multiple machine and avoid cron worker limitations, the runbot is using 2 processes besides the main server.

- **runbot web process**: The main runbot process serving web requests. A typical Odoo instance (odoo-bin process).
- **leader process**: This process should only be started once, detect new commits and creates builds for builders.
- **builder process**: This process can run at most once per physical host, will execute the builds assigned to themselves.

## Operational requirements

You can safely skip ahead to [First steps with Runbot](#first-steps-with-runbot) if you are interested in trying out Runbot.  
This section lists Runbot's expectations for the platform it's running on and configuration examples.

### DNS

You may configure a DNS entry for your runbot domain as well as a CNAME for all subdomain.

```
;; The documentation writer assumes the reader knows how to interpret this fragment of code.
;; The reader can also skip DNS configuration to use Runbot with a limited feature set.

$ORIGIN domain.com.
runbot.domain.com.  IN A      127.0.0.1
*                   IN CNAME  runbot.domain.com.
```

This configuration is not necessary for a minimal setup.  
You need similar domain setup to deploy Runbot in a production environment that gives access to running test database and test artifacts.

### nginx

An example of config is given in the `example_scripts` folder.

This may be adapted depending on your setup, mainly for domain names. This can be adapted during the install but serving at least the runbot frontend (proxy pass `80` to `8069`) is the minimal config needed.
Note that runbot also has a dynamic nginx config listening on the `8080` port, mainly for running build.

This config is an `ir_ui_view` (runbot.nginx_config) and can be edited if needed. The config is applied and updated automatically after some time by the builder process.

It is also advised to adapt this config to work in `https`.

### Running unattended

The directory [./runbot/runbot/example_scripts]() has example configuration to launch every Runbot process. This section explains how to configure Systemd to run Runbot unattended.

NOTE: This part assumes a dedicated user 'runbot' exists for running Runbot and accessing docker and postgresql.

Copy the runbot launch scripts to a known static location on the system.  
If you want to store your launch scripts elsewhere, update the `ExecStart` parameter inside the systemd service unit files to match.

```bash
workspace="/home/runbot/odoo/"

su runbot
mkdir ~/bin
cp -r "${workspace}/runbot/runbot/example_scripts/runbot" ~/bin/runbot
```

Create the corresponding services. You can copy them from the example scripts and adapt them:

```bash
exit # go back to a sudoer user

runbot_user="runbot"
sudo cp "${workspace}/runbot/runbot/example_scripts/services/*" /etc/systemd/system/
sudo sed -i "s/runbot_user/${runbot_user}/" /etc/systemd/system/runbot.service
sudo sed -i "s/runbot_user/${runbot_user}/" /etc/systemd/system/leader.service
sudo sed -i "s/runbot_user/${runbot_user}/" /etc/systemd/system/builder.service
```

Enable all services and start the runbot frontend.

```bash
sudo systemctl daemon-reload

sudo systemctl enable runbot
sudo systemctl enable leader
sudo systemctl enable builder

sudo systemctl start runbot
sudo systemctl status runbot
```

Ensure startup completed succesfully, then start and verify the other runbot processes too.  
Several log files should have been created in `/home/runbot/odoo/logs/`, one per service.

## User guide

Follow along to get a new Runbot instance configured that tests code from this (Runbot) repository. Runbotception!  
Note that Runbot runs on top of Odoo community and we'll not be testing Odoo community itself.

You most likely want to divert from these instructions to better support your own use case.

### Requirements

Runbot is an addon for odoo, meaning that both odoo and runbot repositories are needed to launch Runbot. The information below guides you through installation of git, python+dependencies, postgresql as those are Odoo dependencies.

1. [Prepare - Odoo source install](https://www.odoo.com/documentation/19.0/administration/on_premise/source.html#prepare)
2. [Run the server - Odoo setup guide](https://www.odoo.com/documentation/19.0/developer/tutorials/setup_guide.html#run-the-server)


Install Runbot dependencies.

```bash
sudo apt-get install docker.io python3-unidiff python3-docker python3-matplotlib
```

Choose a workspace to clone both repositories and checkout the default branch in both of them.
The directory used in example scripts is `/home/$USER/odoo/` 

Note: It is highly advised to create a dedicated user for runbot.

```bash
# This example creates a new unix user `runbot` with permissions to use docker and postgresql.

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

You may [add a valid ssh key to a github account](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account)
 to be used by user `runbot` to clone the next repositories. You could clone in `https` but this may be a problem later to access your private repositories.  
It is important to clone the repositories as the runbot user:

```bash
git clone --depth=1 --branch=19.0 git@github.com:odoo/odoo.git
git clone git@github.com:odoo/runbot.git

git -C odoo checkout 19.0
git -C runbot checkout 19.0

mkdir logs
```

Note: `--depth=1 --branch=19.0 ` is optional but this approach requires less available disk space. The odoo community and odoo enterprises repositories are large, monitor your free disk space regularly when you use them inside Runbot. (50+ GiB at minimum)

Finally, check that you have access to docker, listing the docker containers should work without error (but the list will be empty).

```bash
docker ps 
```
If the command returns an error, add the unix user to the 'docker' group and logout/login again.


#### Install runbot database

Initialise a new Odoo database with Runbot automatically installed.

```bash
workspace="/home/$USER/odoo/"

"$workspace/odoo/odoo-bin" --addons-path "$workspace/odoo/addons,$workspace/runbot" -d runbot -i runbot --stop-after-init
```

This is all the preparation necessary to start every runbot process. Usually you'll start each of them in the proper order.

### Runbot process

All requirements are met, go ahead and launch!

```bash
# Launch the Runbot web service
"$workspace/runbot/runbot/example_scripts/runbot/runbot.sh"
```

You can now connect to your running instance and configure runbot.
- Page [http://127.0.0.1:8069]() shows an empty bundle overview page. Nothing is configured yet.
- Log into the backend as admin (default password: admin).
- Visit page [Runbot > Settings > Settings](http://127.0.0.1:8069/odoo/settings) to update your instance settings:
    - `Default number of workers` equals the maximum number of builds to run in parallel, consider setting the value to `#cpu - 1`.
    - Modify `Default odoorc for builds` to change the running build master password to something unique ([ideally a hashed one](https://github.com/odoo/odoo/blob/master/odoo/tools/config.py#L1148)).
    - Tweak the garbage collection settings, if you have limited disk space.
    - `Max running builds` equals the maximum number of builds that remain externally accessible in parallel. These are the odoo-instances intended for manual intergration testing.
    - `Max commit age (in days)` ensures new commits are created recently. Increase this limit in exceptional cases to detect older branches.

If you intend to run this Runbot instance in a production environment, read through the Odoo documentation to secure it properly.  
- Update the instance master password, which is used at the `/web/database/manager` endpoint. ([More info here](https://www.odoo.com/documentation/19.0/administration/on_premise/deploy.html#reset-the-master-password))
- Change the login credentials of the admin user

### Leader process

All requirements are met, go ahead and launch!

```bash
# Launch the Runbot leader process
"$workspace/runbot/runbot/example_scripts/runbot/leader.sh"
```

Right away the leader process will not do anything, an instance admin must assign the leader role to this process first.
- Open the backend, navigate to Runbot > Hosts
- Open the record with hostname 'leader' (configurable name, see 'forced-host-name' parameter)
- Enable options `Is leader` and `Is assigner`, disable `Is builder`, then save

Observe the leader process stopped writing "..is not a leader host.." to the console.


### Builder process

The launch script `builder.sh` should be adapted, specifically the `forced-host-name` parameter value:

```bash
sed -i "s/runbot.domain.com/runbot.my_real_domain.com/" ~/bin/runbot/builder.sh
```

*The host name equals the machine hostname and cannot be changed from the backend. The host name should be different per process, having the same host name for leader and builder is not ideal. The forced-host-name parameter is available to manually set a custom host name when launching the process.*

Note: The host name of the builder process must match your DNS configuration (resolvable domain name). Runbot uses the host name to construct URLs for running builds (live test-instances), artifact downloads and log files. This value should be a fully qualified domain name that includes your real domain.  
The example nginx configuration file demonstrates how to accept and proxy incoming connections on the builder process host.

#### Docker images
Builder processes run builds inside docker containers. These containers are instantiations from docker images defined inside Runbot model 'Docker file', a common docker file is provisioned by default. Builders will fetch (see Remote Registries) or build (see [Dockerfiles](#dockerfiles)) the images for every docker file automatically.

#### Local data

The processes attempt to cache as much data as possible to reduce total wait time until builds finish. This data is stored inside the directory 'static'.

```bash
su runbot

ls ~/odoo/runbot/runbot/static
> build  docker  nginx  repo  sources  src

```

- **repo** contains bare git repositories for each remote
- **sources** contains the exported sources needed for each build
- **build** contains the run-environments (r/w volume mount into docker container) for builds, containing logs/ filestore, ...
- **docker** contains DockerFile and docker build logs
- **nginx** contains the nginx config used to access running instances

All of the above directories are empty right now, we haven't configured Runbot to build anything.

Runbot also supports picking specific database templates to be initialised before build launch. Runbot uses 'template0' by default, but can be overwritten through `Runbot > Settings > Settings` or instance parameter *runbot.runbot_db_template*.

Other cron operations are still disabled for now.


## Test Runbot code
All the Runbot processes are running and provisioned with basic configuration. Now it's time to perform some actual continuous integration (CI)!

### Repositories and remotes
Runbot needs code to build. It can retrieve code automatically through git repository synchronisation.

Open the Runbot backend and open `Runbot > Setting > Repositories`.

Create a new repository to represent the odoo git repository
![Odoo repo configuration](runbot/documentation/images/repo_odoo.png "Odoo repo configuration")

- **Name**: `odoo`, name of the target directory when source code is exported.
- **Identity File** `<empty>` useful if you want to use a specific ssh key to access the repo.
- **Project**: `R&D` by default.
- **Modules to install**: `-*` to remove all from the default value for `-i`([odoo-bin cli](https://www.odoo.com/documentation/19.0/developer/reference/cli.html#cmdoption-odoo-bin-i)). This will speed up installation. [^modules-to-install]
- **Server files**: `odoo-bin`, will instruct runbot how to launch odoo. [^bin-files]
- **Manifest files**: `__manifest__.py`. This field is only useful to configure old versions of odoo.  
- **Addons path**: `addons,odoo/addons`. The paths where addons are stored in this repository.
- **Mode**: `poll`, since integrating github hooks is out of scope for this guide.  
    Poll mode is limited to one update every 5 minutes.
- **Remotes**: `git@github.com:odoo/odoo.git`, the odoo community git repository.[^remotes]  
    WARNING: Runbot will retrieve _all_ the repository data automatically! Downloading the entire odoo repository will take a long time.

[^modules-to-install]: To install and test all modules, leave this space empty or use `*`. Some modules may be blacklisted individually, by using `*-module,-other_module, l10n_*`.
[^bin-files]: `odoo-bin` is the entrypoint to use since Odoo v11, but you may want to add other server files for older versions (comma separated list). All the entrypoint names are tried in definition order.
[^remotes]: You can add more remotes to logically combine the work saved on (private) forks of the project, or to simply have redundancy.


Create a new repository to represent the runbot git repository
![Odoo repo configuration](runbot/documentation/images/repo_runbot.png "Odoo repo configuration")
- **Name**: `runbot`
- **Project**: `R&D`.
- **Modules to install**: `-*,runbot`, only install the runbot module (and module dependencies of course).
- **Addons path**: `\<empty>`, without addons path Runbot uses the repository root to find modules.
- **Mode**: `poll`, since integrating github hooks is out of scope for this guide.
- **Remotes**: `git@github.com:odoo/runbot.git` 
- The remote *PR* option can be checked if needed to fetch pull request too. Will work only if a github token is given for this repo.

If you link your own repository, it is advised to set **mode** to `hook`. The endpoint `/runbot/hook` becomes available to process incoming webhooks from Github. Other systems, like cron, can call endpoint `/runbot/hook/<repo_id>` to refresh the repository.  
*It is advised to change the mode to 'hook' for all repositories to reduce end-to-end test latency.*

A config file with your remotes should be created for each repository. Verify the file contents at `/runbot/static/repo/(runbot|odoo)/config`.  
Data is fetched from the remotes automatically, the first fetch will take a long time.  
After fetching finishes you should see empty batches in both projects on the website frontend (`/` or `/runbot`)

### Triggers and linked config
At this point, runbot discovers new branches, new commits, creates bundles, but no builds.

To test the runbot code we want to create builds when the remote has new commits. To run runbot we also need a commit from the odoo repository. These requirements can be configured as a trigger.  
When triggers activate, they take the linked config to create one or multiple builds. 

Runbot has a couple of pre-configured configurations (config) that perform common operations like execute tests, keep test-build running, create coverage report, start multiple parallel builds for the same batch etc.

Configurations are composed of configuration steps. One of the default configuration steps is `all`, configured to test *all addons* including those from dependencies.  
At the moment we're only interested in testing runbot, skipping tests from odoo `base` and `web` finishes our builds more quickly and doesn't give false positive errors in case some of those tests break.  
We could duplicate an existing configuration but it's quicker to just modify the `all` config step.

Open the Runbot backend and open `Runbot > Configs > Build Config Steps`

Edit the config step `all` to restrict which tests to run
- Open the record named `all`
- **Test tags**: set to value `/runbot` ([More info about test tags](https://www.odoo.com/documentation/19.0/developer/reference/cli.html#cmdoption-odoo-bin-test-tags))

Open `Runbot > Configs > Build Config`

Remove the config step `base` from the config `Default no run`:
- Open the record named `Default no run`
- Inside the step order, remove config step `base`

Config and steps are powerful concepts, even allowing Python code to be executed. Advanced usage is out of scope for this guide.

Open `Runbot > Triggers`

Create a new trigger that will generate builds for the runbot repo:
- *Name*: `Runbot`, just for display.
- *Project id*: `runbot`, the trigger only reacts to repository updates linked to this project.
- *Repositories*: Add link to `runbot`, commits from runbot are the main subject.
- *Dependencies*: Add link to `odoo`, odoo source is required to run Runbot.
- *Config*: `Default no run`, start a build but don't keep a test-build running at the end. You can still wake up a build.

You can either push new commits to the remote, or go on the frontend bundle page and use the `Force new batch` button (refresh icon) to test this new trigger. Build 'Runbot' is automatically created.

If CI options are configured, triggers will send build status information to remotes that have a valid API token. This completes the CI feedback loop.

Runbot is now configured to automatically test changes made to the runbot code. This is the end of the guide. Good luck!

## Bundles

Bundles can be marked as `no_build`, so that new commit(s) won't create batches. The bundle won't be displayed on the overview page either.

## Hosts
Runbot is able to automatically assign pending builds across multiple hosts. Currently, this action is performed by the leader process. The platform can run without a leader process or with exactly one active leader process.  
A builder host will never assign a pending build to itself nor work-steal pending builds from other builders.

To manually assign builds to your fleet, exclude your buildhosts from the assignment pool and assign your build records directly to a specific builder.

Open `Runbot > Hosts`
- Pick your desired builder
- **Only accept assigned build**: `checked`, remove the current host from the assignment pool

Open `Runbot > Objects > Builds`
- Pick a build that the system created for you
- (optional) Duplicate it
- **Host name**: `<name of your builder>`, manually assign this build to the specified builder host


## Modules filters
Modules to install can be filtered by repo, and by config step. The filter configured on the repository is applied first, resulting in a 'default modules list' for all configurations. The filter configured on config is applied on top of the default modules list.

Prefixing a module name with a minus `-` sign eg, `-base`, will exclude it from the modules list. The Odoo Runbot team suggests installing handpicked toplevel modules to prevent installation of unnecessary modules taking additional build time.  
Use an asterisk to target all default modules eg, `-*` removes all modules from the list, and this can be suffixed with module name(s) we want to install eg, `-*,runbot` (comma seperated list).  
Ofcourse this requires the manifest of module `runbot` is correctly declaring its own module dependencies.

Tests can be filtered separately from modules, and an unconfigured value will run tests for all installed modules. You can restrict which test(s) to run using [the test-tags syntax](https://www.odoo.com/documentation/19.0/developer/reference/cli.html?highlight=cli#cmdoption-odoo-bin-test-tags) eg, `/runbot,/runbot_builder` will only run tests defined inside the modules runbot and runbot_builder.

## db template
Database creation before starting a build will use `template0` by default. It is possible to specify a specific template to use in the runbot settings field *Postgresql template*. A custom database is mainly used to add extensions.

The Odoo Runbot team recommends generating a new template database `template_runbot` based on `template0`, prepare it according to your needs, and set its name in the runbot settings.

```bash
su postgres

createdb template_runbot -T template0
# Mark database as template
psql --dbname postgres --command "update pg_database set datistemplate = true where datname = 'template_runbot'"

# Activate extensions and other manipulations
# psql --dbname template_runbot --command "CREATE EXTENSION <...>"
```

## Dockerfiles

Runbot automatically installs a default 'docker file' model to represent the runtime environment of the builds. This Dockerfile is based on Ubuntu Noble (24.04) and is capable of testing/running (supported versions of) Odoo.
DockerFile records  can be assigned to the models version and bundle.

The 'docker file' model uses Odoo QWeb views to compile Dockerfile text. You can construct the Dockerfile contents with reusable layer elements, and you can also paste your own Dockerfile text into a layer of type 'Raw'.  
All Dockerfile records with `to_build` field checked are built automatically (pay attention that no other operations will occur during the build).

The Odoo Runbot team suggests creating a new Dockerfile by duplicating the default one and adapt parameters in the view.  
The Odoo Runbot team also maintains a set of prebuilt docker images that are compatible with actively supported Odoo versions. Ask them for a link to use in your own Runbot.

## User documentation

You can find a more detailed user documentation [here](./runbot/documentation/readme.md)
