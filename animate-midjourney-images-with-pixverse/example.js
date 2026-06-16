/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example.sh:

USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" node ./example.js

Optional environment variables:
  MJ_CHANNEL      - pin Midjourney requests to a specific configured channel.
                    With the Midjourney API v3 this is optional — when omitted the
                    API auto-selects a configured channel with available capacity.
  PIXVERSE_EMAIL  - pin PixVerse requests to a specific configured account.
                    When omitted the API randomly selects an available account.

Pipeline: Midjourney imagine -> U1-U4 upscales -> PixVerse image-to-video (create-v4).

*/

import fs from 'fs';
import { promises as fsp } from "fs"
import { Readable } from 'stream';
import { finished } from 'stream/promises';
import ngrok from '@ngrok/ngrok';
import http from 'http';
import { AsyncFunctionQueue } from './query.js'

const prompts = JSON.parse(await fsp.readFile('./prompts.json', 'utf8'));

/*
  You can ⚙️ configure your
    👉 Midjourney account(s) https://useapi.net/docs/api-midjourney-v3/post-midjourney-accounts
    👉 PixVerse account(s)   https://useapi.net/docs/api-pixverse-v2/post-pixverse-accounts-email
  Once configured, MJ_CHANNEL / PIXVERSE_EMAIL are no longer needed and can be removed.
*/
const { USEAPI_TOKEN, MJ_CHANNEL, PIXVERSE_EMAIL } = process.env;

// Optional params to add at the end of the Midjourney prompt
const promptParams = ' --v 7 --s 250';

// Prompt that drives the PixVerse image-to-video animation
const pixverse_prompt = 'smiling and blinking';

// PixVerse video options, see https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create-v4
const pixverse_model = 'v6';
const pixverse_duration = 5;
const pixverse_quality = '720p';

const data = {};

// Track number of successfully submitted jobs
let submitted = 0;

const rootMidjourneyUrl = 'https://api.useapi.net/v3/midjourney';
const rootPixVerseUrl = 'https://api.useapi.net/v2/pixverse';

const sleep = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));

const dateAsString = () => new Date().toISOString();

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

const contentTypeForFile = (fileName) => {
    const ext = (fileName.split('.').pop() || '').toLowerCase();
    return ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' })[ext] || 'image/png';
}

const downloadFile = async (url, prefix) => {
    const { body } = await fetch(url);
    const localPath = `./${prefix}-${getFileNameFromUrl(url)}`;
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

                    // PixVerse callbacks carry a video_id (and no Midjourney verb)
                    if (job.video_id) {
                        const replyRef = job.replyRef ?? job.response?.replyRef;
                        const completed = job.video_status_final === true || job.video_status_name === 'COMPLETED';
                        const failed = job.video_status_name === 'FAILED' || job.errCode;

                        console.log(`${dateAsString()} ⁝ webhook pixverse ${job.video_id} ${job.video_status_name ?? job.video_status}`);

                        const node = findByJobid(replyRef);
                        if (node) {
                            node.pixverse = { ...node.pixverse, video_id: job.video_id, status: job.video_status_name, url: job.url };

                            if (completed && job.url)
                                node.pixverse.videoFileName = await downloadFile(job.url, `${replyRef}-pixverse`);

                            if (completed || failed)
                                node.completed = true;

                            saveToFile('./result.json', data);
                        }

                        res.end('ok');
                        return;
                    }

                    // Midjourney callbacks — generated media is nested under job.response
                    const status = job.status;
                    const jobid = job.jobid;
                    const response = job.response ?? {};
                    const content = response.content ?? '';
                    const url = response.attachments?.at(0)?.url;

                    console.log(`${dateAsString()} ⁝ webhook ${jobid} ${job.verb} ${status}`, content ? content.substring(0, 20) + '…' + content.substring(content.length - 20) : '');

                    switch (status) {
                        case 'completed':
                        case 'moderated':
                        case 'failed':
                        case 'cancelled': {
                            const node = findByJobid(jobid);
                            if (!node) break;

                            node.status = status;
                            node.content = content;

                            if (status == 'completed') {
                                if (node.buttons) {
                                    // This is an imagine job — kick off the U1-U4 upscales
                                    for (let button in node.buttons)
                                        if (button[0] != '_')
                                            queueMidjourney.enqueue(post_midjourney_button, button, jobid, node.buttons[button]);
                                } else if (url) {
                                    // This is an upscale (U1-U4) leaf — download it and animate via PixVerse
                                    node.imageFileName = await downloadFile(url, jobid);
                                    queuePixVerse.enqueue(post_pixverse, node);
                                } else {
                                    // Completed leaf with no image — nothing to animate
                                    node.completed = true;
                                }
                            } else {
                                // Moderated / failed / cancelled — nothing downstream to run
                                if (node.buttons)
                                    for (let button in node.buttons)
                                        if (button[0] != '_')
                                            node.buttons[button].completed = true;
                                else
                                    node.completed = true;
                            }

                            saveToFile('./result.json', data);
                            break;
                        }
                    }

                    res.end('ok');
                });
                break;
        }
    })
    .listen(8081);

