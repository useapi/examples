#
# pip install aiohttp
# pip install ngrok
#
# Create an example.sh file with the following content and execute it from the command line using ./example.sh:
# USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" python3 ./example.py
#
# Optional environment variables:
#   MJ_CHANNEL      - pin Midjourney requests to a specific configured channel (optional in v3).
#   PIXVERSE_EMAIL  - pin PixVerse requests to a specific configured account (optional).
#
# Pipeline: Midjourney imagine -> U1-U4 upscales -> InsightFaceSwap face swap -> PixVerse image-to-video.
#

import aiohttp
import ngrok

import datetime
import os
import time
import json
import sys
import re
import asyncio

from aiohttp import web

# Global variables
prompts = []
data = {}
submitted = 0

# Load all required parameters from the environment variables
#
#   Configure your
#     ✔️ Midjourney account      https://useapi.net/docs/api-midjourney-v3/post-midjourney-accounts
#     ✔️ InsightFaceSwap account  https://useapi.net/docs/api-faceswap-v1/post-faceswap-account-channel
#     ✔️ PixVerse account         https://useapi.net/docs/api-pixverse-v2/post-pixverse-accounts-email

token = os.getenv("USEAPI_TOKEN")
channel = os.getenv("MJ_CHANNEL")          # optional in v3
pixverse_email = os.getenv("PIXVERSE_EMAIL")  # optional

# Optional params to add at the end of the Midjourney prompt
promptParams = " --v 7 --s 250"

# Prompt that drives the PixVerse image-to-video animation
pixverse_prompt = "smiling and blinking"

# PixVerse video options, see https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create-v4
pixverse_model = "v6"
pixverse_duration = 5
pixverse_quality = "720p"

# API root urls
rootMidjourneyUrl = "https://api.useapi.net/v3/midjourney"
rootFaceSwap = "https://api.useapi.net/v1/faceswap"
rootPixVerseUrl = "https://api.useapi.net/v2/pixverse"

# Source image (face), change to any other file name of your choice
sourceFileName = "./source.jpg"

# https://github.com/ngrok/ngrok-python
listener = ngrok.forward(8081, authtoken_from_env=True)

print(f"Webhook {listener.url()}")


# Simple async query management
class AsyncFunctionQueue:
    def __init__(self):
        self.queue = []
        self.is_function_running = False

    async def enqueue(self, fn, *args):
        self.queue.append((fn, args))
        await self.process_queue()

    async def process_queue(self):
        if self.is_function_running or len(self.queue) == 0:
            return

        self.is_function_running = True
        try:
            item = self.queue[0]
            fn, args = item
            await fn(*args)
            self.queue.remove(item)
        except Exception as error:
            print("An error occurred:", error)
        finally:
            self.is_function_running = False

        await self.process_queue()


# Create async queries
queueMidjourney = AsyncFunctionQueue()
queueFaceSwap = AsyncFunctionQueue()
queuePixVerse = AsyncFunctionQueue()


def dateAsString():
    return datetime.datetime.now().isoformat()


def saveToFile(filePath, data):
    try:
        with open(filePath, "w") as file:
            json.dump(data, file, indent=2)
    except Exception as error:
        print(f"Error writing to file: {error}")


def loadFromFile(filePath):
    try:
        with open(filePath, "r") as file:
            data = json.load(file)
            return data
    except Exception as error:
        print(f"Unable to load file: {filePath}. Error: {error}")
    sys.exit(1)


# Extract png from https://cdn.discordapp.com/attachments/server_id/channel_id/filename.png?ex=
def getFileExtensionFromUrl(url):
    matches = re.search(r"\.([^.?]+)(?=\?|$)", url)
    return matches.group(1) if matches else ""


def contentTypeForFile(fileName):
    ext = fileName.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")


async def downloadFile(url, fileName):
    localPath = f"./{fileName}.{getFileExtensionFromUrl(url)}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            with open(localPath, "wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    file.write(chunk)
    return localPath


