#
# pip install requests
# pip install ngrok
#
# Create an example.sh file with the following content and execute it from the command line using ./example-js.sh:
# USEAPI_SERVER="…" USEAPI_CHANNEL="…" USEAPI_TOKEN="…" USEAPI_DISCORD="…" NGROK_AUTHTOKEN="…" python3 ./example.py
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

#   You can ⚙️ configure your
#   👉 Midjourney account(s) https://useapi.net/docs/api-v2/post-account-midjourney-channel
#   👉 Pika accounts(s) https://useapi.net/docs/api-pika-v1/post-pika-account-channel
#   Once configured params DISCORD, MJ_SERVER, MJ_CHANNEL, PIKA_CHANNEL no longer needed and can be removed.

token = os.getenv("USEAPI_TOKEN")
discord = os.getenv("DISCORD")
server = os.getenv("MJ_SERVER")
channel = os.getenv("MJ_CHANNEL")
pika_channel = os.getenv("PIKA_CHANNEL")

# Optional params to add at the end of the Midjourney prompt
promptParams = " --v 6 --s 900"

# Prompt for Pika animation, see https://pikalabsai.org/pika-labs-commands-and-parameters/
pika_prompt = "smiling and blinking"

# API root url
rootMidjourneyUrl = "https://api.useapi.net/v2"
rootPikaUrl = "https://api.useapi.net/v1/pika"

# https://github.com/ngrok/ngrok-python
listener = ngrok.forward(8081, authtoken_from_env=True)

print(f"Webhook {listener.url()}")


# Simple async query management
class AsyncFunctionQueue:
    def __init__(self):
        self.queue = []
        self.is_function_running = False
        self.query_is_full = False

    def enqueue(self, fn, *args):
        self.query_is_full = False
        self.queue.append((fn, args))
        asyncio.run(self.process_queue())

    async def process_queue(self):
        if self.query_is_full or self.is_function_running or len(self.queue) == 0:
            return

        self.is_function_running = True
        try:
            item = self.queue[0]
            fn, args = item
            result = await fn(*args)
            if result == "full":
                print("Query is full:", fn, args)
                self.query_is_full = True
                return
            elif result == "retry":
                print("Will retry:", fn, args)
                self.query_is_full = False
            else:
                self.query_is_full = False
                self.queue.remove(item)
        except Exception as error:
            print("An error occurred:", error)
        finally:
            self.is_function_running = False

        await self.process_queue()


# Create async queries
queueMidjourney = AsyncFunctionQueue()
queuePika = AsyncFunctionQueue()


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


# Extract filename.png from https://cdn.discordapp.com/attachments/server_id/channed_id/filename.png?ex=
def getFilenameFromUrl(url):
    return url.split("/")[-1].split("?")[0]


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


async def submit_payload(url, params, json=None, files=None):
    global submitted

    response = None
    retry_count = 0

    # When using slow connection fetch may fail to POST large payload so we will retry up to 3 times
    while retry_count < 3:
        try:
            if json:
                response = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=json,
                )

            else:
                response = requests.post(
                    url, headers={"Authorization": f"Bearer {token}"}, files=files
                )
            break
        except requests.RequestException as ex:
            print(f"fetch {url} failed #{retry_count}", ex)
            if retry_count > 1:
                raise
            retry_count += 1

    job = response.json()

    jobid = job.get("jobid")
    status = job.get("status")
    error = job.get("error")
    errorDetails = job.get("errorDetails")
    verb = job.get("verb")
    button = job.get("button", "")
    prompt = job.get("prompt", "")
    executingJobs = job.get("executingJobs")

    print(
        f"{dateAsString()} ⁝ #{submitted} {verb or url} {button} ({prompt}) HTTP {response.status_code}",
        {
            jobid,
            status,
            error,
            errorDetails,
        },
    )

    if response.status_code == 429:
        # Query is full, retry again later once one of running jobs complete
        if executingJobs:
            return "full"
        else:
            # We got rate-limited 429 from Discord, let's play safe and sleep for 10 or so seconds before trying again
            await asyncio.sleep(10)
            return "retry"
    elif response.status_code == 504:
        # Query overflow (should never happen unless maxJobs misconfigured)
        print(
            "504 query overflow detected, sleeping for 3 minutes to allow already running jobs complete"
        )
        await asyncio.sleep(3 * 60)
    else:
        if "pika" in url:
            params["pika"] = {}
        
        update = params["pika"] if "pika" in url else params
                
        update["jobid"] = jobid
        update["status"] = status
        update["code"] = response.status_code
        if error:
            update["error"] = error
        if errorDetails:
            update["errorDetails"] = errorDetails
                
        if error and params.get("completed") == False:
            params["completed"] = True

        saveToFile("./result.json", data)

        submitted += 1


# Use this function to execute any of API v2 Midjourney jobs/button, see https://useapi.net/docs/api-v2/post-jobs-button
# button param may have unique id after which will be removed.
# Examples:
#   post_midjourney_button('U1', <parent_jobid>, {} );
#   post_midjourney_button('V3-456', <parent_jobid>, { prompt: 'color it red' } );
async def post_midjourney_button(button, parent_jobid, params):
    prompt = params.get("prompt")

    button = button.split("-")[0]

    json = {"jobid": parent_jobid, "button": button, "replyUrl": listener.url()}

    if prompt:
        json["prompt"] = f"{prompt} {promptParams}".strip()

    return await submit_payload(
        f"{rootMidjourneyUrl}/jobs/button",
        params=params,
        json=json,
    )


