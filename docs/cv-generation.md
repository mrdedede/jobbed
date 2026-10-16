# CV generation: design (not yet implemented)

Status: design spike. Nothing here is built except what "What exists" lists.
Owners are proposals; change them freely.

## 1. What exists

| Piece | Where | Notes |
|---|---|---|
| Rewrite a CV for one graded posting | `ai.cv_generation.generate_cv(user_id, job_id, model=SONNET)` | One gateway call; stores a row in `generated_cv` (JSONB `cv`, `locale`, FK to the posting and its `ai_analysis`). Refuses ungraded postings (`RuntimeError`). Uses the user's active CV. |
| JSON to DOCX | `cv_generator.docx_gen.render_docx(blocks, l10n, template) -> bytes` | python-docx, pure Python. Template = `user_info/CV_placeholder.docx` (`paths.CV_TEMPLATE_DOCX`). Has no `__main__`. |
| JSON to PDF | `cv_generator.pdf_gen.render_pdf` | Shells out to `soffice --headless --convert-to pdf`. LibreOffice is not in the API image. |
| Storage helpers | `db.generated_cv` | `insert_generated_cv`, `select_generated_cvs`, `select_job_for_generation`. |

Missing: any HTTP endpoint, a quota for it, a per-user template, watermark handling.

## 2. Endpoint shape

- `POST /v1/me/cvs/generate {job_id, locale?, model?}`: needs an existing grade for `(user, job_id)`, else **409**; needs an active CV, else **409**. Returns **202** `{id, status}`.
- `GET /v1/me/generated-cvs` (list), `GET /v1/me/generated-cvs/{id}` (JSON), `GET /v1/me/generated-cvs/{id}.docx` (rendered on request from the stored JSON; no file storage).
- A Sonnet call is about 20 s (README), against about 10 s for a grade. That is long for a synchronous request behind a gateway or proxy timeout.

Recommendation: **asynchronous.** The POST reserves quota, starts the work as a background task and returns 202. The client polls `GET .../{id}`. This needs a `status` column (`pending|done|failed`), which means making `generated_cv.cv` nullable. If that is too much for the first cut, stay synchronous with a 60 s client timeout, because the existing `_in_flight` guard already limits one run per user. Re-decide when the first timeout is seen.
Owner: backend (proposed: andre).

## 3. Cost control

- Quota kind `cv_generation`, **2 per UTC day per user** (`DAILY_CV_CAP`, default 2), through `db.usage.reserve/refund` (`KIND_CV` already exists). Reserve before the call and refund if it fails.
- Model: reuse the `ANALYSIS_MODELS` whitelist (`api.schemas.allowed_models`); default `sonnet`. Haiku is allowed but writes visibly worse prose.
- 429 with `Retry-After`, same as analyze.

Owner: backend.

## 4. Where rendering runs

- DOCX: python-docx is already a dependency and renders in milliseconds. Do it **in the API process**, on download.
- PDF: needs LibreOffice (about 400 MB image, slow cold start).

Recommendation: **DOCX only in the first release.** PDF is a later, separate worker image (a K8s Job or a second Deployment with `soffice`) fed by the same stored JSON. Users can print a DOCX to PDF in the meantime.
Owner: backend for DOCX; infra for the PDF worker, later.

## 5. Watermark removal

**Deferred, to be defined.** It is a future feature; what the watermark is, where it appears and whether a local model is needed will be discussed later. Until then no recommendation is made, and nothing here (including an Ollama deployment on K8s, which today exists only in compose) depends on it.
Owner: to be assigned when scoped.

## 6. Template

Today the template is one shared file, `user_info/CV_placeholder.docx`, which is a single-tenant leftover that the cleanup deliberately keeps. In a container it would have to be baked into the image or mounted.

Recommendation: **one shared template first.** Move it out of `user_info/` into the repo (for example `cv_generator/templates/default.docx`, committed, since it is a generic file) and change `paths.CV_TEMPLATE_DOCX` accordingly. Per-user upload (a `user_templates` table, a size cap, and a check that all five `TOKENS` placeholders are present) is a separate later feature, because it adds a user-supplied-file attack surface.
Owner: backend.

## Suggested build order

1. Move the template into the repo.
2. Endpoint with the quota (sync, or async if the timeout is a problem).
3. DOCX download.
4. PDF worker.
5. Watermark removal, once scoped.
