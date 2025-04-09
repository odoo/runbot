import { Component, useEffect, useState } from '@runbot/owl';

import { registry } from '@web/core/registry';

import { Link } from '../components/link';

import { unslug_re } from '../utils';
import { useAppState, useQuery } from '../hooks';
import { useNavigation } from '../navigation_service';


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
        this.navigationState = useState(useNavigation().state);

        useEffect(
            () => {
                if (this.props.projectId) {
                    this.appState.activeProject = this.appState.projects.find(p => p.id === Number(this.props.projectId));
                } else {
                    this.appState.activeProject = this.appState.projects[0];
                }
                if (this.appState.activeProject) {
                    document.title = `Runbot - ${this.appState.activeProject.name}`;
                }
            },
            () => [this.props.projectId],
        );

        this.queryState = useQuery(
            () => this.fetchBundleData(),
            () => [
                this.appState.activeProject, this.appState.activeCategory,
                this.navigationState.currentSearch,
            ],
        )
    }

    get dataString() {
        return JSON.stringify(this.queryState.data);
    }

    get errorString() {
        return JSON.stringify(this.queryState.error);
    }

    async fetchBundleData() {
        const searchParams = new URLSearchParams(window.location.search);
        const domain = [['last_batch', '!=', false]];
        if (searchParams.get('has_pr') === 'on') {
            domain.splice(0, 0, '&');
            domain.push(['has_pr', '=', true]);
        }
        if (searchParams.has('search')) {
            const search = searchParams.get('search').trim();
            const searchDomains = [];
            const prNumbers = []
            search.split('|').forEach(
                searchComponent => {
                    if (/\d+/.exec(searchComponent)) {
                        prNumbers.push(Number(searchComponent));
                    }
                    const operator = searchComponent.includes('%') ? '=ilike' : 'ilike';
                    searchDomains.push(['name', operator, searchComponent]);
                }
            );
            if (prNumbers.length) {
                searchDomains.push(['branch_ids.name', 'in', prNumbers]);
            }
            const searchDomain = searchDomains.reduce((domain, leaf, index) => {
                if (index !== 0) {
                    domain.splice(0, 0, '|');
                }
                domain.push(leaf);
                return domain;
            }, []);
            domain.splice(0, 0, '&');
            domain.push(...searchDomain);
        }
        const res = await fetch(
            '/runbot/api/runbot.bundle/read', {
                method: 'POST',
                body: JSON.stringify({
                    domain: domain,
                    project_id: this.appState.activeProject.id,
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
        )
        return await res.json();
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