const submit_payload = async (url, payload, params, isPixVerse = false) => {
    while (true) {
        let response;
        let retryCount = 0;
        // On a slow connection fetch may fail to POST a large payload, so retry up to 3 times
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

        const { jobid, video_id, status, error, errorDetails, verb } = job;

        console.log(`${dateAsString()} ⁝ #${submitted} ${verb ?? url} (${params.prompt ?? pixverse_prompt}) HTTP ${response.status}`, { status, jobid, video_id, error, errorDetails });

        // 429 — Midjourney channel busy / all PixVerse accounts at capacity: wait and retry
        if (response.status === 429) {
            await sleep(10 * 1000);
            continue;
        }

        if (isPixVerse) {
            params.pixverse = { ...params.pixverse, video_id, status, code: response.status, error: error ?? errorDetails };
            // If PixVerse failed to accept the job, don't wait on a webhook that won't arrive
            if (response.status !== 200 && params.completed === false)
                params.completed = true;
        } else {
            params.jobid = jobid;
            params.status = status;
            params.error = error;
            params.errorDetails = errorDetails;
            params.code = response.status;
            // Midjourney rejected the job (e.g. 596 moderation / 4xx) — mark downstream done so we don't hang
            if (response.status !== 201) {
                if (params.buttons)
                    for (let b in params.buttons)
                        if (b[0] != '_')
                            params.buttons[b].completed = true;
                else if (params.completed === false)
                    params.completed = true;
            }
        }

        saveToFile('./result.json', data);
        submitted++;
        return job;
    }
}

/*
  Execute a Midjourney API v3 jobs/button, see https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-button
  button param may have a unique suffix after '-' which is stripped before sending.
*/
const post_midjourney_button = async (button, parent_jobid, params) => {
    // V1-xx -> V1
    button = button.split('-')[0];

    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            jobId: parent_jobid,
            button,
            stream: false, // stream defaults to true (SSE); set false to get the immediate JSON job state
            replyUrl: listener.url()
        })
    };

    return await submit_payload(`${rootMidjourneyUrl}/jobs/button`, payload, params);
}

/*
  Animate an image with PixVerse image-to-video (create-v4):
    1. Upload the local image via POST /files          -> returns result[0].path
    2. POST /videos/create with first_frame_path + prompt (i2v) + replyUrl
  See https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create-v4
  params.jobid (the upscale job id) is passed as replyRef so the PixVerse
  callback can be mapped back to this node via findByJobid().
*/
const post_pixverse = async (params) => {
    const { imageFileName, jobid } = params;

    if (!imageFileName) {
        params.completed = true;
        return;
    }

    // 1. Upload the image (raw bytes)
    const uploadUrl = `${rootPixVerseUrl}/files/` + (PIXVERSE_EMAIL ? `?email=${encodeURIComponent(PIXVERSE_EMAIL)}` : '');
    const uploadResponse = await fetch(uploadUrl, {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`,
            'Content-Type': contentTypeForFile(imageFileName)
        },
        body: await fsp.readFile(imageFileName)
    });
    const uploaded = await uploadResponse.json();
    const path = uploaded?.result?.at(0)?.path;

    console.log(`${dateAsString()} ⁝ pixverse upload HTTP ${uploadResponse.status}`, { path });

    if (!path) {
        console.error(`pixverse upload failed`, uploaded);
        params.completed = true;
        params.pixverse = { ...params.pixverse, upload: uploaded, code: uploadResponse.status };
        saveToFile('./result.json', data);
        return;
    }

    // 2. Create the image-to-video job
    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            model: pixverse_model,
            prompt: pixverse_prompt,
            first_frame_path: path,
            duration: pixverse_duration,
            quality: pixverse_quality,
            email: PIXVERSE_EMAIL || undefined,
            replyUrl: listener.url(),
            replyRef: jobid // map the PixVerse callback back to this node
        })
    };

    return await submit_payload(`${rootPixVerseUrl}/videos/create`, payload, params, true);
}

/*
  Execute a Midjourney API v3 jobs/imagine, see https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
  verb param may have a unique suffix after '-' which is stripped before sending.
*/
const post_midjourney = async (verb, params) => {
    let { prompt } = params;

    if (prompt)
        prompt = prompt + promptParams;

    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            stream: false, // stream defaults to true (SSE); set false to get the immediate JSON job state
            channel: MJ_CHANNEL || undefined, // optional in v3
            prompt,
            replyUrl: listener.url()
        })
    };

    // imagine-xx -> imagine
    verb = verb.split('-')[0];

    return await submit_payload(`${rootMidjourneyUrl}/jobs/${verb}`, payload, params);
}

const startTime = performance.now();

// Two queues: one for Midjourney, one for PixVerse
const queueMidjourney = new AsyncFunctionQueue();
const queuePixVerse = new AsyncFunctionQueue();

// data holds every job to execute and tracks progress via the completed field (on the U1-U4 leaves).
for (let ind = 0; ind < prompts.length; ind++) {
    data[`imagine-` + ind] = {
        jobid: null,
        prompt: prompts[ind],
        buttons: {
            U1: { jobid: null, completed: false },
            U2: { jobid: null, completed: false },
            U3: { jobid: null, completed: false },
            U4: { jobid: null, completed: false },
        }
    }
}

saveToFile('./result.json', data);

// Start submitting imagine jobs
Object.keys(data).forEach(key => queueMidjourney.enqueue(post_midjourney, key, data[key]));

while (hasMoreJobsToRun() != null)
    await sleep(10 * 1000);

console.log(`${dateAsString()} ⁝ total elapsed time ${new Date(performance.now() - startTime).toISOString()}`);

process.exit();