# Recursively traverses the job data structure to find a job by its jobid.
def findByJobid(jobid, json_object):
    if isinstance(json_object, dict):
        for key, value in json_object.items():
            if key == "jobid" and value == jobid:
                return json_object
            elif isinstance(value, dict):
                result = findByJobid(jobid, value)
                if result is not None:
                    return result
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        result = findByJobid(jobid, item)
                        if result is not None:
                            return result
    return None


# Checks whether there are more jobs to run by looking for a job with 'completed' = False.
def hasMoreJobsToRun(json_object):
    if isinstance(json_object, dict):
        for key, value in json_object.items():
            if key == "completed" and not value:
                return True
            elif isinstance(value, dict) or isinstance(value, list):
                if hasMoreJobsToRun(value):
                    return True
    elif isinstance(json_object, list):
        for item in json_object:
            if isinstance(item, dict) or isinstance(item, list):
                if hasMoreJobsToRun(item):
                    return True

    return False


# POST a JSON body (Midjourney v3 / PixVerse create) or multipart files (FaceSwap v1).
# Retries on 429 (busy / at capacity). kind is 'midjourney' | 'faceswap' | 'pixverse'.
async def submit_payload(url, params, kind, body=None, files=None):
    global submitted

    while True:
        job = None
        status_code = None
        retry_count = 0

        async with aiohttp.ClientSession() as session:
            while retry_count < 3:
                try:
                    if body is not None:
                        async with session.post(
                            url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Content-Type": "application/json",
                            },
                            json=body,
                        ) as response:
                            job = await response.json()
                            status_code = response.status
                    else:
                        async with session.post(
                            url, headers={"Authorization": f"Bearer {token}"}, data=files
                        ) as response:
                            job = await response.json()
                            status_code = response.status
                    break
                except aiohttp.ClientConnectorError as ex:
                    print(f"fetch {url} failed #{retry_count}", ex)
                    if retry_count > 1:
                        raise
                    retry_count += 1

        jobid = job.get("jobid")
        video_id = job.get("video_id")
        status = job.get("status")
        error = job.get("error")
        errorDetails = job.get("errorDetails")
        verb = job.get("verb")

        print(
            f"{dateAsString()} ⁝ #{submitted} {verb or url} HTTP {status_code}",
            {jobid, video_id, status, error, errorDetails},
        )

        # 429 — busy / at capacity: wait and retry
        if status_code == 429:
            await asyncio.sleep(10)
            continue

        if kind == "pixverse":
            params["pixverse"] = {
                **params.get("pixverse", {}),
                "video_id": video_id,
                "status": status,
                "code": status_code,
                "error": error or errorDetails,
            }
            if status_code != 200 and params.get("completed") is False:
                params["completed"] = True
        elif kind == "faceswap":
            params["faceswap"] = {
                **params.get("faceswap", {}),
                "jobid": jobid,
                "status": status,
                "code": status_code,
                "error": error or errorDetails,
            }
        else:  # midjourney
            params["jobid"] = jobid
            params["status"] = status
            params["code"] = status_code
            if error:
                params["error"] = error
            if errorDetails:
                params["errorDetails"] = errorDetails
            if status_code != 201:
                # Midjourney rejected the job — don't hang waiting on a webhook
                if "buttons" in params:
                    for _b, v in params["buttons"].items():
                        if not _b.startswith("_"):
                            v["completed"] = True
                elif params.get("completed") is False:
                    params["completed"] = True

        saveToFile("./result.json", data)
        submitted += 1
        return job


# Execute a Midjourney API v3 jobs/button, see https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-button
async def post_midjourney_button(button, parent_jobid, params):
    button = button.split("-")[0]
    body = {"jobId": parent_jobid, "button": button, "stream": False, "replyUrl": listener.url()}
    return await submit_payload(f"{rootMidjourneyUrl}/jobs/button", params, "midjourney", body=body)


# Execute a Midjourney API v3 jobs/imagine, see https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
async def post_midjourney(verb, params):
    prompt = params.get("prompt")
    verb = verb.split("-")[0]

    body = {"stream": False, "replyUrl": listener.url()}
    if channel:  # optional in v3
        body["channel"] = channel
    if prompt:
        body["prompt"] = f"{prompt} {promptParams}".strip()

    return await submit_payload(f"{rootMidjourneyUrl}/jobs/{verb}", params, "midjourney", body=body)


