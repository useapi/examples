/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example.sh:

USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" node ./example.js

*/

import fs from 'fs';
import { promises as fsp } from "fs"
import { Readable } from 'stream';
import { finished } from 'stream/promises';
import ngrok from '@ngrok/ngrok';
import http from 'http';
import { AsyncFunctionQueue } from './query.js'
import prompts from './prompts.json' assert { type: 'json' };

// console.log('process.env', process.env);

/* 
  Configure your
    ✔️ Midjourney account https://useapi.net/docs/api-v2/post-account-midjourney-channel
    ✔️ InsightFaceSwap account https://useapi.net/docs/api-faceswap-v1/post-faceswap-account-channel
    ✔️ Pika accounts https://useapi.net/docs/api-pika-v1/post-pika-account-channel
*/
const { USEAPI_TOKEN } = process.env;

// Optional params to add at the end of the prompt
const promptParams = ' --v 6 --s 900';

// Prompt for Pika animation, see https://pikalabsai.org/pika-labs-commands-and-parameters/
const pika_prompt = 'smiling and blinking';

const data = {};

// Track number of successfully submitted jobs
let submitted = 0;

const rootMidjourneyUrl = 'https://api.useapi.net/v2';
const rootPikaUrl = 'https://api.useapi.net/v1/pika';
const rootFaceSwap = 'https://api.useapi.net/v1/faceswap';

// Source image (face), change to any other file name of your choice
const sourceFileName = './source.jpg';

const sleep = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));

const dateAsString = () => new Date().toISOString();

const saveToFile = async (filePath, data) => {
    await fs.writeFile(filePath, JSON.stringify(data, null, 2), (err) => {
        if (err)
            console.error('Error writing to file:', err);
    });
}

// Extract png from https://cdn.discordapp.com/attachments/server_id/channed_id/filename.png?ex=
const getFileExtensionFromUrl = (url) => {
    const matches = url.match(/\.([^.?]+)(?=\?|$)/);
    return matches ? matches[1] : '';
}

const downloadFile = async (url, fileName) => {
    const { body } = await fetch(url);
    const localPath = `./${fileName}.${getFileExtensionFromUrl(url)}`;
    const stream = fs.createWriteStream(localPath);
    await finished(Readable.fromWeb(body).pipe(stream));
    return localPath;
};

const findByJobid = (jobid, jsonObject = data) => {
    let foundObject = null;

    const traverse = (node) => {
        if (foundObject) return;
        if (node.jobid === jobid) {
            foundObject = node;
            return;
        }
        if (node.buttons && typeof node.buttons === 'object')
            for (let key in node.buttons)
                if (key[0] != '_')
                    traverse(node.buttons[key]);
    }

    Object.values(jsonObject).forEach(obj => traverse(obj));

    return foundObject;
}

