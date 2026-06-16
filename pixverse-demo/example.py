#
# pip install requests
# pip install ngrok
#
# Create an example.sh file with the following content and execute it from the command line using ./example.sh:
# USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" python3 ./example.py
#
# Optional environment variables:
#   PIXVERSE_EMAIL  - pin PixVerse requests to a specific configured account.
#                     When omitted the API randomly selects an available account.
#
# Supports both text-to-video (t2v) and image-to-video (i2v).
# Set use_source_image: true in a prompts.json entry to upload ./source.jpg as the first frame.
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
#   👉 PixVerse account(s) https://useapi.net/docs/api-pixverse-v2/post-pixverse-accounts-email
#   Once configured, PIXVERSE_EMAIL is no longer needed and can be removed.

token = os.getenv("USEAPI_TOKEN")
pixverse_email = os.getenv("PIXVERSE_EMAIL")  # optional

# API root url
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


# Create async queue
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


# Extract filename from https://…/filename.mp4?…
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


# Find a data node by its jobid field.
def findByJobid(jobid, json_object):
    if isinstance(json_object, dict):
        for key, value in json_object.items():
            if key == "jobid" and value == jobid:
                return json_object
            elif isinstance(value, dict):
                result = findByJobid(jobid, value)
                if result is not None:
                    return result
    return None


# Checks whether there are more jobs to run by looking for a job with completed = False.
def hasMoreJobsToRun(json_object):
    if isinstance(json_object, dict):
        for key, value in json_object.items():
            if key == "completed" and not value:
                return True
            elif isinstance(value, dict):
                if hasMoreJobsToRun(value):
                    return True
    return False


# POST a JSON payload to PixVerse v2. Retries on 429 (at capacity).
async def submit_payload(url, params, body):
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

        video_id = job.get("video_id")
        status = job.get("status")
        error = job.get("error")
        errorDetails = job.get("errorDetails")

        print(
            f"{dateAsString()} ⁝ #{submitted} {url} ({params.get('prompt', '')}) HTTP {response.status_code}",
            {video_id, status, error, errorDetails},
        )

        # 429 — all PixVerse accounts at capacity: wait and retry
        if response.status_code == 429:
            await asyncio.sleep(10)
            continue

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

        saveToFile("./result.json", data)
        submitted += 1
        return job


# Upload source.jpg and create a PixVerse v2 video.
# If the prompt entry has use_source_image: true, uploads ./source.jpg first → i2v.
# Otherwise submits text-to-video (t2v) without first_frame_path.
# See https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create
async def post_pixverse(params):
    jobid = params.get("jobid")
    prompt = params.get("prompt")
    model = params.get("model", "v6")
    duration = params.get("duration", 5)
    quality = params.get("quality", "720p")
    use_source_image = params.get("use_source_image", False)

    first_frame_path = None

    if use_source_image:
        # 1. Upload the source image (raw bytes)
        upload_url = f"{rootPixVerseUrl}/files"
        if pixverse_email:
            upload_url += f"?email={pixverse_email}"

        with open("./source.jpg", "rb") as image_file:
            upload_response = requests.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": contentTypeForFile("./source.jpg"),
                },
                data=image_file.read(),
            )
        uploaded = upload_response.json()
        result = uploaded.get("result") or []
        first_frame_path = result[0].get("path") if result else None

        print(f"{dateAsString()} ⁝ pixverse upload HTTP {upload_response.status_code}", {first_frame_path})

        if not first_frame_path:
            print("pixverse upload failed", uploaded)
            params["completed"] = True
            params["pixverse"] = {**params.get("pixverse", {}), "upload": uploaded, "code": upload_response.status_code}
            saveToFile("./result.json", data)
            return

    # 2. Create the video (t2v or i2v)
    body = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "quality": quality,
        "replyUrl": listener.url(),
        "replyRef": jobid,  # map the PixVerse callback back to this node
    }

    if first_frame_path:
        body["first_frame_path"] = first_frame_path

    if pixverse_email:
        body["email"] = pixverse_email

    return await submit_payload(f"{rootPixVerseUrl}/videos/create", params, body)


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

        # PixVerse v2 callbacks carry a video_id
        if not job.get("video_id"):
            return

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


start_time = time.time()

prompts = loadFromFile("./prompts.json")

print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

httpd = HTTPServer(("", 8081), SimpleHTTPRequestHandler)

# Build data entries from prompts; each entry tracks its own completed state.
for ind, entry in enumerate(prompts):
    key = f"video-{ind}"
    data[key] = {
        "jobid": key,
        "prompt": entry["prompt"],
        "model": entry.get("model", "v6"),
        "duration": entry.get("duration", 5),
        "quality": entry.get("quality", "720p"),
        "use_source_image": entry.get("use_source_image", False),
        "completed": False,
    }

saveToFile("./result.json", data)

for key in data:
    queuePixVerse.enqueue(post_pixverse, data[key])

while hasMoreJobsToRun(data):
    httpd.handle_request()

execution_time = time.time() - start_time

print(
    f"{dateAsString()} ⁝ total elapsed time {datetime.datetime.utcfromtimestamp(execution_time).strftime('%H:%M:%S')}"
)
