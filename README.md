# CSV-driven Browser Automation Video Recorder + Video Web Service

**Test Task 2** extends the Test Task 1 recorder. Each recorded video is:
- uploaded to a lightweight web service;
- stored on disk, with a record in SQLite;
- available by company name;
- previewed in Chromium.

**Test Task 1** (unchanged) records one `.webm` per CSV row. In each video the browser
opens DuckDuckGo, glides an SVG cursor to the search box, types
`Magento developer in <city>`, searches and clicks the first result.

Python 3.9+. Third-party libraries: `flask`, `requests`, `playwright`; everything else is
the standard library (`sqlite3`, `csv`, ...).

## Files

| File | Task | Contents |
|---|---|---|
| `web_service.py` | 2, new | Flask service and SQLite layer: `POST /upload`, `GET /get_video_url`, `GET /media/<filename>`, `GET /videos` |
| `video_client.py` | 2, new | `UploadVideo(...)` and `GetVideoURL(...)` (exact signatures from the task) |
| `capture_video.py` | 2, modified | New `--server` and `--view-seconds` options: upload and preview after each recorded video |
| `requirements.txt` | 2, modified | `flask`, `requests`, `playwright` (pinned) |
| `qscript.py` | 1, as is | `CaptureVideo_Magento(...)`, `PATH_TO_VIDEO_FOLDER`, `ShowCallout(...)`, visual overlay |
| `input.csv` | 1 | Sample input: UTF-8, `;` delimiter, `"` qualifier |

## Quick start

Install (macOS / Linux; on Windows activate with `.venv\Scripts\Activate.ps1`):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Terminal 1, the web service:

```bash
python web_service.py
```

It listens on `0.0.0.0:8000` and creates `videos.db` and `uploads/` next to
`web_service.py` on first start. To change the address:
`FLASK_HOST=127.0.0.1 FLASK_PORT=9000 python web_service.py`.

Terminal 2, record, upload and preview:

```bash
python capture_video.py --server http://127.0.0.1:8000
```

For every row:
1. A Chromium window records the DuckDuckGo scenario (40–70 s).
2. The video is uploaded and its URL is logged.
3. A second Chromium window opens that URL and plays the video in Chromium's built-in
   player.

```
    saved: /…/video/Muster Shop GmbH.webm
09:57:29 INFO    capture_video:     uploaded: http://127.0.0.1:8000/media/Muster_Shop_GmbH.webm
09:57:30 INFO    capture_video:     preview: video is playing in Chromium
…
10:00:04 INFO    capture_video: Done: 3 ok, 0 failed, 0 skipped; uploaded 3, upload failed 0. Report: /…/logs/report.csv
```

Without `--server` the recorder works exactly as in Task 1. `requests` is imported
only when `--server` is given.

> `requirements.txt` handles Python 3.9: Playwright dropped it in 1.61 and requests in
> 2.33, so on 3.9 it installs Playwright 1.60.0 and requests 2.32.5. Both 3.9 and 3.10+ were
> tested.

## Web service API

| Method | Path | Request | Response |
|---|---|---|---|
| `POST` | `/upload` | `multipart/form-data`: `file`, `company_name` | `200 {"company_name", "video_url"}` |
| `GET` | `/get_video_url` | `?company_name=<name>` | `200 {"company_name", "video_url"}`, or `404` if unknown |
| `GET` | `/media/<filename>` | — | The video file (`video/webm`, supports `Range` for seeking) |
| `GET` | `/videos` | — | All records as a JSON list (optional endpoint) |

Example session:

```bash
curl -F "file=@video/Acme Retail Ltd.webm" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload
{"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}

curl "http://127.0.0.1:8000/get_video_url?company_name=ACME%20GmbH"
{"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}

curl "http://127.0.0.1:8000/get_video_url?company_name=Nobody"
{"error":"company not found","company_name":"Nobody"}          # HTTP 404
```

**Errors.** Every error comes back as JSON `{"error": "..."}`:

| Code | Cause |
|---|---|
| `400` | Missing file, missing or blank `company_name`, empty file, or a file type that is not a video (`.webm`, `.mp4`, `.mkv`, `.mov`, `.ogv`) |
| `404` | Unknown company, or unknown media file |
| `413` | File larger than 500 MB |

### Database

