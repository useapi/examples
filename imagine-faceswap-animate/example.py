#
# pip install aiohttp
# pip install ngrok
#
# Create an example.sh file with the following content and execute it from the command line using ./example-js.sh:
# USEAPI_SERVER="…" NGROK_AUTHTOKEN="…" python3 ./example.py
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

#   Configure your
#     ✔️ Midjourney account https://useapi.net/docs/api-v2/post-account-midjourney-channel
#     ✔️ InsightFaceSwap account https://useapi.net/docs/api-faceswap-v1/post-faceswap-account-channel
#     ✔️ Pika accounts https://useapi.net/docs/api-pika-v1/post-pika-account-channel

token = os.getenv("USEAPI_TOKEN")

# Optional params to add at the end of the Midjourney prompt
promptParams = " --v 6 --s 900"

# Prompt for Pika animation, see https://pikalabsai.org/pika-labs-commands-and-parameters/
pika_prompt = "smiling and blinking"

# API root url
rootMidjourneyUrl = "https://api.useapi.net/v2"
rootPikaUrl = "https://api.useapi.net/v1/pika"
rootFaceSwap = "https://api.useapi.net/v1/faceswap"

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
        self.query_is_full = False

    async def enqueue(self, fn, *args):
        self.query_is_full = False
        self.queue.append((fn, args))
        await self.process_queue()

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
queueFaceSwap = AsyncFunctionQueue()
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


# Extract png from https://cdn.discordapp.com/attachments/server_id/channed_id/filename.png?ex=
def getFileExtensionFromUrl(url):
    matches = re.search(r"\.([^.?]+)(?=\?|$)", url)
    return matches.group(1) if matches else ""


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


async def submit_payload(url, params, json=None, files=None):
    global submitted

    job = None
    retry_count = 0

    # When using slow connection fetch may fail to POST large payload so we will retry up to 3 times
    async with aiohttp.ClientSession() as session:
        while retry_count < 3:
            try:
                if json:
                    async with session.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                        json=json,
                    ) as response:
                        job = await response.json()
                else:
                    async with session.post(
                        url, headers={"Authorization": f"Bearer {token}"}, data=files
                    ) as response:
                        job = await response.json()
                break
            except aiohttp.ClientConnectorError as ex:
                print(f"fetch {url} failed #{retry_count}", ex)
                if retry_count > 1:
                    raise
                retry_count += 1

        jobid = job.get("jobid")
        status = job.get("status")
        error = job.get("error")
        errorDetails = job.get("errorDetails")
        verb = job.get("verb")
        button = job.get("button", "")
        prompt = job.get("prompt", "")
        executingJobs = job.get("executingJobs")
        attachments = job.get("attachments")

        print(
            f"{dateAsString()} ⁝ #{submitted} {verb or url} {button} ({prompt}) HTTP {response.status}",
            {
                jobid,
                status,
                error,
                errorDetails,
            },
        )

        if response.status == 429:
            # Query is full, retry again later once one of running jobs complete
            if ("faceswap" in url) or executingJobs:
                return "full"
            else:
                # We got rate-limited 429 from Discord, let's play safe and sleep for 10 or so seconds before trying again
                await asyncio.sleep(10)
                return "retry"
        elif response.status == 504:
            if "faceswap" not in url:
                # Query overflow (should never happen unless maxJobs misconfigured)
                print(
                    "504 query overflow detected, sleeping for 3 minutes to allow already running jobs complete"
                )
                await asyncio.sleep(3 * 60)
        else:
            if "faceswap" in url:
                params["faceswap"] = {}

            if "pika" in url:
                params["pika"] = {}

            update = (
                params["pika"]
                if "pika" in url
                else params["faceswap"] if "faceswap" in url else params
            )

            update["jobid"] = jobid
            update["status"] = status
            update["code"] = response.status
            if error:
                update["error"] = error
            if errorDetails:
                update["errorDetails"] = errorDetails
            if attachments:
                update["attachments"] = attachments

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

    json = {"replyUrl": listener.url()}

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

    data = aiohttp.FormData()

    data.add_field("prompt", pika_prompt)
    data.add_field("replyRef", jobid)
    data.add_field("replyUrl", listener.url())

    if imageFileName:
        data.add_field(
            name="image",
            value=open(imageFileName, "rb"),
            filename="image.png",
            content_type="image/png",
        )

    verb = verb.split("-")[0]

    return await submit_payload(f"{rootPikaUrl}/{verb}", params=params, files=data)


