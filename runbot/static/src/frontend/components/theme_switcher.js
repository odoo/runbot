import { Component, useState } from '@runbot/owl';

import { getCookie, switchTheme } from '../utils';


export class ThemeSwitcher extends Component {
    static template = 'runbot.ThemeSwitcher';

    setup() {
        this.state = useState({
            theme: getCookie('colorScheme') || 'auto',
        });
    }

    switchTheme(theme) {
        switchTheme(theme);
        this.state.theme = theme;
    }
}
