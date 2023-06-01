FIX: lock in statuses at the end of a staging

The statuses of a staging are computed dynamically. Because github associates
statuses with *commits*, rebuilding a staging (partially or completely) or using
one of its commits for a branch could lead to the statuses becoming inconsistent
with the staging e.g. all-green statuses while the staging had failed.

By locking in the status at the end of the staging, the dashboard is less 
confusing and more consistent, and post-mortem analysis (e.g. of staging
failures) easier.
