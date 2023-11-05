/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example-js.sh:

USEAPI_SERVER="…" USEAPI_CHANNEL="…" USEAPI_TOKEN="…" USEAPI_DISCORD="…" node ./example.js

*/
const fs = require('fs');
const util = require('util');
const { Readable } = require('stream');
const { finished } = require('stream/promises');

const inputFileName = './prompts.json';
// We will utilize all three available job slots for the Basic or Standard plan.
const maxJobs = 3;

const sleep = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));

const dateAsString = () => new Date().toISOString();

const loadFromFile = async (filePath) => {
    const readFileAsync = util.promisify(fs.readFile);

    const data = await readFileAsync(filePath, 'utf8');

    if (data)
        return JSON.parse(data)
    else
        throw new Error('Unable to load file:', filePath);
}


const saveToFile = async (filePath, data) => {
    await fs.writeFile(filePath, JSON.stringify(data, null, 2), (err) => {
        if (err)
            console.error('Error writing to file:', err);
    });
}

const getFileNameFromUrl = (url) => {
    // Extract filename.png from https://cdn.discordapp.com/attachments/server_id/channed_id/filename.png?ex=
    const matches = url.match(/\/([^/?#]+)(?:[?#]|$)/);
    return matches && matches.length > 1 ? matches[1] : null;
}

const downloadFile = async (url, ind) => {
    const { body } = await fetch(url);
    localPath = `./${ind}-${getFileNameFromUrl(url)}`;
    const stream = fs.createWriteStream(localPath);
    await finished(Readable.fromWeb(body).pipe(stream));
};

const main = async () => {
    // Load all required parameters from the environment variables
    const token = process.env.USEAPI_TOKEN;
    const discord = process.env.USEAPI_DISCORD;
    const server = process.env.USEAPI_SERVER;
    const channel = process.env.USEAPI_CHANNEL;

    const prompts = await loadFromFile(inputFileName);

    console.log(`${dateAsString()} ⁝ prompts to process`, prompts.length);

    const results = [];

    let ind = 0;

    const startTime = performance.now();

    for (const prompt of prompts) {
        ind++;

        const data = {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        };

        // Detailed documentation at https://useapi.net/docs/api-v1/jobs-imagine
        data.body = JSON.stringify({ prompt, discord, server, channel, maxJobs });

        console.log(`${dateAsString()} ⁝ #${ind} prompt`, prompt);

        let attempt = 0;
        let retry = true;

        do {
            attempt++;

            const apiUrl = "https://api.useapi.net/v1/jobs/imagine";
            const response = await fetch(apiUrl, data);
            const result = await response.json();

            console.log(`${dateAsString()} ⁝ attempt #${attempt}, response`, { status: response.status, jobid: result.jobid, job_status: result.status, ind });

            switch (response.status) {
                case 429: // Query is full
                    console.log(`${dateAsString()} ⁝ #${ind} attempt #${attempt} sleeping for 10secs...`);
                    // Wait for 10 seconds before trying again
                    await sleep(10 * 1000);
                    break;
                case 200: // OK
                case 422: // Moderated
                    results.push({ status: response.status, jobid: result.jobid, job_status: result.status, ind, prompt });
                    retry = false;
                    break;
                default:
                    console.error(`Unexpected response.status`, result);
                    retry = false;
                    break;
            }
        } while (retry);

        await saveToFile('./result.json', results);
    }

    console.log(`${dateAsString()} ⁝ downloading generated images`);

    ind = 0;

    for (const item of results) {
        ind++;

        const { jobid, status, prompt } = item;
        console.log(`${dateAsString()} ⁝ #${ind} jobid`, { jobid, status });

        if (status == 422)
            console.warn(`moderated prompt`, prompt);

        if (status == 200) {
            let attempt = 0;
            let retry = true;
            do {
                attempt++;

                const apiUrl = `https://api.useapi.net/v1/jobs/?jobid=${jobid}`;
                const response = await fetch(apiUrl, {
                    headers: {
                        "Authorization": `Bearer ${token}`,
                    },
                });
                const result = await response.json();

                console.log(`${dateAsString()} ⁝ attempt #${attempt}, response`, { status: response.status, jobid: result.jobid, job_status: result.status, ind });

                switch (response.status) {
                    case 200:
                        if (result.status == 'completed') {
                            if (result.attachments?.length) {
                                downloadFile(result.attachments[0].url, ind);
                            } else {
                                console.error(`#${ind} completed jobid has no attachments`);
                            }
                            retry = false;
                        } else
                            if (result.status == 'started' || result.status == 'progress') {
                                console.log(`${dateAsString()} ⁝ #${ind} attempt #${attempt} sleeping for 10secs...`, result.status);
                                // Wait for 10 seconds before trying again
                                await sleep(10 * 1000);
                            } else {
                                console.error(`Unexpected response.status`, result);
                                retry = false;
                            }
                        break;
                    default:
                        console.error(`Unexpected response.status`, result);
                        retry = false;
                        break;
                }
            } while (retry);
        }
    }

    const executionTimeInMilliseconds = performance.now() - startTime;

    console.log(`${dateAsString()}  ⁝  total elapsed time ${new Date(executionTimeInMilliseconds).toISOString()}`);
}

main();
