import { reactive, useEffect, useState } from '@runbot/owl';

import { getCookie } from './utils';


let appState = undefined

/**
 * Get and listen to the global application state, this state will be initialised
 * with the data provided by the odoo.__runbot_data__ variable.
 *
 * @returns {Object} the app state.
 */
export const useAppState = () => {
    if (!appState) {
        appState = reactive(odoo.__runbot_data__);
        let activeCategory = appState.categories.find(c => c.id === appState.defaultCategory);
        const categoryCookie = getCookie('category');
        if (categoryCookie && appState.categories.find(c => c.id == categoryCookie)) {
            activeCategory = appState.categories.find(c => c.id == categoryCookie)
        }
        appState.activeCategory = activeCategory;
    }
    return useState(appState);
}

/**
 * Given an async function and its parameters handle loading state and data.
 *
 * @param {() => Promise<Any>} fn The async function.
 * @param {Function} depsFn The dependency function.
 */
export const useQuery = (fn, depsFn) => {
    let counter = 0; // Used to make sure we don't commit outdated data.
    const state = useState({
        loading: true,
        error: null,
        data: undefined,
    });

    useEffect(
        () => {
            // Use effect's effect can't be async.
            (async () => {
                const guard = (callback) => {
                    if (counter !== thisCounter) {
                        return;
                    }
                    callback();
                }
                const thisCounter = ++counter;
                state.loading = true;
                try {
                    const data = await fn();
                    guard(() => {
                        state.loading = false;
                        state.error = null;
                        state.data = data;
                    });
                } catch (err) {
                    guard(() => {
                        state.loading = false;
                        state.error = err?.message || 'error';
                        state.data = null;
                    });
                }
            })();
        },
        depsFn,
    )

    return state;
}