# InsightFaceSwap API https://useapi.net/docs/api-faceswap-v1
# faceswap/swap https://useapi.net/docs/api-faceswap-v1/post-faceswap-swap
async def post_faceswap(verb, params):
    sourceFileName = params.get("sourceFileName")
    targetFileName = params.get("targetFileName")
    jobid = params.get("jobid")

    data = aiohttp.FormData()

    data.add_field("replyRef", jobid)
    data.add_field("replyUrl", listener.url())

    if sourceFileName:
        data.add_field(
            name="saveid_image",
            value=open(sourceFileName, "rb"),
            filename="saveid_image.png",
            content_type="image/png",
        )

    if targetFileName:
        data.add_field(
            name="swapid_image",
            value=open(targetFileName, "rb"),
            filename="swapid_image.png",
            content_type="image/png",
        )

    verb = verb.split("-")[0]

    return await submit_payload(f"{rootFaceSwap}/{verb}", params=params, files=data)


async def async_print(*args, **kwargs):
    # Perform the print operation
    print(*args, *kwargs)


async def handle_post(request):
    global data

    job = await request.json()

    web.Response(text="ok")

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
                            await queueMidjourney.enqueue(
                                post_midjourney_button, _button, jobid, value
                            )
                            _submitted = True

                if (
                    button in ["U1", "U2", "U3", "U4"]
                    and attachments
                    and len(attachments[0].get("url", "")) > 0
                ):
                    fileName = f"{jobid.split('-')[0]}-{button}"
                    node["targetFileName"] = await downloadFile(
                        attachments[0]["url"], fileName
                    )

                    # Swap face
                    await queueFaceSwap.enqueue(post_faceswap, "swap", node)

            else:
                node["faceswap"] = "skipping"
                node["pika"] = "skipping"

            saveToFile("./result.json", data)

            if not _submitted:
                await queueMidjourney.enqueue(
                    async_print, f"👉 ${jobid} ${status} ${content}"
                )

    # FaceSwap
    if job["verb"] == "faceswap-swap":
        if status in ["completed", "failed"]:
            node = findByJobid(jobid=replyRef, json_object=data)

            if not node:
                print(f"Unable to locate jobid {jobid}")

            node["faceswap"] = {
                **node.get("faceswap", {}),
                "status": status,
                "content": content,
            }

            if (
                status == "completed"
                and attachments
                and len(attachments[0].get("url", "")) > 0
            ):
                fileName = f"{replyRef.split('-')[0]}-faceswap"
                node["imageFileName"] = await downloadFile(
                    attachments[0]["url"], fileName
                )
            else:
                # We can still animate originally unscaled image
                node["imageFileName"] = node["targetFileName"]

            saveToFile("./result.json", data)

            await queuePika.enqueue(post_pika, "animate", node)

    # Pika
    if verb in ("pika-create", "pika-animate"):
        if status in ("completed", "moderated", "failed", "cancelled"):
            node = findByJobid(jobid=replyRef, json_object=data)

            if not node:
                print(f"Unable to locate jobid {jobid}")

            node["pika"] = {
                **node.get("pika", {}),
                "status": status,
                "content": content,
            }

            if attachments and len(attachments[0].get("url", "")) > 0:
                fileName = f"{replyRef.split('-')[0]}-animated"
                node["pika"]["imageFileName"] = await downloadFile(
                    attachments[0]["url"], fileName
                )

            node["completed"] = True

            saveToFile("./result.json", data)

            await queuePika.enqueue(async_print, f"👉 {jobid} {status} {content}")


start_time = time.time()

prompts = loadFromFile("./prompts.json")

print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

# We will use data variable to hold list of all jobs to execute and to track progress via completed field (where applicable).
for ind in range(len(prompts)):
    data[f"imagine-{ind}"] = {
        "jobid": None,
        "prompt": prompts[ind],
        "buttons": {
            "U1": {
                "jobid": None,
                "completed": False,
                "sourceFileName": sourceFileName,
                "pika_prompt": pika_prompt,
            },
            "U2": {
                "jobid": None,
                "completed": False,
                "sourceFileName": sourceFileName,
                "pika_prompt": pika_prompt,
            },
            "U3": {
                "jobid": None,
                "completed": False,
                "sourceFileName": sourceFileName,
                "pika_prompt": pika_prompt,
            },
            "U4": {
                "jobid": None,
                "completed": False,
                "sourceFileName": sourceFileName,
                "pika_prompt": pika_prompt,
            },
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
