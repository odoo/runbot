const Interactions = [];

async function start() {
    for (const I of Interactions) {
        const interaction = new I();
        await interaction.whenReady;
    }
}

start();
