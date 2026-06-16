#
# pip install requests
#
# Create an example.sh file with the following content and execute it from the command line using ./example.sh:
# USEAPI_TOKEN="…" python3 ./example.py
#
# USEAPI_CHANNEL is optional with the Midjourney API v3 — when omitted the API
# automatically selects a configured channel with available capacity.
#

import datetime
import os
import time
import requests
import json
import sys

# Midjourney API v3 root url
rootUrl = 'https://api.useapi.net/v3/midjourney'

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

def download_file(url, ind):
    localPath = f"./{ind}-{getFilenameFromUrl(url)}"
    response = requests.get(url)
    with open(localPath, 'wb') as file:
        file.write(response.content)

def main():
    # Load all required parameters from the environment variables
    token = os.getenv('USEAPI_TOKEN')
    channel = os.getenv('USEAPI_CHANNEL')  # optional in v3
    # You can use https://webhook.site if you want to receive results via callback.
    replyUrl = None
    # Time to pause between calls, in seconds
    sleepSecs = 5

    prompts = loadFromFile('./prompts.json')

    print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

    results = []
    ind = 0

    start_time = time.time()

    headers = {
        'Authorization': f"Bearer {token}",
        'Content-Type': 'application/json'
    }

    for prompt in prompts:
        ind += 1
        # Detailed documentation at https://useapi.net/docs/api-midjourney-v3/post-midjourney-jobs-imagine
        body = {
            'prompt': prompt,
            'stream': False  # stream defaults to true (SSE); set false to get the JSON job state we poll below
        }
        # channel is optional — API auto-selects a channel with capacity when omitted
        if channel:
            body['channel'] = channel
        if replyUrl is not None:
            body['replyUrl'] = f"{replyUrl}?ind={ind}"

        print(f"{dateAsString()} ⁝ #{ind} prompt: {prompt}")

        attempt = 0
        retry = True

        while retry:
            attempt += 1
            response = requests.post(f"{rootUrl}/jobs/imagine", headers=headers, data=json.dumps(body))
            result = response.json()

            print(f"{dateAsString()} ⁝ attempt #{attempt}, response: {{ status: {response.status_code}, jobid: {result.get('jobid')}, job_status: {result.get('status')} }}")

            if response.status_code == 201:  # Created — job accepted
                results.append({ 'status': response.status_code, 'jobid': result.get('jobid'), 'job_status': result.get('status'), 'ind': ind, 'prompt': prompt })
                retry = False
            elif response.status_code == 429:  # Channel at capacity or rate limited — wait and retry
                print(f"{dateAsString()} ⁝ #{ind} attempt #{attempt} channel busy, sleeping for {sleepSecs} secs...")
                time.sleep(sleepSecs)
            elif response.status_code == 596:  # Pending moderation / CAPTCHA — resolve in Discord, then POST /accounts/{channel}/reset
                print(f"{dateAsString()} ⁝ #{ind} channel pending moderation/CAPTCHA — resolve in Discord, then POST /accounts/{{channel}}/reset: {result}")
                results.append({ 'status': response.status_code, 'jobid': None, 'job_status': 'moderated', 'ind': ind, 'prompt': prompt })
                retry = False
            else:  # 400 / 401 / 402 / ...
                print(f"Unexpected response.status: {response.status_code}, result: {result}")
                retry = False

        saveToFile('./result.json', results)

    print(f"{dateAsString()} ⁝ downloading generated images")

    ind = 0

    for item in results:
        ind += 1
        jobid = item.get('jobid')
        status = item.get('status')

        print(f"{dateAsString()} ⁝ #{ind} jobid: {{ jobid: {jobid}, status: {status} }}")

        if status == 596:
            print(f"channel moderation pending, skipping prompt: {item.get('prompt')}")
        elif status == 201 and jobid:
            attempt = 0
            retry = True

            while retry:
                attempt += 1
                response = requests.get(f"{rootUrl}/jobs/{jobid}", headers={"Authorization": f"Bearer {token}"})
                result = response.json()

                print(f"{dateAsString()} ⁝ attempt #{attempt}, response: {{ status: {response.status_code}, jobid: {result.get('jobid')}, job_status: {result.get('status')} }}")

                if response.status_code == 200:
                    job_status = result.get('status')
                    if job_status == 'completed':
                        # In v3 generated media is nested under result['response']
                        attachments = (result.get('response') or {}).get('attachments', [])
                        if len(attachments):
                            download_file(attachments[0]['url'], ind)
                        else:
                            print(f"#{ind} completed jobid has no attachments")
                        retry = False
                    elif job_status in ['created', 'started', 'progress']:
                        print(f"{dateAsString()} ⁝ #{ind} attempt #{attempt} sleeping for {sleepSecs} secs... status: {job_status}")
                        time.sleep(sleepSecs)
                    elif job_status in ['moderated', 'failed']:
                        print(f"#{ind} job {job_status}: {result.get('error', result)}")
                        retry = False
                    else:
                        print(f"Unexpected job status: {result}")
                        retry = False
                else:
                    print(f"Unexpected response.status: {response.status_code}, result: {result}")
                    retry = False

    execution_time = time.time() - start_time

    print(f"{dateAsString()}  ⁝  total elapsed time {datetime.datetime.utcfromtimestamp(execution_time).strftime('%H:%M:%S')}")

main()