const hasMoreJobsToRun = (jsonObject = data) => {
    let foundObject = null;

    const traverse = (node) => {
        if (foundObject) return;

        if (node.completed === false) {
            foundObject = node;
            return;
        }

        if (node.buttons && typeof node.buttons === 'object')
            for (let key in node.buttons)
                if (key[0] != '_')
                    traverse(node.buttons[key]);
    }

    Object.values(jsonObject).forEach(obj => traverse(obj));

    return foundObject;
}

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
                req.on('end', async () => {
                    const job = JSON.parse(body);
                    const { status, jobid, content, attachments, button, replyRef } = job;

                    console.log(`${dateAsString()} ⁝ webhook ${jobid} ${job.verb} ${status}`, content?.substring(0, 10) + '…');

                    // Midjourney
                    if (['imagine', 'describe', 'blend', 'button'].includes(job.verb)) {
                        switch (status) {
                            case 'completed':
                            case 'moderated':
                            case 'failed':
                            case 'cancelled':
                                const node = findByJobid(jobid);

                                node.status = status;
                                node.content = content;

                                let _submitted = false;

                                if (status == 'completed') {
                                    if (node.buttons)
                                        for (let button in node.buttons)
                                            if (button[0] != '_') {
                                                queueMidjourney.enqueue(post_midjourney_button, button, jobid, node.buttons[button]);
                                                _submitted++;
                                            }

                                    // Save upscaled file and start Pika generation(s)
                                    if (['U1', 'U2', 'U3', 'U4'].includes(button) && attachments?.at(0)?.url?.length) {
                                        const fileName = `${jobid.split('-')[0]}-${button}`;
                                        node.targetFileName = await downloadFile(attachments[0].url, fileName);

                                        // Swap face 
                                        queueFaceSwap.enqueue(post_faceswap, 'swap', node);
                                    }
                                } else {
                                    node.faceswap = 'skipping';
                                    node.pika = 'skipping';
                                }

                                saveToFile('./result.json', data);

                                if (!_submitted)
                                    queueMidjourney.enqueue(console.log, `👉 ${job.verb} ${jobid} ${status} ${content?.substring(0, 10) + '…'}`);

                                break;
                        }
                    }

                    // FaceSwap
                    if (['faceswap-swap'].includes(job.verb)) {
                        switch (status) {
                            case 'completed':
                            case 'failed':
                                const node = findByJobid(replyRef);

                                node.faceswap = { ...node.faceswap, status, content };

                                if (status == 'completed' && attachments?.at(0)?.url?.length) {
                                    const fileName = `${replyRef.split('-')[0]}-faceswap`;
                                    node.imageFileName = await downloadFile(attachments[0].url, fileName);
                                } else {
                                    // We can still animate originally unscaled image
                                    node.imageFileName = node.targetFileName;
                                }

                                saveToFile('./result.json', data);

                                queuePika.enqueue(post_pika, 'animate', node);
                        }
                    }

                    // Pika
                    if (['pika-create', 'pika-animate'].includes(job.verb)) {
                        switch (status) {
                            case 'completed':
                            case 'moderated':
                            case 'failed':
                            case 'cancelled':
                                const node = findByJobid(replyRef);

                                node.completed = true;

                                node.pika = { ...node.pika, status, content };

                                if (attachments?.at(0)?.url?.length) {
                                    const fileName = `${replyRef.split('-')[0]}-animated`;
                                    node.pika.imageFileName = await downloadFile(attachments[0].url, fileName);
                                }

                                saveToFile('./result.json', data);

                                queuePika.enqueue(console.log, `👉 ${job.verb} ${jobid} ${status} ${content?.substring(0, 10) + '…'}`);
                        }
                    }

                    res.end('ok');
                });
                break;
        }
    })
    .listen(8081);

const submit_payload = async (url, payload, params) => {
    let response;
    let retryCount = 0;

    // When using slow connection fetch may fail to POST large payload so we will retry up to 3 times
    while (retryCount < 3)
        try {
            response = await fetch(url, payload);
            break;
        } catch (ex) {
            console.error(`fetch ${url} failed #${retryCount}`, ex);
            if (retryCount > 1)
                throw ex;
            retryCount++;
        }

    const job = await response.json();

    const { jobid, status, error, errorDetails, verb, button, executingJobs, attachments } = job;

    console.log(`${dateAsString()} ⁝ #${submitted} ${verb ?? url} ${button ?? ''} (${params.prompt ?? ''}) HTTP ${response.status}`, { status, jobid, error, errorDetails });

    switch (response.status) {
        // Query is full, retry again later once one of running jobs complete
        case 429:
            if (url.includes('faceswap') || executingJobs)
                return 'full';
            else {
                // We got rate-limited 429 from Discord, let's play safe and sleep for 10 or so seconds before trying again
                await sleep(10 * 1000);
                return 'retry';
            }
        case 504:
            if (!url.includes('faceswap')) {
                // Query overflow (should never happen unless maxJobs misconfigured)          
                console.error('504 query overflow detected, sleeping for 3 minutes to allow already running jobs complete');

                await sleep(3 * 60 * 1000);
            }
        default:
            if (url.includes('faceswap'))
                params.faceswap = { jobid, status, error, errorDetails, attachments, code: response.status };
            else if (url.includes('pika')) {
                params.pika = { jobid, status, error, errorDetails, code: response.status };
            } else {
                params.jobid = jobid;
                params.status = status;
                params.error = error;
                params.errorDetails = errorDetails;
                params.code = response.status;
            }

            // Mark as completed if error occurred
            if (error && params.completed === false)
                params.completed = true;

            saveToFile('./result.json', data);

            submitted++;
    }
}

/*
  Use this function to execute any of API v2 Midjourney jobs/button, see https://useapi.net/docs/api-v2/post-jobs-button
  button param may have unique id after which will be removed.
  Examples:
    post_midjourney_button('U1', <parent_jobid>, {} );
    post_midjourney_button('V3-456', <parent_jobid>, { prompt: 'color it red' } );
*/
const post_midjourney_button = async (button, parent_jobid, params) => {
    let { prompt } = params;

    if (prompt)
        prompt = prompt + promptParams;

    // V1-xx -> V1
    button = button.split('-')[0];

    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            jobid: parent_jobid,
            button,
            prompt,
            replyUrl: listener.url()
        })
    };

    return await submit_payload(`${rootMidjourneyUrl}/jobs/button`, payload, params);
}

