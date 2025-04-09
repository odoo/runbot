import { Component, useState } from '@runbot/owl';

import { useNavigation } from '../navigation_service';


export class Link extends Component {
    static template = 'runbot.Link';
    static props = {
        href: String,
        class: {
            type: String,
            optional: true,
        },
        title: {
            type: String,
            optional: true,
        },
    };

    setup() {
        this.state = useState({
            active: true,
        });
        const { navigate } = useNavigation();
        this.navigate = navigate;
    }

    onClick(ev) {
        const url = new URL(ev.currentTarget.href);
        if (url.origin !== window.location.origin) {
            return
        }
        const navigateUrl = url.href.replace(window.location.origin, '');
        this.navigate(navigateUrl);
        ev.preventDefault();
        ev.stopPropagation();
    }
}
