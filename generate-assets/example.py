#
# pip install requests
#
# Create an example.sh file with the following content and execute it from the command line using ./example-js.sh:
# USEAPI_SERVER="…" USEAPI_CHANNEL="…" USEAPI_TOKEN="…" USEAPI_DISCORD="…" python3 ./example.py
#

import datetime
import os
import time
import requests
import json
import sys

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

def download_file(url, ind):
    localPath = f"./{ind}-{getFilenameFromUrl(url)}"
    response = requests.get(url)
    with open(localPath, 'wb') as file:
        file.write(response.content)

def main():
    # Load all required parameters from the environment variables
    token = os.getenv('USEAPI_TOKEN')
    discord = os.getenv('USEAPI_DISCORD')
    server = os.getenv('USEAPI_SERVER')
    channel = os.getenv('USEAPI_CHANNEL')

    prompts = loadFromFile('./prompts.json')

    print(f"{dateAsString()} ⁝ prompts to process: {len(prompts)}")

    results = []
    ind = 0
    
    start_time = time.time() 

    for prompt in prompts:
        ind += 1
        data = {
            'method': 'POST',
            'headers': {
                'Authorization': f"Bearer {token}",
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                'prompt': prompt,
                'discord': discord,
                'server': server,
                'channel': channel,
                # We will utilize all three available job slots for the Basic or Standard plan.
                'maxJobs': 3
            })
        }

        print(f"{dateAsString()} ⁝ #{ind} prompt: {prompt}")

        attempt = 0
        retry = True

        while retry:
            attempt += 1
            response = requests.post("https://api.useapi.net/v1/jobs/imagine", headers=data['headers'], data=data['body'])
            result = response.json()

            print(f"{dateAsString()} ⁝ attempt #{attempt}, response: {{ status: {response.status_code}, jobid: {result.get('jobid')}, job_status: {result.get('status')} }}")

            if response.status_code == 429:
                print(f"{dateAsString()} ⁝ #{ind} attempt #{attempt} sleeping for 10 secs...")
                time.sleep(10)
            elif response.status_code in [200, 422]:
                results.append({ 'status': response.status_code, 'jobid': result.get('jobid'), 'job_status': result.get('status'), 'ind': ind, 'prompt': prompt })
                retry = False
            else:
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

        if status == 422:
            print(f"Moderated prompt: {item.get('prompt')}")
        elif status == 200:
            attempt = 0
            retry = True
            
            while retry:
                attempt += 1
                response = requests.get(f"https://api.useapi.net/v1/jobs/?jobid={jobid}", headers={"Authorization": f"Bearer {token}"})
                result = response.json()

                print(f"{dateAsString()} ⁝ attempt #{attempt}, response: {{ status: {response.status_code}, jobid: {result.get('jobid')}, job_status: {result.get('status')} }}")
                
                if response.status_code == 200:
                    if result.get('status') == 'completed':
                        if len(result.get('attachments', [])):
                            download_file(result['attachments'][0]['url'], ind)
                        else:
                            print(f"#{ind} completed jobid has no attachments")
                        retry = False
                    elif result.get('status') in ['started', 'progress']:
                        print(f"{dateAsString()} ⁝ #{ind} attempt #{attempt} sleeping for 10 secs... status: {result.get('status')}")
                        time.sleep(10)
                else:
                    print(f"Unexpected response.status: {response.status_code}, result: {result}")
                    retry = False

    execution_time = time.time() - start_time 

    print(f"{dateAsString()}  ⁝  total elapsed time {datetime.datetime.utcfromtimestamp(execution_time).strftime('%H:%M:%S')}")
    
main()