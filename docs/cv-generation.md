# CV generation

Rewrites the user's active CV for one posting they already graded. The model
returns structured JSON; the browser renders it to DOCX or PDF.

## Endpoints

All need `Authorization: Bearer <jwt>` and act on the token's user.

| Method | Path | Body | Answer |
|---|---|---|---|
| POST | `/v1/me/cvs/generate` | `{"job_id": "<uuid>"}` | **202** `{"generation_id": "<uuid>"}`. **409** `generation already running`. **429** with `Retry-After` when the daily cap is reached. |
| GET | `/v1/me/cvs/generate/{generation_id}` | – | `{"status": "running"\|"done"\|"failed", "cv": GenCvOut\|null, "error": {"status", "detail"}\|null}`. **404** for an unknown id, another user's, one finished over 1 h ago, or one lost to an API restart. |
| GET | `/v1/me/generated-cvs` | – | `[GenCvOut]`, newest first. |

`GenCvOut` is `{cv_id, title, company, adequation_grade (nullable), locale, cv, job_id}`.

Failures inside the run are reported in the poll body, not as the HTTP status:

| `error.status` | `error.detail` | Meaning |
|---|---|---|
| 409 | `no analysis for job` | The posting has no grade for this user. |
| 409 | `no active CV` | Add or activate a CV. |
| 502 | `the AI model call failed` | The gateway or model failed. |
| 502 | `the model's answer was unusable` | The answer was not JSON of the expected shape. |
| 503 | `interrupted` | The API shut down mid-run. |
| 500 | `generation failed` | Anything else (logged). |

A `done` status with `cv: null` means the CV was stored but could not be read
back; list `/v1/me/generated-cvs` instead.

## How it runs

- The POST reserves one unit of the `cv_generation` quota, starts an asyncio
  task and returns at once. The task calls `ai.cv_generation.generate_cv`
  (always Sonnet), which stores a row in `generated_cv`.
- Status lives in an in-memory registry in `api/routers/user.py`. Finished
  entries are pruned after an hour. A restart loses it, which the poll reports
  as 404.
- One generation per user at a time (process-local lock). This is why the API
  runs a single worker and the Deployment uses `strategy: Recreate`.
- **Quota:** `DAILY_CV_CAP` (default 2) per user per UTC day, separate from
  `DAILY_ANALYSIS_CAP`. The unit is refunded when the run fails before a CV is
  stored, including when the API shuts down (the lifespan cancels running
  tasks). It is kept once the CV is stored.

## Rendering

The API stores JSON only. The front end turns it into DOCX (`docx` npm
package) and PDF (print dialog) in the browser, with localized headings and a
profile (name, contact, photo) the user keeps in the browser. The Python
renderers in `cv_generator/` (`docx_gen`, `pdf_gen`) are library code for local
use. They are not in the API image, which has neither the template nor
LibreOffice.

## Not built

- Moving status to a table, which the API needs before it can run more than
  one pod.
- Per-user DOCX templates.
- Watermark removal (to be scoped).