# Use this function to execute any of API v2 Midjourney jobs, see https://useapi.net/docs/api-v2.
# verb param may have unique id after which will be removed.
# Examples:
#   post_midjourney('imagine', { prompt: 'cat in the hat' } );
#   post_midjourney('imagine-456', { prompt: 'cat in the hat' } );
#   post_midjourney('blend', { blendUrls: ['https://url.to.blend.1','https://url.to.blend2'] } );
#   post_midjourney('blend-123', { blendUrls: ['https://url.to.blend.1','https://url.to.blend2'] } );
#   post_midjourney('describe', { describeUrl: 'https://url.to.describe' } );
#   post_midjourney('describe-345', { describeUrl: 'https://url.to.describe' } );
async def post_midjourney(verb, params):
    prompt = params.get("prompt")
    blendUrls = params.get("blendUrls")
    blendDimensions = params.get("blendDimensions")
    describeUrl = params.get("describeUrl")

    verb = verb.split("-")[0]

    json = {
        "discord": discord,
        "server": server,
        "channel": channel,
        "replyUrl": listener.url(),
    }

    if prompt:
        json["prompt"] = f"{prompt} {promptParams}".strip()

    if blendUrls:
        json["blendUrls"] = blendUrls

    if blendDimensions:
        json["blendDimensions"] = blendDimensions

    if describeUrl:
        json["describeUrl"] = describeUrl

    return await submit_payload(
        f"{rootMidjourneyUrl}/jobs/{verb}", params=params, json=json
    )


# Use this function to execute any of API v1 Pika jobs, see https://useapi.net/docs/api-pika-v1.
# verb param may have unique id after which will be removed.
# Examples:
#   post_pika('create', { pika_prompt: 'dancing cat in the hat' } );
#   post_pika('create-123', { pika_prompt: 'dancing cat in the hat' } );
#   post_pika('animate', { imageFileName: './starting-image.jpg', pika_prompt: 'dancing cat in the hat' } );
#   post_pika('animate-456', { imageFileName: './starting-image.jpg'pika_prompt } );
async def post_pika(verb, params):
    imageFileName = params.get("imageFileName")
    pika_prompt = params.get("pika_prompt")
    jobid = params.get("jobid")

    verb = verb.split("-")[0]

    files = {
        "discord": (None, discord),
        "channel": (None, pika_channel),
        "prompt": (None, pika_prompt),
        "replyRef": (None, jobid),
        "replyUrl": (None, listener.url()),
    }

    if imageFileName:
        with open(imageFileName, "rb") as image_file:
            files["image"] = ("image.png", image_file.read(), "image/png")

    return await submit_payload(f"{rootPikaUrl}/{verb}", params=params, files=files)


async def async_print(*args, **kwargs):
    # Perform the print operation
    print(*args, *kwargs)


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
        self.wfile.write("ok".format(self.path).encode("utf-8"))

        jobid = job.get("jobid")
        verb = job.get("verb")
        button = job.get("button")
        status = job.get("status")
        content = job.get("content", "")
        attachments = job.get("attachments")
        replyRef = job.get("replyRef")

        print(
            f"{dateAsString()} ⁝ webhook #{jobid} {verb} {status} {content[:20]}…{content[-20:]}"
        )

        # Midjourney
        if verb in ("imagine", "describe", "blend", "button"):
            if status in ("completed", "moderated", "failed", "cancelled"):
                node = findByJobid(jobid=jobid, json_object=data)

                if not node:
                    print(f"not {jobid}")

                node["status"] = status
                node["content"] = content

                _submitted = False

                if status == "completed":
                    if "buttons" in node:
                        for _button, value in node["buttons"].items():
                            if not _button.startswith("_"):
                                queueMidjourney.enqueue(
                                    post_midjourney_button, _button, jobid, value
                                )
                                _submitted = True

                    if (
                        button in ["U1", "U2", "U3", "U4"]
                        and attachments
                        and len(attachments[0].get("url", "")) > 0
                    ):
                        node["imageFileName"] = downloadFile(
                            attachments[0]["url"], jobid
                        )

                        # Start Pika generation, pika/create or pika/animate
                        queuePika.enqueue(post_pika, "animate", node)
                else:
                    node["pika"] = "skipping"

                saveToFile("./result.json", data)

                if not _submitted:
                    queueMidjourney.enqueue(
                        async_print, f"👉 ${jobid} ${status} ${content}"
                    )

        if verb in ("pika-create", "pika-animate"):
            if status in ("completed", "moderated", "failed", "cancelled"):
                node = findByJobid(jobid=replyRef, json_object=data)

                if not node:
                    print(f"Unable to locate jobid {jobid}")

                node["completed"] = True

                node["pika"] = {
                    **node.get("pika", {}),
                    "status": status,
                    "content": content,
                }

                if attachments and len(attachments[0].get("url", "")) > 0:
                    node["pika"]["imageFileName"] = downloadFile(
                        attachments[0]["url"], jobid
                    )

                saveToFile("./result.json", data)

                queuePika.enqueue(async_print, f"👉 {jobid} {status} {content}")


start_time = time.time()

prompts = loadFromFile("./prompts.json")

print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

httpd = HTTPServer(("", 8081), SimpleHTTPRequestHandler)

# We will use data variable to hold list of all jobs to execute and to track progress via completed field (where applicable).
for ind in range(len(prompts)):
    data[f"imagine-{ind}"] = {
        "jobid": None,
        "prompt": prompts[ind],
        "buttons": {
            "U1": {"jobid": None, "completed": False, "pika_prompt": pika_prompt},
            "U2": {"jobid": None, "completed": False, "pika_prompt": pika_prompt},
            "U3": {"jobid": None, "completed": False, "pika_prompt": pika_prompt},
            "U4": {"jobid": None, "completed": False, "pika_prompt": pika_prompt},
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
