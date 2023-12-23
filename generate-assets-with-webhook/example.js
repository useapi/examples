/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example-js.sh:

USEAPI_SERVER="…" USEAPI_CHANNEL="…" USEAPI_TOKEN="…" USEAPI_DISCORD="…" NGROK_AUTHTOKEN"…" node ./example.js

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
const discord = process.env.USEAPI_DISCORD;
const server = process.env.USEAPI_SERVER;
const channel = process.env.USEAPI_CHANNEL;

const inputFileName = './prompts.json';
// We will utilize all three available job slots for the Basic or Standard plan.
const maxJobs = 3;
// Provide additional prompt params
const withParams = ' --relax --s 750'; // --fast, --relax, --s all goes here
// Time to pause between Discord 429 calls
const sleepSecs = 5;
// API root url
const rootUrl = 'https://api.useapi.net/v2';

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
                    const job = JSON.parse(body);
                    const { prompt, status, replyRef, jobid, content, error, errorDetails, attachments } = job;
                    const url = attachments?.at(0)?.url;

                    console.log(`${dateAsString()} ⁝ webhook #${replyRef} ${jobid} ${status}`, content.substring(0, 20) + '…' + content.substring(content.length - 20));

                    results[replyRef] = { prompt, status, replyRef, jobid, content, error, errorDetails, url };

                    // Update number of running jobs
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

        // Detailed documentation at https://useapi.net/docs/api-v2/post-jobs-imagine     
        const data = {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                discord,
                server,
                channel,
                maxJobs,
                prompt: `${prompts[prompt_ind]} ${withParams}`.trim(),
                replyUrl: `${listener.url()}?ind=${prompt_ind}`,
                replyRef
            })
        };

        console.log(`${dateAsString()} ⁝ prompt #${prompt_ind}`, prompts[prompt_ind]);

        const apiUrl = `${rootUrl}/jobs/imagine`;

        const response = await fetch(apiUrl, data);

        const result = await response.json();

        const { prompt, status, jobid, content, error, errorDetails, executingJobs } = result;

        console.log(`${dateAsString()} ⁝ response #${prompt_ind} HTTP ${response.status}`, { status, jobid, prompt_ind, executingJobs });

        switch (response.status) {
            case 429:
                if (executingJobs)
                    // Query is full : exit and rely on webhook to call once job completed
                    return;
                else
                    // Discord reported 429 : sleep and try again                
                    await sleep(sleepSecs * 1000);
                break;
            case 200: // OK
            case 422: // Moderated                    
                results[replyRef] = { prompt, status, jobid, content, error, errorDetails };
                prompt_ind++;
                break;
            default:
                console.error(`Unexpected response.status`, result);
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