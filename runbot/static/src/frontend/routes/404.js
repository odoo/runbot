import { Component, useEffect, useState } from '@runbot/owl';

import { registry } from '@web/core/registry';
import { useQuery } from '../hooks';
import { useNavigation } from '../navigation_service';


export class Page404 extends Component {
    static template = 'runbot.404';

    setup() {
        this.navigationState = useState(useNavigation().state);

        this.queryState = useQuery(
            () => fetch(this.navigationState.currentPath, {
                method: 'HEAD',
            }),
            () => [this.navigationState.currentPath],
        );

        useEffect(
            () => {
                if (this.queryState.loading || this.queryState.error) {
                    return;
                }
                if (this.queryState.data.status === 200) {
                    // The url exists in the backend, we try to reload the page
                    window.location.reload();
                } else {
                    this.queryState.error = 1;
                }
            },
            () => [this.queryState.loading, this.queryState.data],
        )
    }
}

registry.category('runbot.routes').add('runbot.Page404', {
    routes: [
        new RegExp('^.*$'),
    ],
    Component: Page404,
    hasNavbar: true,
}, {
    sequence: 1000,
});