On first start the service creates `videos.db` with the `videos` table exactly as given in
the task: `id`, `company_name UNIQUE`, `file_path`, `video_url`, `created_at`, `updated_at`.
The table holds only the absolute file path and the URL. The video bytes stay on disk in
`uploads/`.

```bash
sqlite3 -header videos.db "SELECT company_name, file_path, video_url, created_at, updated_at FROM videos"
```

### Design notes

- **Re-upload = upsert.** `INSERT … ON CONFLICT(company_name) DO UPDATE` keeps one row per
  company. It updates `file_path`, `video_url` and `updated_at`; `created_at` stays as it was.
- **Atomic file replace.** The upload is written to a temporary file and then moved over
  the old one with `os.replace`, so a URL never serves a half-written video.
- **File names**
  - `secure_filename(company_name)` turns `ACME GmbH` into `ACME_GmbH.webm`, as in the
    task example.
  - Different companies can sanitise to the same name (`ACME GmbH` and `ACME/GmbH`). A name
    already owned by another company gets `_2`, `_3`, ..., so one upload never overwrites
    another company's video.
  - A name with no ASCII letters (`Ромашка`) gets a stable `video_<hash>.webm`.
- **Concurrency**
  - The Flask dev server is threaded. Each request opens its own SQLite connection (kept in
    `flask.g`, closed on teardown).
  - WAL mode and a busy timeout prevent "database is locked".
  - Choosing a file name and writing the row happen under one lock.
  - Tested with 20 parallel uploads: no errors and no duplicate rows.
- **URLs** are built from `request.host_url` (via `url_for(..., _external=True)`), never
  from a hard-coded host. The stored URL reflects the address the uploader used, for example
  `http://127.0.0.1:8000`.
- **Safe file serving.** `/media/` uses `send_from_directory`, which rejects `../` path
  escapes and answers `Range` requests.

## Client functions (`video_client.py`)

```python
from video_client import UploadVideo, GetVideoURL

url = UploadVideo("http://127.0.0.1:8000", "video/Acme Retail Ltd.webm", "Acme Retail Ltd")
url = GetVideoURL("http://127.0.0.1:8000", "Acme Retail Ltd")
```

- **`UploadVideo(server_url, video_path, company_name) -> str`** sends a multipart upload
  (`files={"file": (filename, fileobj, mime)}`, `data={"company_name": ...}`) and returns the
  URL.
- **`GetVideoURL(server_url, company_name) -> str`** returns the stored URL. It raises
  `VideoNotFound`, a subclass of `LookupError`, on 404.
- **Errors.** A non-2xx answer or an unreachable server raises `VideoServiceError`, with the
  HTTP status and the server's error message.

## Recorder options (`capture_video.py`)

| Option | Default | Meaning |
|---|---|---|
| `csv_file` | `input.csv` | Input CSV |
| `--server URL` | off | Upload every recorded video to this web service and preview it |
| `--view-seconds N` | `10` | Preview time per video. `0` keeps the preview open until Enter is pressed |
| `--out` | `./video` | Video folder (overrides `qscript.PATH_TO_VIDEO_FOLDER`) |
| `--retries` | `1` | Extra recording attempts for a failed row |
| `--headless` | off | Hide the recording browser (DuckDuckGo blocks headless, see Notes) |
| `--report` | `logs/report.csv` | Per row: recording status, video, attempts, clicked result, `upload`, `video_url`, `upload_error` |
| `--log-file` | `logs/capture.log` | Debug log with tracebacks |

**How the preview behaves**
- It opens the returned URL with Playwright (`headless=False`) and waits until the `<video>`
  element is actually playing.
- `--view-seconds N`: closes after N seconds, or earlier if you close the window.
- `--view-seconds 0`: waits for Enter. Without a terminal (for example in CI) it closes
  right away instead of hanging.

**Failures**
- A failed upload or preview is logged and recorded in the report. The batch continues with
  the next row.

Exit codes:
- `0`: everything ok;
- `1`: a recording or an upload failed;
- `2`: bad input, or `--server` was given but `requests` is not installed.

## Acceptance checklist (Task 2)