# Swap a source face onto the Midjourney target image with InsightFaceSwap v1.
# See https://useapi.net/docs/api-faceswap-v1/post-faceswap-swap
async def post_faceswap(params):
    src = params.get("sourceFileName")
    target = params.get("targetFileName")
    jobid = params.get("jobid")

    files = aiohttp.FormData()
    files.add_field("replyRef", jobid)
    files.add_field("replyUrl", listener.url())
    if src:
        files.add_field(name="saveid_image", value=open(src, "rb"), filename="saveid_image.png", content_type="image/png")
    if target:
        files.add_field(name="swapid_image", value=open(target, "rb"), filename="swapid_image.png", content_type="image/png")

    job = await submit_payload(f"{rootFaceSwap}/swap", params, "faceswap", files=files)

    # If face swap did not start, fall back to animating the original upscale
    if not job or not job.get("jobid"):
        params["imageFileName"] = target
        await queuePixVerse.enqueue(post_pixverse, params)


# Animate an image with PixVerse image-to-video (create-v4):
#   1. Upload the local image via POST /files            -> returns result[0]["path"]
#   2. POST /videos/create with first_frame_path + prompt (i2v) + replyUrl
# See https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create-v4
# params["jobid"] (the upscale job id) is passed as replyRef so the PixVerse
# callback can be mapped back to this node via findByJobid().
async def post_pixverse(params):
    imageFileName = params.get("imageFileName")
    jobid = params.get("jobid")

    if not imageFileName:
        params["completed"] = True
        return

    # 1. Upload the image (raw bytes)
    upload_url = f"{rootPixVerseUrl}/files/"
    if pixverse_email:
        upload_url += f"?email={pixverse_email}"

    with open(imageFileName, "rb") as image_file:
        image_bytes = image_file.read()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            upload_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": contentTypeForFile(imageFileName)},
            data=image_bytes,
        ) as response:
            uploaded = await response.json()
            upload_status = response.status

    result = uploaded.get("result") or []
    path = result[0].get("path") if result else None

    print(f"{dateAsString()} ⁝ pixverse upload HTTP {upload_status}", {path})

    if not path:
        print("pixverse upload failed", uploaded)
        params["completed"] = True
        params["pixverse"] = {**params.get("pixverse", {}), "upload": uploaded, "code": upload_status}
        saveToFile("./result.json", data)
        return

    # 2. Create the image-to-video job
    body = {
        "model": pixverse_model,
        "prompt": pixverse_prompt,
        "first_frame_path": path,
        "duration": pixverse_duration,
        "quality": pixverse_quality,
        "replyUrl": listener.url(),
        "replyRef": jobid,
    }
    if pixverse_email:
        body["email"] = pixverse_email

    return await submit_payload(f"{rootPixVerseUrl}/videos/create", params, "pixverse", body=body)


