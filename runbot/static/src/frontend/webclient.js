import { Component, useState, useEffect } from '@runbot/owl';

import { useRegistry } from '@web/core/registry_hook';
import { registry } from '@web/core/registry';

import { Navbar } from './components/navbar';
import { useNavigation } from './navigation_service';


const routeRegistry = registry.category('runbot.routes');

export class Webclient extends Component {
    static template = 'runbot.Webclient';
    static props = {};
    static components = {
        Navbar,
    };

    setup() {
        this.routes = useRegistry(routeRegistry);
        this.navigationState = useState(useNavigation().state);

        useEffect(() => {
            const listener = () => this.navigationState.currentPath = window.location.pathname;
            window.addEventListener('popstate', listener);
            return () => window.removeEventListener('popstate', listener);
        }, () => [])
    }

    get currentRouteAndProps() {
        const pathname = this.navigationState.currentPath;
        for (let route of this.routes.entries) {
            const [_, routeConfig] = route;
            const { routes } = routeConfig;
            for (let routeRoute of routes) {
                const match = routeRoute.exec(pathname);
                if (!match) {
                    continue;
                }
                return [routeConfig, match.groups];
            }
        }
        return null;
    }
}

registry.category('main.components').add('webclient', {
    Component: Webclient,
});

