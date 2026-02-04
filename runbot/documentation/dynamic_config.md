# Dynamic configs

Dynamic configs are a way to define how to install, test, create builds, or even run scripts from a JSON file that can be defined either on the config server side or in the code at a specified path.

Dynamic configs can also be extended to define custom behavior in another repository (extend a blacklist, change behavior, ...) or on the config itself, to define a final layer of configuration.

## Example

An [example file](../tests/test_build_config_step_dynamic.json) is available and used for testing. It is a simplified version of the parallel testing used on runbot and will be used as a reference since it is quite complete.

## Config Schema

### Config root
The config structure is defined as follows:
```python
    {
        'name': REQUIRED(NAME),
        'vars': OPTIONAL(VARS),
        'steps': REQUIRED(LIST(STEP)),
        'description': OPTIONAL(DYNAMIC_VALUE),
    }
```

The steps are the sequence of operations to execute inside a single build. At least one step is required.

The name is mandatory and should ideally be unique. It is used for display and can also be used as an anchor when using extensions. The NAME can contain alphanumerical characters, spaces, _ and -.
```json
    "name": "Parallel testing",
```
The vars key is a dictionary of variables. This can be used to define values that will be used in multiple places in the config. Variables are currently supported by `description`, `install_modules`, and `test_tags`.

```json
    "vars": {
        "module_filter": "*,-hw_*"
    },
```

The steps key is a list of steps, each step defining a name, a job_type, and some parameters specific to the job_type.

```json
    "steps": [
        {
            "name": "install all",
            "job_type": "odoo",
            "db_name": "all",
            "install_modules": "{{module_filter}}"
        },
    ]
```

The description can be used to add a custom description to the build, usually used when creating multiple children to differentiate them.

```json
    "description": "Post install tests for **{{test_module_filter}}**",
```

### Config steps

The config steps are mainly defined by their `job_type`. The `name` key is also mandatory for display and extension purposes.

#### Odoo step
```python
{
    'name': REQUIRED(NAME),
    'job_type': 'odoo',
    'db_name': OPTIONAL(TECHNICAL_NAME),
    'install_modules': OPTIONAL(DYNAMIC_VALUE),
    'install_default_modules': OPTIONAL(DYNAMIC_VALUE),
    'test_tags': OPTIONAL(DYNAMIC_VALUESTR),
    'demo_mode': OPTIONAL(IN(['default', 'with_demo', 'without_demo'])),
    'enable_auto_tags': OPTIONAL(BOOL),
    'cpu_limit': OPTIONAL(INT),
    'make_stats': OPTIONAL(BOOL),
}
```
The `db_name` is optionnal, usually set to all as a convention on runbot for databases that contains *almost* all modules. If not defined the sanitized version of the name will be used.

`install_modules` and `install_default_modules` behave the same way except that `install_modules` will consider that we start with no module (prepends `-*` filter) while `install_default_modules` will be based on the runbot default module list (all available modules minus the repo blacklist)

