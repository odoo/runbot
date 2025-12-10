import { whenReady } from "@odoo/owl";
import { makeEnv, startServices } from "@web/env";

export async function start() {
    await whenReady();

    const env = makeEnv();
    await startServices(env);
}

start();
