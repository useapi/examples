#
# pip install requests
# pip install ngrok
#
# Create an example.sh file with the following content and execute it from the command line using ./example.sh:
# USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" python3 ./example.py
#
# Optional environment variables:
#   MJ_CHANNEL      - pin Midjourney requests to a specific configured channel.
#                     Optional with the Midjourney API v3 — when omitted the API
#                     auto-selects a configured channel with available capacity.
#   PIXVERSE_EMAIL  - pin PixVerse requests to a specific configured account.
#                     When omitted the API randomly selects an available account.
#
# Pipeline: Midjourney imagine -> U1-U4 upscales -> PixVerse image-to-video (create-v4).
#

import requests
import ngrok

import datetime
import os
import time
import json
import sys
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler

# Global variables
prompts = []
data = {}
submitted = 0

# Load all required parameters from the environment variables
#
#   You can ⚙️ configure your
#   👉 Midjourney account(s) https://useapi.net/docs/api-midjourney-v3/post-midjourney-accounts
#   👉 PixVerse account(s)   https://useapi.net/docs/api-pixverse-v2/post-pixverse-accounts-email
#   Once configured, MJ_CHANNEL / PIXVERSE_EMAIL are no longer needed and can be removed.

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
rootPixVerseUrl = "https://api.useapi.net/v2/pixverse"

# https://github.com/ngrok/ngrok-python
listener = ngrok.forward(8081, authtoken_from_env=True)

print(f"Webhook {listener.url()}")


# Simple async query management
class AsyncFunctionQueue:
    def __init__(self):
        self.queue = []
        self.is_function_running = False

    def enqueue(self, fn, *args):
        self.queue.append((fn, args))
        asyncio.run(self.process_queue())

    async def process_queue(self):
        if self.is_function_running or len(self.queue) == 0:
            return

        self.is_function_running = True
        try:
            item = self.queue[0]
            fn, args = item
            result = await fn(*args)
            if result == "retry":
                print("Will retry:", fn, args)
            else:
                self.queue.remove(item)
        except Exception as error:
            print("An error occurred:", error)
        finally:
            self.is_function_running = False

        await self.process_queue()


# Create async queries
queueMidjourney = AsyncFunctionQueue()
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


# Extract filename.png from https://cdn.discordapp.com/attachments/server_id/channel_id/filename.png?ex=
def getFilenameFromUrl(url):
    return url.split("/")[-1].split("?")[0]


def contentTypeForFile(fileName):
    ext = fileName.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")


def downloadFile(url, prefix):
    localPath = f"./{prefix}-{getFilenameFromUrl(url)}"
    response = requests.get(url)
    with open(localPath, "wb") as file:
        file.write(response.content)
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


# POST a JSON payload to Midjourney v3 or PixVerse v2. Retries on 429 (busy/at capacity).
async def submit_payload(url, params, body, is_pixverse=False):
    global submitted

    while True:
        response = None
        retry_count = 0
        # On a slow connection the POST may fail, so retry up to 3 times
        while retry_count < 3:
            try:
                response = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                break
            except requests.RequestException as ex:
                print(f"fetch {url} failed #{retry_count}", ex)
                if retry_count > 1:
                    raise
                retry_count += 1

        job = response.json()

        jobid = job.get("jobid")
        video_id = job.get("video_id")
        status = job.get("status")
        error = job.get("error")
        errorDetails = job.get("errorDetails")
        verb = job.get("verb")

        print(
            f"{dateAsString()} ⁝ #{submitted} {verb or url} HTTP {response.status_code}",
            {jobid, video_id, status, error, errorDetails},
        )

        # 429 — Midjourney channel busy / all PixVerse accounts at capacity: wait and retry
        if response.status_code == 429:
            await asyncio.sleep(10)
            continue

        if is_pixverse:
            params["pixverse"] = {
                **params.get("pixverse", {}),
                "video_id": video_id,
                "status": status,
                "code": response.status_code,
                "error": error or errorDetails,
            }
            # If PixVerse failed to accept the job, no webhook will arrive
            if response.status_code != 200 and params.get("completed") is False:
                params["completed"] = True
        else:
            params["jobid"] = jobid
            params["status"] = status
            params["code"] = response.status_code
            if error:
                params["error"] = error
            if errorDetails:
                params["errorDetails"] = errorDetails
            # Midjourney rejected the job (596 moderation / 4xx) — mark downstream done so we don't hang
            if response.status_code != 201:
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

    return await submit_payload(f"{rootMidjourneyUrl}/jobs/button", params, body)