Both entries will use the value as a runbot module filter, and then passed as the -i, [see corresponding section](#module-selection) for more info.

```json
    "install_modules": "{{module_filter}}"
```
In this example, `module_filter` is a variable defined in the vars section.

test_tags is used to define which tests to run and will be passed to odoo.

```json
    "test_tags": "-post_install,-/test_lint"
```
test_tags is also a dynamic value and can use variables, as well as [filter](#filter).

```json
    "test_tags": "-at_install,{{split_test_module_selector|filter_all_modules|make_module_test_tags}}",
```


`demo_mode` is used to define whether the flag --with-demo or --without-demo should be passed to odoo during installation. The default value is 'default', meaning that no flag will be passed (behavior depends on the odoo default, changed in 18.2).

`enable_auto_tags` is True by default and will enable the automatic addition of test-tags when running tests. The automatic test tags are based on build errors.

`cpu_limit` is the maximum CPU time that can be used by the docker. This is also used as an execution time limit for the step. It is mostly useful to avoid having a build stuck and taking a slot for too long. It is not advised to set this value in the config since it could start to break randomly if the execution time is too close to the limit. This can be set using an [extension](#extensions) on the config in the database.

#### Create build step
```python
{
    'name': REQUIRED(NAME),
    'job_type': 'create_build',
    'children': REQUIRED(LIST(CONFIG)),
    'for_each_vars': OPTIONAL(LIST(VARS)),
    'for_each_module': OPTIONAL(DYNAMIC_VALUE),
    'max_builds': OPTIONAL(INT),
}
```

`children` is a list of configs, each child will use one of the configs defined in the list.

```json
{
    "name": "Test at install",
    "job_type": "create_build",
    "children": [
        {
            "name": "Test at install",
            "steps":[{
                "name": "test_at_install",
                "job_type": "odoo",
                "install_modules": "{{module_filter}}",
                "test_tags": "-post_install,-/test_lint"
            }]
        },
        {
            "name": "Test pylint",
            "steps":[{
                "name": "test_pylint",
                "job_type": "odoo",
                "install_modules": "test_lint",
                "test_tags": "-post_install,/test_lint",
                "enable_auto_tags": false
            }]
        }
    ]
}
```

In this example, two children will be created: one testing at_install tests of all modules, the other testing test_lint only.

It is also possible to define `for_each_vars`, which is a list of variable dictionaries. For each entry in the list, a child will be created for each config in the children list, with the variables overridden by the for_each_vars entry.

```json
{
    "name": "Test single module",
    "job_type": "create_build",
    "for_each_vars": [
        {"module": "mail"},
        {"module": "web"},
    ],
    "children": [{
        "name": "Test Single module",
        "description": "Testing module **{{module}}**",
        "steps": [
            {
                "name": "test_post_install",
                "install_modules": "{{module}}",
                "test_tags": "/{{module}}",
                "job_type": "odoo",
                "db_name": "all"
            }
        ]
    }]
}
```

In this example, two children will be created, both using the same config, but one will test the mail module and the other the web module. The description also uses the variable to differentiate the two builds.

This is how the runbot post install builds are created, using [filters](#filters) to transform module filters into test tags.
```json
{
    "name": "create_post_install",
    "job_type": "create_build",
    "for_each_vars": [
        {"test_module_filter": "-> !mail"},
        {"test_module_filter": "mail -> !web"},
        {"test_module_filter": "web -> web"},
        {"test_module_filter": "!web ->"}
    ],
    "children": [{
        "name": "Test Post Install",
        "description": "Post install tests for **{{test_module_filter}}**",
        "steps": [
            {"name": "restore", "job_type": "restore", "db_name": "all"},
            {
                "name": "test_post_install",
                "job_type": "odoo",
                "test_tags": "-at_install,{{test_module_filter|filter_all_modules|make_module_test_tags}}",
                "db_name": "all"
            }
        ]
    }]
}
```

`for_each_module` allow to create one child per selected module (comma separated list, can be a dynamic value)

```json
 {
    "job_type": "create_build",
    "for_each_module": "base,web,mail"
}
```
``` json
{
    "job_type": "create_build",
    "for_each_module": "{{*|filter_all_modules|modified_modules}}",
    "max_builds": 20,
}
```



### Restore steps

```python
{
    'name': REQUIRED(NAME),
    'job_type': 'restore',
    'db_name': OPTIONAL(TECHNICAL_NAME),
    'build_id': OPTIONAL(INT),
    'trigger_id': OPTIONAL(INT),
    'use_current_batch': OPTIONAL(BOOL),
    'zip_url': OPTIONAL(STR),
}
```

This job type will restore a database

By default, it will restore a db "all" in the parent build.
A `db_name` can be provided to change the database name from "all" to any other value.
A `build_id` can be provided to restore from another build instead of the parent one.
A `trigger_id` can be provided to restore from the build created by a specific trigger instead of the parent one. By default, it will look for such a trigger in the batch from a base branch that was used to create the current batch (base_reference_batch_id). This is mainly useful for multibuild, restoring a database that is close enough to the current commits automatically when we only want to run the tests. Alternatively, if `use_current_batch` is set to True, the trigger will be searched in the current batch instead of the base_reference_batch_id one. Note that it will most likely fail if executed before the build creating the dump is finished, but can still be useful for manual triggers when the build will be run manually after the main database is installed, when some model/data changes are needed.


### Command steps

```python
{
    'name': REQUIRED(NAME),
    'job_type': 'command',
    'db_name': OPTIONAL(TECHNICAL_NAME),
    'command': REQUIRED(COMMAND),
    'cpu_limit': OPTIONAL(INT),
    'install_requirements': OPTIONAL(BOOL),
    'export_database': OPTIONAL(BOOL),
    'make_stats': OPTIONAL(BOOL),
}
```

The `command` step will run a custom command inside the odoo container.

The `command` is a dynamic value that will be formatted with some basic values: `db_name`, `data_dir`, `addons_path`, `exports`, `exports_paths`

Example:
```json
{
    "name": "Running standalone",
    "db_name": "l10n",
    "job_type": "command",
    "command": "odoo/odoo/tests/test_module_operations.py -d {{db_name}} --data-dir {{data_dir}} --addons-path {{addons_path}} --standalone all_l10n"
}
```
Note: db_name is not the same as the db_name passed in the parameter; the parameter is actually a suffix, while the parameter is the complete dbname (with build destination prefix).



## Extensions

It is possible to extend a dynamic config by defining an extension either on the config server side or in another file/repository.
```json
{
    "extension": true,
    "vars": {
        "module_filter": ["APPEND", ",-l10n_*"]
    },
}
```
This will alter the variable `module_filter` append `,-l10n_*`, effectively excluding all l10n modules from the selection.

```json
{
    "extension": true,
    "steps": [
        {
            "@name": "install all",
            "cpu_limit": ["SET", 6500]
        }
    ]
}
```
This will search in all steps for a step named "install all" and set its cpu_limit to 6500.

The basic logic of extension is that both the base and extension structures are explored on all matching entries, until one of the entries of the extension is a command (a two-element list with a command and a value). The command is then applied to the base value.
When traversing a list, the matching is done using all keys starting with `@`. In the previous example, the step with name "install all" is matched using the `@name` key. If no key was given, all steps would have been extended.

Currently available commands are:

- SET: set the value to the given value
- APPEND: append the given value to the base value (only for strings and lists)


## Module selection

The runbot module selection works by parsing all filters/selectors (comma-separated list) in the given order, each element adding or removing modules from the selection.

The basic selector is a fnmatch on a module name.

Selectors can be prefixed with a `-` to exclude modules matching the selector.

If not prefixed with a `-`, the selector will add all available modules matching the selector to the selection.

The first selector is usually `*`, meaning to select all modules, or '-*', to ensure that we start with an empty list.

`*,-hw_*` means to select all modules except those starting with `hw_`.
`*,-*l10n_*,test_l10n*` means to select all modules except those containing `l10n_`, but still includes modules starting with `test_l10n`.
`blacklisted_module` will force inclusion of a `blacklisted_module` even if it is blacklisted. Since the selection is not starting with `*`, only modules not blacklisted on the repo will be added by default. Note that once a `*` is added at some point, the repo blacklist is completely ignored.

Additionally, some filters can also be used to filter the current list of modules using a range based on alphanumeric sorting of module names:
`[!]<m1> -> [!]<m2>`

The -> defines a range, selecting all modules between m1 and m2 inclusively. If m1 is omitted, the range starts at the beginning of the list; if m2 is omitted, the range ends at the end of the list.
The ! negates the module, meaning that the module itself will be excluded from the selection.

`-> !mail` will keep only modules that are before `mail` in alphanumeric order, excluding mail.
`mail -> !web` will keep only modules between mail and web, excluding web but including mail.
`web -> web` will keep web.
`!web ->` will keep only modules after web, excluding web.

Note that using `web` instead of `web->web` would include web, but also all modules before and after web, effectively selecting all modules. An equivalent solution could be to use `-*,web`.


## Filters

Filters are a way to transform dynamic values before using them. They are defined by appending `|filter_name` to the dynamic value.

For example, to transform a module filter into test tags:

```json
    {"test_tags": "-at_install,{{test_module_filter|filter_all_modules|make_module_test_tags}}",
```

In this example, the `filter_all_modules` filters will first transform the `test_module_filter` variable (which is a module filter) into a list of modules, and then the `make_module_test_tags` filters will transform this list of modules into test tags by prepending each module with a `/` to indicate that we want to run all tests from these modules.

Note that `filter_all_modules` is actually equivalent to `filter_default_modules`, but prepending a `*` at the begining of the filter.

`*,mail -> !web|filter_default_modules` is the same as `mail -> !web|filter_all_modules`

In some case we also want to combine the test-tags module with another tag or test method, this can be done using prepend and append

`"{{-*,web*|filter_all_modules|make_module_test_tags|append('.test_method')}}`
`{{-*,web*|filter_all_modules|make_module_test_tags|prepend('custom_tag')}}`


It is also possible to filter modules based on the one modified in the current bundle.
`{{*|filter_all_modules|modified_modules}}"`
