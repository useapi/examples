/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example.sh:

USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" node ./example.js

USEAPI_CHANNEL is optional with the Midjourney API v3 — when omitted the API
automatically selects a configured channel with available capacity.

*/

import fs from 'fs';
import util from 'util';
import { Readable } from 'stream';
import { finished } from 'stream/promises';
import ngrok from '@ngrok/ngrok';
import http from 'http';
import { AsyncFunctionQueue } from './query.js'

const results = {};

let prompt_ind = 0;
let webhook_ind = 0;

const token = process.env.USEAPI_TOKEN;
const channel = process.env.USEAPI_CHANNEL; // optional in v3

const inputFileName = './prompts.json';
// Provide additional prompt params
const withParams = ' --relax'; // --fast, --relax, --s all goes here
// Time to pause between 429 (channel busy) retries
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

// https://github.com/ngrok/ngrok-javascript
const listener = await ngrok.forward({ addr: 8081, authtoken_from_env: true });

console.info('Webhook', listener.url());

// Create server to receive webhook events
http
    .createServer(function (req, res) {
        switch (req.method) {
            case 'GET':
                res.writeHead(200);
                res.write("Hello from ngrok server");
                res.end();
                break;
            case 'POST':
                let body = '';
                req.on('data', chunk => body += chunk.toString());
                req.on('end', () => {
                    // v3 callback body has the same JSON shape as GET /jobs/{jobid}
                    const job = JSON.parse(body);
                    const status = job.status;
                    const jobid = job.jobid;
                    const replyRef = job.request?.replyRef ?? job.response?.replyRef ?? job.replyRef;
                    const content = job.response?.content ?? '';
                    // In v3 generated media is nested under job.response
                    const url = job.response?.attachments?.at(0)?.url;

                    console.log(`${dateAsString()} ⁝ webhook #${replyRef} ${jobid} ${status}`, content ? content.substring(0, 20) + '…' + content.substring(content.length - 20) : '');

                    results[replyRef] = job;

                    // On a terminal state we can download (if any) and start another prompt
                    switch (status) {
                        case 'completed':
                        case 'moderated':
                        case 'failed':
                        case 'cancelled':
                            if (url)
                                downloadFile(url, +replyRef);
                            queue.enqueue(submit);
                            webhook_ind++;
                            break;
                    }

                    res.end('ok');
                });
                break;
        }
    })
    .listen(8081);

const prompts = await loadFromFile(inputFileName);

console.log(`${dateAsString()} ⁝ prompts to process`, prompts.length);

const submit = async () => {
    while (prompt_ind < prompts.length) {
        const replyRef = `${prompt_ind}`;

        // Detailed documentation at https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
        const data = {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                stream: false, // stream defaults to true (SSE); set false to get the immediate JSON job state
                channel: channel || undefined, // optional — API auto-selects a channel with capacity when omitted
                prompt: `${prompts[prompt_ind]} ${withParams}`.trim(),
                replyUrl: `${listener.url()}?ind=${prompt_ind}`,
                replyRef
            })
        };

        console.log(`${dateAsString()} ⁝ prompt #${prompt_ind}`, prompts[prompt_ind]);

        const apiUrl = `${rootUrl}/jobs/imagine`;

        const response = await fetch(apiUrl, data);

        const job = await response.json();

        const { status, jobid } = job;

        console.log(`${dateAsString()} ⁝ response #${prompt_ind} HTTP ${response.status}`, { status, jobid });

        switch (response.status) {
            case 201: // Created — job accepted; its terminal state will arrive via webhook
                results[replyRef] = job;
                prompt_ind++;
                break;
            case 429: // Channel at capacity or rate limited
                if (prompt_ind - webhook_ind > 0)
                    // Jobs in flight: stop submitting and let the webhook resume us on completion
                    return;
                else
                    // Nothing in flight: wait and try again
                    await sleep(sleepSecs * 1000);
                break;
            case 596: // Channel pending moderation/CAPTCHA — resolve in Discord, then POST /accounts/{channel}/reset
                console.error(`${dateAsString()} ⁝ #${prompt_ind} channel pending moderation/CAPTCHA — resolve in Discord, then POST /accounts/{channel}/reset`, job);
                results[replyRef] = job;
                webhook_ind++; // no webhook will arrive for this prompt
                prompt_ind++;
                break;
            default:
                console.error(`Unexpected response.status`, job);
                webhook_ind++; // no webhook will arrive for this prompt
                prompt_ind++;
                break;
        }
    }
    // Persist results to the file for debugging purposes
    saveToFile('./result.json', results);
}

const startTime = performance.now();

const queue = new AsyncFunctionQueue();

// Start submitting prompts
queue.enqueue(submit);

while (webhook_ind < prompts.length)
    await sleep(10 * 1000);

console.log(`${dateAsString()} ⁝ total elapsed time ${new Date(performance.now() - startTime).toISOString()}`);

process.exit();
