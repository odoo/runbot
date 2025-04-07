import { Component } from '@runbot/owl';

import { Link } from './link';
import { ThemeSwitcher } from './theme_switcher';
import { useAppState } from '../hooks';
import { slugify } from '../utils';


export class Navbar extends Component {
    static template = 'runbot.Navbar';
    static components = { Link, ThemeSwitcher };

    setup() {
        this.appState = useAppState();
    }

    slugify(project) {
        return slugify(project.name, project.id);
    }

    get activeProject() {
        return this.appState.projects.find(p => p.id === this.appState.activeProject) ||
            this.appState.projects[0];
    }
}