# Execute a Midjourney API v3 jobs/imagine, see https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
async def post_midjourney(verb, params):
    prompt = params.get("prompt")

    verb = verb.split("-")[0]

    body = {"stream": False, "replyUrl": listener.url()}

    if channel:  # optional in v3
        body["channel"] = channel

    if prompt:
        body["prompt"] = f"{prompt} {promptParams}".strip()

    return await submit_payload(f"{rootMidjourneyUrl}/jobs/{verb}", params, body)


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
        upload_response = requests.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": contentTypeForFile(imageFileName),
            },
            data=image_file.read(),
        )
    uploaded = upload_response.json()
    result = uploaded.get("result") or []
    path = result[0].get("path") if result else None

    print(f"{dateAsString()} ⁝ pixverse upload HTTP {upload_response.status_code}", {path})

    if not path:
        print("pixverse upload failed", uploaded)
        params["completed"] = True
        params["pixverse"] = {**params.get("pixverse", {}), "upload": uploaded, "code": upload_response.status_code}
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
        "replyRef": jobid,  # map the PixVerse callback back to this node
    }
    if pixverse_email:
        body["email"] = pixverse_email

    return await submit_payload(f"{rootPixVerseUrl}/videos/create", params, body, is_pixverse=True)


# Webhook callback
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hello from ngrok server")

    def do_POST(self):
        global data

        content_length = int(self.headers["Content-Length"])
        post_data = self.rfile.read(content_length)

        job = json.loads(post_data.decode("utf-8"))

        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

        # PixVerse callbacks carry a video_id (and no Midjourney verb)
        if job.get("video_id"):
            replyRef = job.get("replyRef") or (job.get("response") or {}).get("replyRef")
            completed = job.get("video_status_final") is True or job.get("video_status_name") == "COMPLETED"
            failed = job.get("video_status_name") == "FAILED" or job.get("errCode")
            url = job.get("url")

            print(f"{dateAsString()} ⁝ webhook pixverse {job.get('video_id')} {job.get('video_status_name')}")

            node = findByJobid(jobid=replyRef, json_object=data)
            if node:
                node["pixverse"] = {
                    **node.get("pixverse", {}),
                    "video_id": job.get("video_id"),
                    "status": job.get("video_status_name"),
                    "url": url,
                }
                if completed and url:
                    node["pixverse"]["videoFileName"] = downloadFile(url, f"{replyRef}-pixverse")
                if completed or failed:
                    node["completed"] = True
                saveToFile("./result.json", data)
            return

        # Midjourney callbacks — generated media is nested under job["response"]
        jobid = job.get("jobid")
        verb = job.get("verb")
        status = job.get("status")
        response = job.get("response") or {}
        content = response.get("content") or ""
        attachments = response.get("attachments")
        url = attachments[0].get("url") if attachments else None

        print(f"{dateAsString()} ⁝ webhook #{jobid} {verb} {status} {content[:20]}…{content[-20:] if content else ''}")

        if status in ("completed", "moderated", "failed", "cancelled"):
            node = findByJobid(jobid=jobid, json_object=data)

            if not node:
                return

            node["status"] = status
            node["content"] = content

            if status == "completed":
                if "buttons" in node:
                    # This is an imagine job — kick off the U1-U4 upscales
                    for _button, value in node["buttons"].items():
                        if not _button.startswith("_"):
                            queueMidjourney.enqueue(post_midjourney_button, _button, jobid, value)
                elif url:
                    # This is an upscale (U1-U4) leaf — download it and animate via PixVerse
                    node["imageFileName"] = downloadFile(url, jobid)
                    queuePixVerse.enqueue(post_pixverse, node)
                else:
                    # Completed leaf with no image — nothing to animate
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


start_time = time.time()

prompts = loadFromFile("./prompts.json")

print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

httpd = HTTPServer(("", 8081), SimpleHTTPRequestHandler)

# data holds every job to execute and tracks progress via the completed field (on the U1-U4 leaves).
for ind in range(len(prompts)):
    data[f"imagine-{ind}"] = {
        "jobid": None,
        "prompt": prompts[ind],
        "buttons": {
            "U1": {"jobid": None, "completed": False},
            "U2": {"jobid": None, "completed": False},
            "U3": {"jobid": None, "completed": False},
            "U4": {"jobid": None, "completed": False},
        },
    }

saveToFile("./result.json", data)

for key in data:
    queueMidjourney.enqueue(post_midjourney, key, data[key])

while hasMoreJobsToRun(data):
    httpd.handle_request()

execution_time = time.time() - start_time

print(
    f"{dateAsString()}  ⁝  total elapsed time {datetime.datetime.utcfromtimestamp(execution_time).strftime('%H:%M:%S')}"
)