async def handle_post(request):
    global data

    job = await request.json()

    # ---- PixVerse callbacks carry a video_id (and no Midjourney verb) ----
    if job.get("video_id"):
        replyRef = job.get("replyRef") or (job.get("response") or {}).get("replyRef")
        completed = job.get("video_status_final") is True or job.get("video_status_name") == "COMPLETED"
        failed = job.get("video_status_name") == "FAILED" or job.get("errCode")
        url = job.get("url")

        print(f"{dateAsString()} ⁝ webhook pixverse {job.get('video_id')} {job.get('video_status_name')}")

        node = findByJobid(jobid=replyRef, json_object=data)
        if node:
            node["pixverse"] = {**node.get("pixverse", {}), "video_id": job.get("video_id"), "status": job.get("video_status_name"), "url": url}
            if completed and url:
                node["pixverse"]["videoFileName"] = await downloadFile(url, f"{str(replyRef).split('-')[0]}-animated")
            if completed or failed:
                node["completed"] = True
            saveToFile("./result.json", data)
        return web.Response(text="ok")

    verb = job.get("verb")
    status = job.get("status")
    content = job.get("content", "") or ""
    replyRef = job.get("replyRef")

    # ---- InsightFaceSwap (v1) callbacks — verb 'faceswap-swap', media at top level ----
    if isinstance(verb, str) and verb.startswith("faceswap"):
        attachments = job.get("attachments")
        print(f"{dateAsString()} ⁝ webhook faceswap {replyRef} {status}")

        if status in ("completed", "failed", "moderated", "cancelled"):
            node = findByJobid(jobid=replyRef, json_object=data)
            if node:
                node["faceswap"] = {**node.get("faceswap", {}), "status": status, "content": content}
                if status == "completed" and attachments and len(attachments[0].get("url", "")) > 0:
                    node["imageFileName"] = await downloadFile(attachments[0]["url"], f"{str(replyRef).split('-')[0]}-faceswap")
                else:
                    # Face swap failed — animate the original upscale instead
                    node["imageFileName"] = node.get("targetFileName")
                saveToFile("./result.json", data)
                await queuePixVerse.enqueue(post_pixverse, node)
        return web.Response(text="ok")

    # ---- Midjourney (v3) callbacks — media nested under job["response"] ----
    jobid = job.get("jobid")
    response = job.get("response") or {}
    content = response.get("content") or ""
    attachments = response.get("attachments")
    url = attachments[0].get("url") if attachments else None

    print(f"{dateAsString()} ⁝ webhook #{jobid} {verb} {status} {content[:20]}…{content[-20:] if content else ''}")

    if status in ("completed", "moderated", "failed", "cancelled"):
        node = findByJobid(jobid=jobid, json_object=data)
        if node:
            node["status"] = status
            node["content"] = content

            if status == "completed":
                if "buttons" in node:
                    # imagine job — kick off the U1-U4 upscales
                    for _button, value in node["buttons"].items():
                        if not _button.startswith("_"):
                            await queueMidjourney.enqueue(post_midjourney_button, _button, jobid, value)
                elif url:
                    # upscale (U1-U4) leaf — download the target image, then swap the face
                    node["targetFileName"] = await downloadFile(url, f"{str(jobid).split('-')[0]}-target")
                    await queueFaceSwap.enqueue(post_faceswap, node)
                else:
                    # Completed leaf with no image — nothing downstream to run
                    node["completed"] = True
            else:
                # Moderated / failed / cancelled — nothing downstream to run
                if "buttons" in node:
                    for _button, value in node["buttons"].items():
                        if not _button.startswith("_"):
                            value["completed"] = True
                else:
                    node["completed"] = True

            saveToFile("./result.json", data)

    return web.Response(text="ok")


start_time = time.time()

prompts = loadFromFile("./prompts.json")

print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

# data holds every job to execute and tracks progress via the completed field (on the U1-U4 leaves).
for ind in range(len(prompts)):
    data[f"imagine-{ind}"] = {
        "jobid": None,
        "prompt": prompts[ind],
        "buttons": {
            "U1": {"jobid": None, "completed": False, "sourceFileName": sourceFileName},
            "U2": {"jobid": None, "completed": False, "sourceFileName": sourceFileName},
            "U3": {"jobid": None, "completed": False, "sourceFileName": sourceFileName},
            "U4": {"jobid": None, "completed": False, "sourceFileName": sourceFileName},
        },
    }

saveToFile("./result.json", data)


async def run():
    for key in data:
        await queueMidjourney.enqueue(post_midjourney, key, data[key])


async def check_if_completed():
    while True:
        await asyncio.sleep(5)

        if not hasMoreJobsToRun(data):
            raise aiohttp.web.GracefulExit()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.create_task(run())
    loop.create_task(check_if_completed())

    app = web.Application()
    app.router.add_post("/", handle_post)

    web.run_app(app, port=8081, loop=loop)

    execution_time = time.time() - start_time

    print(
        f"{dateAsString()}  ⁝  total elapsed time {datetime.datetime.utcfromtimestamp(execution_time).strftime('%H:%M:%S')}"
    )
