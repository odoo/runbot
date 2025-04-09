import { reactive } from '@runbot/owl';


const state = reactive({
    currentPath: window.location.pathname,
    currentSearch: window.location.search,
});

export const useNavigation = () => {

    const navigate = (url) => {
        if (!url.startsWith('/') && url !== '#') {
            throw new Error('Navigate can only be used with same origin links.')
        }
        window.history.pushState({}, '', url);
        state.currentPath = window.location.pathname;
        state.currentSearch = window.location.search;
    }

    return { state, navigate };
};

odoo.runbotNavigate = useNavigation().navigate;
