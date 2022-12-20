# Teams and Codeowner

## How

Codeowner is using two way to define which team should be notified when a file is modified:

- Module ownership to link a module to a team. (Editable by team manager)
- Regexes, to target specific files or more specific rules. (Editable by runbot admin)

For each file, the codeowner will check all regexes and all module ownership.
If a module ownersip is a `fallback`, the team won't be added as a reviewer if any previous rule matched for a file.
If no reviewer is found for a file, a fallback github team is added as reviewer.

The codeowner is not applied on draft pull request (and will give a red ci as a reminder)
A pr is considered draft if:

- marked as draft on github
- contains `[DRAFT]` or `[WIP]` in the title
- is linked to any other draft pr (in the same bundle)

The codeowner is not applied on forwardport initial push. Any following push (conflict resolution) will trigger the codeowner again.

## Module ownership

Module ownership links a module to a team with an additionnal `is_fallback` flag to define if the codeowner should only be triggered if no one else was added for a file.

Module ownership is also a way to define which team should be contacted for some question on a module.

Module coverage should idealy reach 100% with module ownership. Having all files covered allows to ensure that at least one reviewer will be added when a pr is open (mainly for external contributors). This can sometimes generate to much github notifications, this is why it is important to configure members, create subteams, and skip pr policy.

## Team management

Team managers can be anyone from the team with a basic knowledge of the guidelines to follow and a good understanding of the system.

Team manager can modify.

- Teams
- Module ownership
- Github account of users

Some basic config can be done on Teams

- `Github team`: the corresponding github team to add as reviewer
- `Github logins`: additional github logins in the github team, mainly for github users no listed in the members of the runbot team. Mainly usefull if `Skip team pr` is checked. This list can be updated automatically using the `Fetch members` action. This field can also be manually modified to avoid being notified by some github login, even if it is adviced to add them as a `Team members` if they have an internal user.
- `Skip team pr`: If checked, don't add the team as reviewer if the pr was oppened by one of the members of the team.
- `Module Ownership`: The list of modules owned by the team. `Fallback` options can be edited from there but it is adviced to use the `Modules` or `Module ownership` menu to add or remove a module, mainly to avoid removing all ownership from a module.
- `Team members`: the members of the team. Those members will see a link to the team dashboard and team errors count will be displayed on main page

## Disable codeowner on demand

In some rare cases, if a pr modifies a lot of files in an almost automated way, it can be useful to disable the codeowner. This can be done on a bundle. Note that forwardport won't be impacted, and this should be done per forwardport in case of conflict.
