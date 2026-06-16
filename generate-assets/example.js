/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example.sh:

USEAPI_TOKEN="…" node ./example.js

USEAPI_CHANNEL is optional with the Midjourney API v3 — when omitted the API
automatically selects a configured channel with available capacity. Provide it
only when you want to pin requests to a specific channel.

*/
const fs = require('fs');
const util = require('util');
const { Readable } = require('stream');
const { finished } = require('stream/promises');

const inputFileName = './prompts.json';
// You can use https://webhook.site or https://ngrok.com if you want to receive results via callback.
const replyUrl = undefined;
// Provide additional prompt params
const withParams = ' --relax'; // --fast or --relax
// Time to pause between calls
const sleepSecs = 5;
// Midjourney API v3 root url
const rootUrl = 'https://api.useapi.net/v3/midjourney';

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
    // Extract filename.png from https://cdn.discordapp.com/attachments/server_id/channel_id/filename.png?ex=
    const matches = url.match(/\/([^/?#]+)(?:[?#]|$)/);
    return matches && matches.length > 1 ? matches[1] : null;
}

const downloadFile = async (url, ind) => {
    const { body } = await fetch(url);
    const localPath = `./${ind}-${getFileNameFromUrl(url)}`;
    const stream = fs.createWriteStream(localPath);
    await finished(Readable.fromWeb(body).pipe(stream));
};

const main = async () => {
    // Load all required parameters from the environment variables
    const token = process.env.USEAPI_TOKEN;
    const channel = process.env.USEAPI_CHANNEL; // optional in v3

    const prompts = await loadFromFile(inputFileName);

    console.log(`${dateAsString()} ⁝ prompts to process`, prompts.length);

    const results = [];

    let ind = 0;

    const startTime = performance.now();

    for (const _prompt of prompts) {
        const prompt = `${_prompt} ${withParams}`.trim();
        ind++;

        const data = {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        };

        // Detailed documentation at https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
        data.body = JSON.stringify({
            prompt,
            stream: false, // stream defaults to true (SSE); set false to get the JSON job state we poll below
            channel: channel || undefined, // optional — API auto-selects a channel with capacity when omitted
            replyUrl: replyUrl ? `${replyUrl}?ind=${ind}` : undefined
        });


        console.log(`${dateAsString()} ⁝ #${ind} prompt`, prompt);

        let attempt = 0;
        let retry = true;

        do {
            attempt++;

            const apiUrl = `${rootUrl}/jobs/imagine`;
            const response = await fetch(apiUrl, data);
            const result = await response.json();

            console.log(`${dateAsString()} ⁝ attempt #${attempt}, response`, { status: response.status, jobid: result.jobid, job_status: result.status, ind });

            switch (response.status) {
                case 201: // Created — job accepted
                    results.push({ status: response.status, jobid: result.jobid, job_status: result.status, ind, prompt });
                    retry = false;
                    break;
                case 429: // Channel at capacity or rate limited — wait and retry
                    console.log(`${dateAsString()} ⁝ #${ind} attempt #${attempt} channel busy, sleeping for ${sleepSecs} secs...`);
                    await sleep(sleepSecs * 1000);
                    break;
                case 596: // Pending moderation / CAPTCHA — resolve in Discord, then POST /accounts/{channel}/reset
                    console.error(`${dateAsString()} ⁝ #${ind} channel pending moderation/CAPTCHA — resolve in Discord, then POST /accounts/{channel}/reset`, result);
                    results.push({ status: response.status, jobid: undefined, job_status: 'moderated', ind, prompt });
                    retry = false;
                    break;
                default: // 400 / 401 / 402 / ...
                    console.error(`Unexpected response.status ${response.status}`, result);
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

        if (status == 596)
            console.warn(`channel moderation pending, skipping prompt`, prompt);

        if (status == 201 && jobid) {
            let attempt = 0;
            let retry = true;
            do {
                attempt++;

                const apiUrl = `${rootUrl}/jobs/${jobid}`;
                const response = await fetch(apiUrl, {
                    headers: {
                        "Authorization": `Bearer ${token}`,
                    },
                });
                const result = await response.json();

                console.log(`${dateAsString()} ⁝ attempt #${attempt}, response`, { status: response.status, jobid: result.jobid, job_status: result.status, ind });

                if (response.status == 200) {
                    switch (result.status) {
                        case 'completed':
                            // In v3 generated media is nested under result.response
                            if (result.response?.attachments?.length) {
                                downloadFile(result.response.attachments[0].url, ind);
                            } else {
                                console.error(`#${ind} completed jobid has no attachments`);
                            }
                            retry = false;
                            break;
                        case 'created':
                        case 'started':
                        case 'progress':
                            console.log(`${dateAsString()} ⁝ #${ind} attempt #${attempt} sleeping for ${sleepSecs} secs...`, result.status);
                            await sleep(sleepSecs * 1000);
                            break;
                        case 'moderated':
                        case 'failed':
                            console.error(`#${ind} job ${result.status}`, result.error || result);
                            retry = false;
                            break;
                        default:
                            console.error(`Unexpected job status`, result);
                            retry = false;
                            break;
                    }
                } else {
                    console.error(`Unexpected response.status ${response.status}`, result);
                    retry = false;
                }
            } while (retry);
        }
    }

    const executionTimeInMilliseconds = performance.now() - startTime;

    console.log(`${dateAsString()}  ⁝  total elapsed time ${new Date(executionTimeInMilliseconds).toISOString()}`);
}

main();
