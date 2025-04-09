import { Component, useEffect, useState } from '@runbot/owl';
import { useNavigation } from '../navigation_service';
import { useAppState } from '../hooks';
import { slugify } from '../utils';


export class BundleSearch extends Component {
    static template = 'runbot.BundleSearch';

    setup() {
        this.appState = useAppState();
        this.useNavigation = useNavigation();
        
        this.navState = useState(this.useNavigation.state);
        const searchParams = new URLSearchParams(this.navState.currentSearch);
        this.searchState = useState({
            search: searchParams.get('search') || '',
            has_pr: (searchParams.get('has_pr') || '') === 'on',
        });

        useEffect(
            () => {
                const searchParams = new URLSearchParams(this.navState.currentSearch);
                this.searchState.search = searchParams.get('search') || '';
                this.searchState.has_pr = (searchParams.get('has_pr') || '') === 'on';
            },
            () => [this.navState.currentSearch],
        );
    }

    onHasPrKeydown(ev) {
        if (ev.key !== ' ') {
            return;
        }
        this.searchState.has_pr = !this.searchState.has_pr;
        ev.preventDefault();
        ev.stopPropagation();
    }

    // TODO: debounce me
    submitSearch(ev) {
        const formData = Object.fromEntries(new FormData(ev.currentTarget));
        this.useNavigation.navigate(
            `/runbot/${slugify(this.appState.activeProject)}?${new URLSearchParams(formData).toString()}`
        );
    }
}
