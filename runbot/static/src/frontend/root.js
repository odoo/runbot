import { whenReady, Component, xml, App, onError, EventBus } from '@runbot/owl';

import { getTemplate } from '@web/core/templates';
import { registry } from '@web/core/registry';
import { useRegistry } from '@web/core/registry_hook';
import { InteractionService } from '@web/public/interaction_service';


const mainComponents = registry.category('main.components');

class ErrorHandler extends Component {
    static template = xml`<t t-slot="default" />`;
    static props = ["onError", "slots"];
    setup() {
        onError((error) => {
            this.props.onError(error);
        });
    }
}

class ComponentContainer extends Component {
    static components = { ErrorHandler };
    static props = {};
    static template = xml`
    <div class="o-main-components-container">
        <t t-foreach="Components.entries" t-as="C" t-key="C[0]">
            <ErrorHandler onError="error => this.handleComponentError(error, C)">
                <t t-component="C[1].Component" t-props="C[1].props"/>
            </ErrorHandler>
        </t>
    </div>
    `;

    setup() {
        this.Components = useRegistry(mainComponents);
    }

    handleComponentError(error, C) {
        console.error('Error while rendering', C[0], 'removing from app.');
        // remove the faulty component and rerender without it
        this.Components.entries.splice(this.Components.entries.indexOf(C), 1);
        this.render();
        /**
         * we rethrow the error to notify the user something bad happened.
         * We do it after a tick to make sure owl can properly finish its
         * rendering
         */
        Promise.resolve().then(() => {
            throw error;
        });
    }
}

/**
 * Bootstrap the frontend.
 */
(async function startApp() {
    await whenReady();

    const env = {
        // These attributes are required by vendored data
        bus: new EventBus(),
        isReady: Promise.resolve(true),
        services: {},
        debug: odoo.debug,
    };

    const app = new App(ComponentContainer, {
        getTemplate,
        env,
    });
    await app.mount(document.body);
    const Interactions = registry.category('public.interactions').getAll();
    const service = new InteractionService(document.body, env);
    service.activate(Interactions);
})();