| Item | Verified |
|---|---|
| `pip install -r requirements.txt` works on a clean machine | Fresh virtual environments on Python 3.9.6 and 3.11 |
| `python web_service.py` starts and creates `videos.db` and `uploads/` | Yes; the schema in `videos.db` matches the task |
| `POST /upload` with a video and a company name returns 200 and a working URL | `curl` and `UploadVideo` |
| The returned URL serves the uploaded file (HTTP 200) and plays in a browser | Byte-identical download, `video/webm`, `Range` → 206; plays in Chromium |
| `GET /get_video_url?company_name=...` returns the URL; 404 for unknown names | Yes |
| The database stores `company_name`, `file_path` and `video_url` | Yes; no video bytes in the database |
| Re-uploading the same company updates the record (no duplicate rows) | One row; `updated_at` changes, `created_at` stays |
| `python capture_video.py --server http://127.0.0.1:8000` uploads each recorded video and opens it in Chromium | 3 of 3 rows on the live DuckDuckGo |
| Works end-to-end on a clean machine after `pip install -r requirements.txt` and `python -m playwright install chromium` | Yes, Python 3.11 venv |

Edge cases that were also checked:
- a failed upload (server down) does not stop the batch;
- `--view-seconds 0` without a terminal does not hang;
- the recorder runs without `requests` when `--server` is not given;
- 20 parallel uploads work;
- a file-name collision between two companies is resolved;
- Cyrillic company names work;
- path traversal on `/media/` is rejected.

## Task 1: how the recorder works

For each CSV row (`URL;Company Name;Language;Country;Postcode;City;Street;HouseNo`),
`capture_video.py` calls `qscript.CaptureVideo_Magento(...)`. The function records the
scenario with `record_video_dir=PATH_TO_VIDEO_FOLDER` and `record_video_size=1280×720`.
It closes the context to finalize the file, saves it as
`<PATH_TO_VIDEO_FOLDER>/<company_name>.webm` and returns the absolute path, or `None`.

### Overlay
- **Injection.** The SVG cursor (white arrow, black outline), the yellow highlight, the
  click ripple and the callout are injected with `context.add_init_script`, so they survive
  the full-page navigation of the search.
- **CSP safety.** Styles go through the CSSOM and animations through the Web Animations API,
  with no `<style>` and no `innerHTML`. Strict-CSP pages still render the overlay.
- **No interference.** The overlay has `pointer-events: none` and is topmost.

### Mouse and keyboard
- The mouse moves along an eased, curved path in small steps.
- Clicks are real mouse down/up events, so form submit and link navigation happen as normal.
- The query is typed one character at a time, with 60–180 ms delays.

### Results and the first result
- **Visible selectors only.** DuckDuckGo has a hidden duplicate submit button.
- **Blank results page.** Pages that are still blank after 15 s are reloaded once.
- **Reliable click.**
  - The script waits for the layout to settle and re-aims if the target moved.
  - `elementFromPoint` confirms that the first result is under the cursor; a popup that
    covers it is hidden.
  - `target="_blank"` is removed, so the landing page opens in the recorded tab.

### Failures and file names
- **Failures.**
  - Every attempt has a 120 s budget, and a failed row is retried.
  - On final failure the last partial video is kept as `<name>_FAILED.webm`.
- **File names.**
  - Characters that are not allowed become `_`.
  - Reserved Windows names get a `_` prefix.
  - Duplicate names within one run get `_2`, `_3`, ...

## Notes

- **Headed by default.** DuckDuckGo answers headless Chromium with "Unexpected error"
  (bot protection, which is deliberately not worked around), so the recorder runs headed.
  On a Linux server without a display, use `xvfb-run -a python capture_video.py ...`.
- **Live site.** DuckDuckGo is a live site: the order of results changes between runs, and
  its selectors are constants at the top of `qscript.py`.
- **macOS Python 3.9 warning.** The system Python 3.9 on macOS prints urllib3's
  `NotOpenSSLWarning` (it is built against LibreSSL). The warning is harmless for the local
  `http://` service.
- **Dev server only.** `web_service.py` uses Flask's development server, as the task asks.
  It listens on all interfaces (`0.0.0.0`), so other machines on the network can reach it.
- **Tested on** macOS:
  - Task 2 end to end on Python 3.11;
  - the web service and the client also on Python 3.9.6;
  - the Task 1 recorder on Python 3.9.6, 3.11 and 3.14.

  The code is cross-platform, but it was not run on Windows.
