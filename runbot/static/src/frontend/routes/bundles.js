import { Component, useEffect } from '@runbot/owl';

import { registry } from '@web/core/registry';

import { Link } from '../components/link';

import { unslug_re } from '../utils';
import { useAppState, useQuery } from '../hooks';


export class Bundles extends Component {
    static template = 'runbot.Bundles';
    static props = {
        projectId: {
            type: String,
            optional: true,
        },
        search: {
            type: String,
            optional: true,
        }
    };
    static components = {
        Link,
    };

    setup() {
        this.appState = useAppState();

        if (this.props.projectId) {
            this.appState.activeProject = Number(this.props.projectId);
        }
        if (!this.appState.activeProject) {
            this.appState.activeProject = this.appState.projects[0].id;
        }

        useEffect(
            () => {
                if (this.props.projectId) {
                    console.log('updating activeProject');
                    this.appState.activeProject = Number(this.props.projectId);
                }
                const project = this.appState.projects.find(p => p.id === this.appState.activeProject);
                document.title = `Runbot - ${project.name}`
            },
            () => [this.props.projectId],
        );

        this.queryState = useQuery(
            () => fetch(
                '/runbot/api/runbot.bundle/read', {
                    method: 'POST',
                    body: JSON.stringify({
                        domain: [['last_batch', '!=', false]],
                        project_id: this.appState.activeProject,
                        category_id: this.appState.activeCategory.id,
                        context: {
                            category_id: this.appState.activeCategory.id,
                        },
                        specification: {
                            id: {},
                            name: {},
                            sticky: {},
                            branch_ids: {
                                fields: {
                                    id: {},
                                    dname: {},
                                    branch_url: {},
                                }
                            },
                            last_batchs: {
                                fields: {
                                    id: {},
                                    state: {},
                                    age: {},
                                    slot_ids: {
                                        fields: {
                                            link_type: {},
                                            trigger_id: {
                                                fields: {
                                                    name: {},
                                                },
                                            },
                                            build_id: {
                                                fields: {
                                                    id: {},
                                                    local_state: {},
                                                    local_result: {},
                                                    global_state: {},
                                                    global_result: {},
                                                    requested_action: {},
                                                    log_list: {},
                                                    version_id: {},
                                                    config_id: {},
                                                    trigger_id: {},
                                                    create_batch_id: {},
                                                    host_id: {
                                                        fields: {
                                                            name: {}
                                                        }
                                                    },
                                                    database_ids: {
                                                        fields: {
                                                            name: {}
                                                        }
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    commit_link_ids: {
                                        fields: {
                                            match_type: {},
                                            remote_base_url: {},
                                            commit_id: {
                                                fields: {
                                                    name: {},
                                                    dname: {},
                                                    subject: {},
                                                    repo_id: {
                                                        fields: {
                                                            id: {},
                                                            sequence: {},
                                                        }
                                                    },
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    })
                }
            ).then(d => d.json()),
            () => [this.appState.activeProject, this.appState.activeCategory],
        )
    }

    get dataString() {
        return JSON.stringify(this.queryState.data);
    }

    get errorString() {
        return JSON.stringify(this.queryState.error);
    }

    sortedCommits(commit_links) {
        return [...commit_links].sort((a, b) => {
            return a.commit_id.repo_id.sequence - b.commit_id.repo_id.sequence ||
                a.commit_id.repo_id.id - b.commit_id.repo_id.id;
        });
    }
}


registry.category('runbot.routes').add('runbot.Bundles', {
    routes: [
        new RegExp('^\\/$'),
        new RegExp('^\\/runbot\\/?$'),
        new RegExp(`^\\/runbot\\/${unslug_re('projectId')}/?$`),
    ].reverse(),
    Component: Bundles,
    hasNavbar: true,
});