/*
  Use this function to execute any of API v1 Pika jobs, see https://useapi.net/docs/api-pika-v1.
  verb param may have unique id after which will be removed.
  Examples:
    post_pika('create', { pika_prompt: 'dancing cat in the hat' } );
    post_pika('create-123', { pika_prompt: 'dancing cat in the hat' } );
    post_pika('animate', { imageFileName: './starting-image.jpg', pika_prompt: 'dancing cat in the hat' } );
    post_pika('animate-456', { imageFileName: './starting-image.jpg'pika_prompt } );
*/
const post_pika = async (verb, params) => {
    let { imageFileName, pika_prompt, jobid } = params;

    const formData = new FormData();

    if (jobid)
        formData.append('replyRef', jobid);

    formData.append('replyUrl', listener.url());

    if (pika_prompt)
        formData.append('prompt', pika_prompt);

    if (imageFileName)
        formData.append('image', new Blob([await fsp.readFile(imageFileName)]));

    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`
        },
        body: formData
    };

    // animate-xx -> animate
    verb = verb.split('-')[0];

    return await submit_payload(`${rootPikaUrl}/${verb}`, payload, params);
}

// https://useapi.net/docs/api-faceswap-v1/post-faceswap-swap
const post_faceswap = async (verb, params) => {
    let { sourceFileName, targetFileName, jobid } = params;

    const formData = new FormData();

    formData.append('replyUrl', listener.url());

    if (jobid)
        formData.append('replyRef', jobid);

    if (sourceFileName)
        formData.append('saveid_image', new Blob([await fsp.readFile(sourceFileName)]));

    if (targetFileName)
        formData.append('swapid_image', new Blob([await fsp.readFile(targetFileName)]));

    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`
        },
        body: formData
    };

    // swap-xx -> swap
    verb = verb.split('-')[0];

    return await submit_payload(`${rootFaceSwap}/${verb}`, payload, params);
}

/*
  Use this function to execute any of API v2 Midjourney jobs, see https://useapi.net/docs/api-v2.
  verb param may have unique id after which will be removed.
  Examples:
    post_midjourney('imagine', { prompt: 'cat in the hat' } );
    post_midjourney('imagine-456', { prompt: 'cat in the hat' } );
    post_midjourney('blend', { blendUrls: ['https://url.to.blend.1','https://url.to.blend2'] } );
    post_midjourney('blend-123', { blendUrls: ['https://url.to.blend.1','https://url.to.blend2'] } );
    post_midjourney('describe', { describeUrl: 'https://url.to.describe' } );
    post_midjourney('describe-345', { describeUrl: 'https://url.to.describe' } );
*/
const post_midjourney = async (verb, params) => {
    // Known params for jobs/imagine, jobs/blend and jobs/describe
    let { prompt, blendUrls, blendDimensions, describeUrl } = params;

    if (prompt)
        prompt = prompt + promptParams;

    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            prompt,
            blendUrls,
            blendDimensions,
            describeUrl,
            replyUrl: listener.url()
        })
    };

    // imagine-xx -> imagine
    verb = verb.split('-')[0];

    return await submit_payload(`${rootMidjourneyUrl}/jobs/${verb}`, payload, params);
}

const startTime = performance.now();

// We need queries for Midjourney, FaceSwap and Pika
const queueMidjourney = new AsyncFunctionQueue();
const queueFaceSwap = new AsyncFunctionQueue();
const queuePika = new AsyncFunctionQueue();

// We will use data variable to hold list of all jobs to execute and to track progress via completed field (where applicable).
for (let ind = 0; ind < prompts.length; ind++) {
    data[`imagine-` + ind] = {
        jobid: null,
        prompt: prompts[ind],
        buttons: {
            U1: { jobid: null, completed: false, sourceFileName, pika_prompt },
            U2: { jobid: null, completed: false, sourceFileName, pika_prompt },
            U3: { jobid: null, completed: false, sourceFileName, pika_prompt },
            U4: { jobid: null, completed: false, sourceFileName, pika_prompt },
        }
    }
}

saveToFile('./result.json', data);

// Start querying from the top level
Object.keys(data).forEach(key => queueMidjourney.enqueue(post_midjourney, key, data[key]));

while (hasMoreJobsToRun() != null)
    await sleep(10 * 1000);

console.log(`${dateAsString()} ⁝ total elapsed time ${new Date(performance.now() - startTime).toISOString()}`);

process.exit();