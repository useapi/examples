#
# pip install requests
# pip install ngrok
#
# Create an example.sh file with the following content and execute it from the command line using ./example.sh:
# USEAPI_TOKEN="…" NGROK_AUTHTOKEN="…" python3 ./example.py
#
# USEAPI_CHANNEL is optional with the Midjourney API v3 — when omitted the API
# automatically selects a configured channel with available capacity.
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

prompts = []
prompt_ind = 0
webhook_ind = 0
results = {}

# Load all required parameters from the environment variables
token = os.getenv('USEAPI_TOKEN')
channel = os.getenv('USEAPI_CHANNEL')  # optional in v3

# Provide additional prompt params
withParams = ' --relax' # --fast, --relax, --s all goes here
# Time to pause between 429 (channel busy) retries
sleepSecs = 5
# Midjourney API v3 root url
rootUrl = 'https://api.useapi.net/v3/midjourney'

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

        fn, args = self.queue.pop(0)

        try:
            self.is_function_running = True
            await fn(*args)
        except Exception as error:
            print('An error occurred:', error)
        finally:
            self.is_function_running = False
            await self.process_queue()

# Create async query
queue = AsyncFunctionQueue()

# Webhook callback
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Hello from ngrok server')

    def do_POST(self):
        global results
        global webhook_ind

        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)

        # v3 callback body has the same JSON shape as GET /jobs/{jobid}
        job = json.loads(post_data.decode('utf-8'))

        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"ok")

        status = job.get('status')
        jobid = job.get('jobid')
        replyRef = (job.get('request') or {}).get('replyRef') or (job.get('response') or {}).get('replyRef') or job.get('replyRef')
        # In v3 generated media and content are nested under job['response']
        response = job.get('response') or {}
        content = response.get('content') or ''
        attachments = response.get('attachments')
        url = attachments[0]['url'] if attachments else None

        print(f"{dateAsString()} ⁝ webhook #{replyRef} {jobid} {status} {content[:20]}…{content[-20:]}")

        results[replyRef] = job

        # On a terminal state we can download (if any) and start another prompt
        if status in ('completed', 'moderated', 'failed', 'cancelled'):
            if url:
                downloadFile(url, int(replyRef))

            queue.enqueue(submit)

            webhook_ind += 1

def dateAsString():
    return datetime.datetime.now().isoformat()

def loadFromFile(filePath):
    try:
        with open(filePath, 'r') as file:
            data = json.load(file)
            return data
    except Exception as error:
        print(f'Unable to load file: {filePath}. Error: {error}')
    sys.exit(1)

def saveToFile(filePath, data):
    try:
        with open(filePath, 'w') as file:
            json.dump(data, file, indent=2)
    except Exception as error:
        print(f'Error writing to file: {error}')

# Extract filename.png from https://cdn.discordapp.com/attachments/server_id/channel_id/filename.png?ex=
def getFilenameFromUrl(url):
    return url.split("/")[-1].split("?")[0]

def downloadFile(url, ind):
    localPath = f"./{ind}-{getFilenameFromUrl(url)}"
    response = requests.get(url)
    with open(localPath, 'wb') as file:
        file.write(response.content)

async def submit():
    global prompt_ind
    global webhook_ind
    global results

    while prompt_ind < len(prompts):

        replyRef = f"{prompt_ind}"

        print(f"{dateAsString()} ⁝ prompt #{prompt_ind} {prompts[prompt_ind]}")

        # Detailed documentation at https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
        body = {
            'stream': False,  # stream defaults to true (SSE); set false to get the immediate JSON job state
            'prompt': f"{prompts[prompt_ind]} {withParams}".strip(),
            'replyRef': replyRef,
            'replyUrl': f"{listener.url()}?ind={prompt_ind}"
        }
        # channel is optional — API auto-selects a channel with capacity when omitted
        if channel:
            body['channel'] = channel

        headers = {
            'Authorization': f"Bearer {token}",
            'Content-Type': 'application/json'
        }

        response = requests.post(f"{rootUrl}/jobs/imagine", headers=headers, data=json.dumps(body))

        result = response.json()

        print(f"{dateAsString()} ⁝ response #{prompt_ind} HTTP {response.status_code} {{ jobid: {result.get('jobid')}, status: {result.get('status')} }}")

        if response.status_code == 201:  # Created — job accepted; its terminal state will arrive via webhook
            results[replyRef] = result
            prompt_ind += 1
        elif response.status_code == 429:  # Channel at capacity or rate limited
            if prompt_ind - webhook_ind > 0:
                # Jobs in flight: stop submitting and let the webhook resume us on completion
                return
            else:
                # Nothing in flight: wait and try again
                time.sleep(sleepSecs)
        elif response.status_code == 596:  # Channel pending moderation/CAPTCHA — resolve in Discord, then POST /accounts/{channel}/reset
            print(f"{dateAsString()} ⁝ #{prompt_ind} channel pending moderation/CAPTCHA — resolve in Discord, then POST /accounts/{{channel}}/reset: {result}")
            results[replyRef] = result
            webhook_ind += 1  # no webhook will arrive for this prompt
            prompt_ind += 1
        else:
            print(f"Unexpected response.status: {response.status_code}, result: {result}")
            webhook_ind += 1  # no webhook will arrive for this prompt
            prompt_ind += 1

        # Persist results to the file for debugging purposes
        saveToFile('./result.json', results)

prompts = loadFromFile('./prompts.json')

print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

httpd = HTTPServer(('', 8081), SimpleHTTPRequestHandler)

start_time = time.time()

queue.enqueue(submit)

while webhook_ind < len(prompts):
    httpd.handle_request()

execution_time = time.time() - start_time

print(f"{dateAsString()}  ⁝  total elapsed time {datetime.datetime.utcfromtimestamp(execution_time).strftime('%H:%M:%S')}")
