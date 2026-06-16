/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example.sh:

USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" node ./example.js

Pipeline: Midjourney imagine -> U1-U4 upscales -> InsightFaceSwap face swap -> PixVerse image-to-video.

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
  Configure your
    ✔️ Midjourney account     https://useapi.net/docs/api-midjourney-v3/post-midjourney-accounts
    ✔️ InsightFaceSwap account https://useapi.net/docs/api-faceswap-v1/post-faceswap-account-channel
    ✔️ PixVerse account        https://useapi.net/docs/api-pixverse-v2/post-pixverse-accounts-email
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
const rootFaceSwap = 'https://api.useapi.net/v1/faceswap';
const rootPixVerseUrl = 'https://api.useapi.net/v2/pixverse';

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

// Extract png from https://cdn.discordapp.com/attachments/server_id/channel_id/filename.png?ex=
const getFileExtensionFromUrl = (url) => {
    const matches = url.match(/\.([^.?]+)(?=\?|$)/);
    return matches ? matches[1] : '';
}

const contentTypeForFile = (fileName) => {
    const ext = (fileName.split('.').pop() || '').toLowerCase();
    return ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' })[ext] || 'image/png';
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

                    // ---- PixVerse callbacks carry a video_id (and no Midjourney verb) ----
                    if (job.video_id) {
                        const replyRef = job.replyRef ?? job.response?.replyRef;
                        const completed = job.video_status_final === true || job.video_status_name === 'COMPLETED';
                        const failed = job.video_status_name === 'FAILED' || job.errCode;

                        console.log(`${dateAsString()} ⁝ webhook pixverse ${job.video_id} ${job.video_status_name ?? job.video_status}`);

                        const node = findByJobid(replyRef);
                        if (node) {
                            node.pixverse = { ...node.pixverse, video_id: job.video_id, status: job.video_status_name, url: job.url };
                            if (completed && job.url)
                                node.pixverse.videoFileName = await downloadFile(job.url, `${`${replyRef}`.split('-')[0]}-animated`);
                            if (completed || failed)
                                node.completed = true;
                            saveToFile('./result.json', data);
                        }

                        res.end('ok');
                        return;
                    }

                    // ---- InsightFaceSwap (v1) callbacks — verb 'faceswap-swap', media at top level ----
                    if (typeof job.verb === 'string' && job.verb.startsWith('faceswap')) {
                        const { status, content, attachments, replyRef } = job;

                        console.log(`${dateAsString()} ⁝ webhook faceswap ${replyRef} ${status}`, content?.substring(0, 10) + '…');

                        if (['completed', 'failed', 'moderated', 'cancelled'].includes(status)) {
                            const node = findByJobid(replyRef);
                            if (node) {
                                node.faceswap = { ...node.faceswap, status, content };

                                if (status == 'completed' && attachments?.at(0)?.url?.length)
                                    node.imageFileName = await downloadFile(attachments[0].url, `${`${replyRef}`.split('-')[0]}-faceswap`);
                                else
                                    // Face swap failed — animate the original upscale instead
                                    node.imageFileName = node.targetFileName;

                                saveToFile('./result.json', data);

                                queuePixVerse.enqueue(post_pixverse, node);
                            }
                        }

                        res.end('ok');
                        return;
                    }

                    // ---- Midjourney (v3) callbacks — media nested under job.response ----
                    const status = job.status;
                    const jobid = job.jobid;
                    const response = job.response ?? {};
                    const content = response.content ?? '';
                    const url = response.attachments?.at(0)?.url;

                    console.log(`${dateAsString()} ⁝ webhook ${jobid} ${job.verb} ${status}`, content ? content.substring(0, 10) + '…' : '');

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
                                    // imagine job — kick off the U1-U4 upscales
                                    for (let button in node.buttons)
                                        if (button[0] != '_')
                                            queueMidjourney.enqueue(post_midjourney_button, button, jobid, node.buttons[button]);
                                } else if (url) {
                                    // upscale (U1-U4) leaf — download the target image, then swap the face
                                    node.targetFileName = await downloadFile(url, `${`${jobid}`.split('-')[0]}-target`);
                                    queueFaceSwap.enqueue(post_faceswap, node);
                                } else {
                                    // Completed leaf with no image — nothing downstream to run
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

const submit_payload = async (url, payload, params, kind) => {
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

        // 429 — busy / at capacity: wait and retry
        if (response.status === 429) {
            await sleep(10 * 1000);
            continue;
        }

        if (kind === 'pixverse') {
            params.pixverse = { ...params.pixverse, video_id, status, code: response.status, error: error ?? errorDetails };
            if (response.status !== 200 && params.completed === false)
                params.completed = true; // no webhook will arrive
        } else if (kind === 'faceswap') {
            params.faceswap = { ...params.faceswap, jobid, status, code: response.status, error: error ?? errorDetails };
            // Fallback handled by the caller when no faceswap jobid is returned
        } else { // midjourney
            params.jobid = jobid;
            params.status = status;
            params.error = error;
            params.errorDetails = errorDetails;
            params.code = response.status;
            if (response.status !== 201) {
                // Midjourney rejected the job — don't hang waiting on a webhook
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

// Execute a Midjourney API v3 jobs/button, see https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-button
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

    return await submit_payload(`${rootMidjourneyUrl}/jobs/button`, payload, params, 'midjourney');
}

// Swap a source face onto the Midjourney target image with InsightFaceSwap v1.
// See https://useapi.net/docs/api-faceswap-v1/post-faceswap-swap
const post_faceswap = async (params) => {
    const { sourceFileName, targetFileName, jobid } = params;

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
        headers: { 'Authorization': `Bearer ${USEAPI_TOKEN}` },
        body: formData
    };

    const job = await submit_payload(`${rootFaceSwap}/swap`, payload, params, 'faceswap');

    // If face swap did not start, fall back to animating the original upscale
    if (!job?.jobid) {
        params.imageFileName = targetFileName;
        queuePixVerse.enqueue(post_pixverse, params);
    }
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
            replyRef: jobid
        })
    };

    return await submit_payload(`${rootPixVerseUrl}/videos/create`, payload, params, 'pixverse');
}

// Execute a Midjourney API v3 jobs/imagine, see https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
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

    return await submit_payload(`${rootMidjourneyUrl}/jobs/${verb}`, payload, params, 'midjourney');
}

const startTime = performance.now();

// Queues for Midjourney, FaceSwap and PixVerse
const queueMidjourney = new AsyncFunctionQueue();
const queueFaceSwap = new AsyncFunctionQueue();
const queuePixVerse = new AsyncFunctionQueue();

// data holds every job to execute and tracks progress via the completed field (on the U1-U4 leaves).
for (let ind = 0; ind < prompts.length; ind++) {
    data[`imagine-` + ind] = {
        jobid: null,
        prompt: prompts[ind],
        buttons: {
            U1: { jobid: null, completed: false, sourceFileName },
            U2: { jobid: null, completed: false, sourceFileName },
            U3: { jobid: null, completed: false, sourceFileName },
            U4: { jobid: null, completed: false, sourceFileName },
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
