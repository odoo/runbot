# Upgrades

In order to test an upgrade, Runbot needs:
- a source build (Template) that contains a database with the source version. This build also provides initial commits in order to execute pre-upgrade tests.
- target commits: upgrade commits coming from the master version, and odoo/enterprise/... coming from the target version.

To have a well-defined set of commits, all of them are taken from other batches (one per version), referenced on the current batch.

Upgrade logic can be a little more complex to understand because of the nature of the upgrade repository: the upgrade repository has only a single branch (master) that contains all the upgrade scripts between all versions. This means that we need to use master sources in stable branches, making the Runbot base branch matching insufficient to define the commits to use in upgrade tests.

As a simplified example, the latest stable version is 10.0 on this page.

## Glossary 

Bundle: a group of branches having the same name in different repositories.
*Base bundle*: a group of "main" branches (master, 10.0, saas-10.1, ...).  
*Batch*: at a given time, a snapshot of all commits in a bundle, reference batches, and corresponding builds. A batch is *preparing* after creation, waiting for all commits, *ready* when all commits are fixed and builds can be started, and *done* when all builds are finished.

## Understanding reference batches

When creating a new batch on a base bundle (after detecting a new commit or starting manually from the interface), the last done batch of each version (base bundle) is linked to the new batch to be used in upgrade tests.

When creating a new batch on a dev bundle, the last batch (done or not) of the corresponding bundle is referenced by the new dev batch and will be used to define reference batches for upgrades.

This ensures a consistent set of commits for upgrade tests, and also that a dev branch will use the same references as a base to allow better build matching (deduplication to reduce load). It also ensures that an error does not appear in a dev branch without being seen in the base branch.

## Upgrade builds

Upgrades on runbot.odoo.com are using 3 distinct builds:

### Template
The template runs in all versions and installs the database (or set of databases) to be used as a source in upgrade tests.

### Upgrade current
The upgrade current runs in all versions and upgrades the current version, from and to the compatible versions.

For example: 
When testing a master branch, it will upgrade from the last stable version (10.0) to master and the enabled stable saas between master and these versions (saas-10.1).
 - 10.0 -> master
 - saas-10.1 -> master

When testing a stable branch (e.g., 10.0), it will upgrade from the previous stable version to the current stable version, and from the last stable saas to the current stable version, but also to the above versions that would use the current version as a source. See the "Upgrade to above versions" section for more details.
 - 9.0 -> 10.0
 - saas-9.5 -> 10.0
 - 10.0 -> master
 - 10.0 -> saas-10.1

## Upgrade stable
Upgrade stable only runs in master and only runs when upgrade scripts are modified. This build will test all combinations that are not tested in upgrade current to ensure that the modifications in upgrade scripts are covered.

For example:
 - 8.0 -> 9.0
 - 9.0 -> 10.0
 - saas-9.5 -> 10.0
 - 10.0 -> saas-10.1

## Upgrade to above versions

In the upgrade current build, the reason why we need to test upgrading to the above version is to ensure that a change in 10.0 will not break the upgrade from 10.0 to master.
For example, adding a new module in 19.0 is acceptable (a new module is stable-friendly), but it would break the upgrade from 19.0 to master if not forward-ported properly.
To avoid breaking the master branch, this test is needed in 10.0.

A build breaking when upgrading to a higher version can be bypassed in some cases with an upgrade exception. The upgrade exception will basically silence this specific error in all versions, meaning that merging the module will not break the master upgrade. The upgrade exception is needed while the module is being forward-ported and can be removed once the forward-port is complete.

## Commit selection

To select the commits of the target version, we need some of them from master (upgrades) and others from the target version (odoo/enterprise).
If the current branch is a master branch, all commits should come from the current batch.
When the current branch is a stable one, the commits are selected from the reference batches: the master one for the upgrade commits, and the one matching the target branch for odoo and enterprise. When the target is master from a stable branch, all commits come from the same batch, which is crucial to avoid inconsistencies between upgrade and odoo/enterprise commits.