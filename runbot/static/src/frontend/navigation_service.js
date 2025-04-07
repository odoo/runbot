import { reactive } from '@runbot/owl';


const state = reactive({
    currentPath: window.location.pathname,
});

export const useNavigation = () => {

    const navigate = (url) => {
        if (!url.startsWith('/') && url !== '#') {
            throw new Error('Navigate can only be used with same origin links.')
        }
        window.history.pushState({}, '', url);
        state.currentPath = window.location.pathname;
    }

    return { state, navigate };
};

odoo.runbotNavigate = useNavigation().navigate;
