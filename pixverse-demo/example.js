/*

Requires Node.js 18.x to be installed.

Create an example.sh file with the following content and execute it from the command line using ./example.sh:

USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" node ./example.js

Optional environment variables:
  PIXVERSE_EMAIL  - pin PixVerse requests to a specific configured account.
                    When omitted the API randomly selects an available account.

Supports both text-to-video (t2v) and image-to-video (i2v).
Set use_source_image: true in a prompts.json entry to upload ./source.jpg as the first frame.

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
    👉 PixVerse account(s) https://useapi.net/docs/api-pixverse-v2/post-pixverse-accounts-email
  Once configured, PIXVERSE_EMAIL is no longer needed and can be removed.
*/
const { USEAPI_TOKEN, PIXVERSE_EMAIL } = process.env;

const rootPixVerseUrl = 'https://api.useapi.net/v2/pixverse';

const data = {};

// Track number of successfully submitted jobs
let submitted = 0;

const sleep = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));

const dateAsString = () => new Date().toISOString();

const saveToFile = async (filePath, data) => {
    await fs.writeFile(filePath, JSON.stringify(data, null, 2), (err) => {
        if (err)
            console.error('Error writing to file:', err);
    });
}

const getFileNameFromUrl = (url) => {
    // Extract filename from https://…/filename.mp4?…
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
    }

    Object.values(jsonObject).forEach(obj => traverse(obj));

    return foundObject;
}

const hasMoreJobsToRun = (jsonObject = data) => {
    return Object.values(jsonObject).find(node => node.completed === false) ?? null;
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

                    // PixVerse v2 callbacks carry a video_id
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
                    }

                    res.end('ok');
                });
                break;
        }
    })
    .listen(8081);

const submit_payload = async (url, payload, params) => {
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

        const { video_id, status, error, errorDetails } = job;

        console.log(`${dateAsString()} ⁝ #${submitted} ${url} (${params.prompt ?? ''}) HTTP ${response.status}`, { status, video_id, error, errorDetails });

        // 429 — all PixVerse accounts at capacity: wait and retry
        if (response.status === 429) {
            await sleep(10 * 1000);
            continue;
        }

        params.pixverse = { ...params.pixverse, video_id, status, code: response.status, error: error ?? errorDetails };
        // If PixVerse failed to accept the job, don't wait on a webhook that won't arrive
        if (response.status !== 200 && params.completed === false)
            params.completed = true;

        saveToFile('./result.json', data);
        submitted++;
        return job;
    }
}

/*
  Upload a local image and create a PixVerse v2 video.
  If the prompt entry has use_source_image: true, uploads ./source.jpg first → i2v.
  Otherwise submits text-to-video (t2v) without first_frame_path.
  See https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create
*/
const post_pixverse = async (params) => {
    const { jobid, prompt, model, duration, quality, use_source_image } = params;

    let first_frame_path;

    if (use_source_image) {
        // 1. Upload the source image (raw bytes)
        const uploadUrl = `${rootPixVerseUrl}/files` + (PIXVERSE_EMAIL ? `?email=${encodeURIComponent(PIXVERSE_EMAIL)}` : '');
        const uploadResponse = await fetch(uploadUrl, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${USEAPI_TOKEN}`,
                'Content-Type': contentTypeForFile('./source.jpg')
            },
            body: await fsp.readFile('./source.jpg')
        });
        const uploaded = await uploadResponse.json();
        first_frame_path = uploaded?.result?.at(0)?.path;

        console.log(`${dateAsString()} ⁝ pixverse upload HTTP ${uploadResponse.status}`, { first_frame_path });

        if (!first_frame_path) {
            console.error(`pixverse upload failed`, uploaded);
            params.completed = true;
            params.pixverse = { ...params.pixverse, upload: uploaded, code: uploadResponse.status };
            saveToFile('./result.json', data);
            return;
        }
    }

    // 2. Create the video (t2v or i2v)
    const body = {
        model: model ?? 'v6',
        prompt,
        duration: duration ?? 5,
        quality: quality ?? '720p',
        replyUrl: listener.url(),
        replyRef: jobid // map the PixVerse callback back to this node
    };

    if (first_frame_path)
        body.first_frame_path = first_frame_path;

    if (PIXVERSE_EMAIL)
        body.email = PIXVERSE_EMAIL;

    const payload = {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${USEAPI_TOKEN}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(body)
    };

    return await submit_payload(`${rootPixVerseUrl}/videos/create`, payload, params);
}

const startTime = performance.now();

const queuePixVerse = new AsyncFunctionQueue();

// Build data entries from prompts; each entry tracks its own completed state.
for (let ind = 0; ind < prompts.length; ind++) {
    const entry = prompts[ind];
    const key = `video-${ind}`;
    data[key] = {
        jobid: key,
        prompt: entry.prompt,
        model: entry.model ?? 'v6',
        duration: entry.duration ?? 5,
        quality: entry.quality ?? '720p',
        use_source_image: entry.use_source_image ?? false,
        completed: false
    };
}

saveToFile('./result.json', data);

console.log(`${dateAsString()} ⁝ prompts to process: ${prompts.length}`);

// Enqueue all video creation jobs
Object.values(data).forEach(entry => queuePixVerse.enqueue(post_pixverse, entry));

while (hasMoreJobsToRun() != null)
    await sleep(10 * 1000);

console.log(`${dateAsString()} ⁝ total elapsed time ${new Date(performance.now() - startTime).toISOString()}`);

process.exit();
