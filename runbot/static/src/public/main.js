import { Runbot } from "./runbot";

const Interactions = [Runbot];

async function start() {
    for (const I of Interactions) {
        const interaction = new I();
        await interaction.whenReady;
    }
}

start();
