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

prompts = []
prompt_ind = 0
webhook_ind = 0
results = {}

# Load all required parameters from the environment variables
token = os.getenv('USEAPI_TOKEN')
discord = os.getenv('USEAPI_DISCORD')
server = os.getenv('USEAPI_SERVER')
channel = os.getenv('USEAPI_CHANNEL')

# We will utilize all three available job slots for the Basic or Standard plan.
maxJobs = 3
# Provide additional prompt params
withParams = ' --relax' # --fast, --relax, --s all goes here
# Time to pause between Discord 429 calls
sleepSecs = 5
# API root url
rootUrl = 'https://api.useapi.net/v2'

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

        job = json.loads(post_data.decode('utf-8')) 

        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write("ok".format(self.path).encode('utf-8'))       
        
        replyRef = job.get('replyRef')
        jobid = job.get('jobid')
        status = job.get('status')
        content = job.get('content')
        attachments = job.get('attachments')

        print(f"{dateAsString()} ⁝ webhook #{replyRef} {jobid} {status} {content[:20]}…{content[-20:]}")
        
        attachments = job.get('attachments')
        url = attachments[0]['url'] if attachments else None
        
        results[replyRef] = job

        # If job completed we can start another one
        if job.get('status') in ('completed', 'moderated', 'failed', 'cancelled'):
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

# Extract filename.png from https://cdn.discordapp.com/attachments/server_id/channed_id/filename.png?ex=
def getFilenameFromUrl(url):
    return url.split("/")[-1].split("?")[0]

def downloadFile(url, ind):
    localPath = f"./{ind}-{getFilenameFromUrl(url)}"
    response = requests.get(url)
    with open(localPath, 'wb') as file:
        file.write(response.content)
        
async def submit():
    global prompt_ind
    global results
        
    while prompt_ind < len(prompts):

        print(f"{dateAsString()} ⁝ prompt #{prompt_ind} {prompts[prompt_ind]}")

        data = {
            'method': 'POST',
            'headers': {
                'Authorization': f"Bearer {token}",
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                'prompt': f"{prompts[prompt_ind]} {withParams}".strip(),
                'discord': discord,
                'server': server,
                'channel': channel,
                'maxJobs': maxJobs,
                'replyRef': f"{prompt_ind}",
                'replyUrl': f"{listener.url()}?ind={prompt_ind}" 
            })
        }

        response = requests.post(f"{rootUrl}/jobs/imagine", headers=data['headers'], data=data['body'])
            
        result = response.json()

        print(f"{dateAsString()} ⁝ response #{prompt_ind} HTTP {response.status_code} {{ jobid: {result.get('jobid')}, status: {result.get('status')}, executingJobs: {result.get('executingJobs')} }}")

        if response.status_code == 429:
            if result.get('executingJobs'):
                # Query is full : exit and rely on webhook to call once job completed
                return
            else:
                # Discord reported 429 : sleep and try again
                await time.sleep(sleepSecs)                
        elif response.status_code in [200, 422]: # OK or Moderated
            results[prompt_ind] = result
            prompt_ind += 1
        else:
            print(f"Unexpected response.status: {response.status_code}, result: {result}")
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