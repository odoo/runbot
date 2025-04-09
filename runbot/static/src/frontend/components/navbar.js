import { Component } from '@runbot/owl';

import { Link } from './link';
import { ThemeSwitcher } from './theme_switcher';
import { useAppState } from '../hooks';
import { slugify, setCookie } from '../utils';
import { BundleSearch } from './bundle_search';


export class Navbar extends Component {
    static template = 'runbot.Navbar';
    static components = { Link, ThemeSwitcher, BundleSearch };

    setup() {
        this.appState = useAppState();
    }

    get activeProject() {
        return this.appState.activeProject || this.appState.projects[0];
    }

    slugify(project) {
        return slugify(project);
    }

    backToOldClient() {
        setCookie('use_owl_client', '0');
        window.location.reload();
    }
}
