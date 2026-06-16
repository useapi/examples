## Create videos with PixVerse AI (v2 web API)

### Introduction

In this article we will show how to use the [PixVerse v2 API](https://useapi.net/docs/api-pixverse-v2) provided by [useapi.net](https://useapi.net) to generate videos. PixVerse v2 supports both **text-to-video** (t2v) and **image-to-video** (i2v) generation using the web API — no Discord bot required.

Available models include `v6` and later. Videos can be generated at 5 or 8 seconds duration in `540p`, `720p`, or `1080p` quality.

### Examples

[videos/create](https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create) (text-to-video)
> A cinematic shot of a tiny hedgehog dressed in a complete astronaut suit, floating in the vastness of outer space

[videos/create](https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create) (image-to-video)  
Source [image](./source.jpg)
> smiling and blinking

### Setup

We will use the API provided by [useapi.net](https://useapi.net) to interact with [PixVerse](https://useapi.net/docs/api-pixverse-v2).

#### Useapi.net

You need a monthly [subscription](https://useapi.net/docs/subscription) to use the [useapi.net](https://useapi.net) APIs mentioned in this article.
Follow these [steps](https://useapi.net/docs/start-here/setup-useapi) to get started and obtain your `USEAPI_TOKEN`.

#### PixVerse

Configure your PixVerse account (one-time) via [POST /accounts/email](https://useapi.net/docs/api-pixverse-v2/post-pixverse-accounts-email). When no account is specified the API randomly selects an available one. Set `PIXVERSE_EMAIL` only to pin requests to a specific account.

Useapi.net provides an easy way to experiment with all API endpoints without writing any code. Check the `Try It` section at the end of each document page, such as PixVerse [videos/create](https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create#try-it) or [files](https://useapi.net/docs/api-pixverse-v2/post-pixverse-files#try-it).

For your convenience, we have published all the [source code](https://github.com/useapi/examples/tree/main/pixverse-demo) used in this article. You can choose between JavaScript and Python examples. Clone this repository locally and use it as a starting point for your experiments.

### Ngrok

Follow official [instructions](https://ngrok.com/docs/getting-started/#step-2-connect-your-account) to sign up for an ngrok account and copy your ngrok `authtoken` from your ngrok dashboard.

### Preparing PixVerse prompts

Edit the locally cloned [prompts.json](https://github.com/useapi/examples/blob/main/pixverse-demo/prompts.json) file. It is a JSON array of prompt objects:

```json
[
    {
        "prompt": "A cinematic shot of a tiny hedgehog …",
        "model": "v6",
        "duration": 5,
        "quality": "720p"
    },
    {
        "prompt": "smiling and blinking",
        "model": "v6",
        "duration": 5,
        "quality": "720p",
        "use_source_image": true
    }
]
```

Set `use_source_image: true` on any entry to upload `./source.jpg` as the first frame and run image-to-video. Omit it (or set it to `false`) for text-to-video.

Per-entry params:

| Field | Type | Default | Description |
|---|---|---|---|
| `prompt` | string | required | Text description of the video |
| `model` | string | `"v6"` | PixVerse model version |
| `duration` | number | `5` | Video length in seconds (`5` or `8`) |
| `quality` | string | `"720p"` | Resolution (`"540p"`, `"720p"`, `"1080p"`) |
| `use_source_image` | boolean | `false` | Upload `./source.jpg` as first frame (i2v) |

### Executing prompts using the PixVerse v2 API by useapi.net

Create a file locally in the same folder named `example.sh` with the following content:

#### [JavaScript](https://github.com/useapi/examples/blob/main/pixverse-demo/example.js)
```bash
USEAPI_TOKEN="useapi API token" NGROK_AUTHTOKEN="ngrok authtoken" node ./example.js
```

#### [Python](https://github.com/useapi/examples/blob/main/pixverse-demo/example.py)
```bash
USEAPI_TOKEN="useapi API token" NGROK_AUTHTOKEN="ngrok authtoken" python3 ./example.py
```

`PIXVERSE_EMAIL` is optional — set it only to pin PixVerse requests to a specific configured account.

Execute it from the command line like this: `./example.sh` and observe the magic of the API.

The generated videos will be saved locally. The pipeline:
1. For each prompt entry with `use_source_image: true`: upload `./source.jpg` via [POST /files](https://useapi.net/docs/api-pixverse-v2/post-pixverse-files) → obtain `first_frame_path`.
2. Submit [POST /videos/create](https://useapi.net/docs/api-pixverse-v2/post-pixverse-videos-create) with `prompt`, `model`, `duration`, `quality`, and optionally `first_frame_path`.
3. Receive the result via ngrok webhook; download the mp4 when `video_status_final` is `true`.

### Conclusion

Visit our [Discord Server](https://discord.gg/w28uK3cnmF) or [Telegram Channel](https://t.me/use_api) for any support questions and concerns.

We regularly post guides and tutorials on the [YouTube Channel](https://www.youtube.com/@midjourneyapi).
