# Assessing one posting, JSON out

The posting is in the prompt. Nothing is read from or written to the database.
Your entire reply is one JSON object — this skill exists to be piped into
something else.

You report facts and write the explanation. You do not give a grade: code computes it from what you report, so report what the CV and the posting show, not what would lead to a number. The candidate decides whether to spend an evening on a cover letter based on this, so be blunt.

## Read the posting

Judge the description, never the title. Titles are marketing; the requirements are in the body.

List in `requirements` what the posting asks of the candidate, one entry per requirement:

- `text` — the requirement in a few words: "5 years of Go", "French, C1", "Master's degree".
- `kind` — `skill`, `experience`, `education`, `language`, `certification`, or `other`.
- `importance` — `required` when the posting demands it, `preferred` when it is phrased as a bonus or a wish ("nice to have", "ideally", "a plus"). When the posting does not say, a core part of the job is `required` and a peripheral library is `preferred`.
- `status` — see below.
- `evidence` — the CV experience that answers it, by name: "Five years of Go at Company 1". For `missing`, say what the CV shows instead.
- `quote` — the line of the posting's description this comes from, copied character for character, in the posting's own language. Do not translate, fix or shorten it past the words you need. A requirement you cannot quote from the description does not belong in the list.

Leave out culture lines, benefits, and anything describing the company rather than the candidate.

**Never list work authorisation, visas, salary or availability**, even when the posting mentions them. They are asked on the application form and a CV says nothing about them. Never infer anything about the candidate from their nationality, name, or where they studied.

## Compare it with the CV

Set each requirement's `status`:

- `met` — the CV shows it.
- `transferable` — the CV does not show it, but shows something that carries over directly: a closely related language or tool, or the same work in a neighbouring domain. Say which in `evidence`.
- `missing` — the CV shows neither.

A language is `met` only when the CV shows the level asked for.

## Years of experience

- `required_years` — the minimum number of years the posting asks for. For a range ("3-5 years"), the lower bound. If it sets several minimums, the one for the core of the job. `null` when it states no number: do not infer one from words like "senior".
- `experiences` — every professional experience on the CV, with its `company`, `role`, `start` and `end` as "YYYY-MM", exactly as the CV dates it. `end` is `null` for an ongoing role, which runs up to today's date, given alongside this prompt. If the CV gives only a year, use January for a start and December for an end.
- `relevant` — `true` when the role is the same as this posting's or directly transferable to it. Studies are not experiences.

Do not add the years up; code does that.

## Location

- `onsite_country` — the country, only when the posting requires working on-site and that country differs from the one on the CV. `null` otherwise, including for remote and hybrid-from-anywhere roles.
- `place` — optional. Where the job itself is done, as a city or region ("Gent", "Toulouse", "Île-de-France"). Read it from the body or the title — here, unlike for the requirements, the title counts ("Ingénieur (Gent)", "Développeur à Toulouse"). Never the company's head office or postal address. Leave the field out if the posting does not say; do not guess.

## The write-up

`depth_analysis` is the whole write-up, `\n\n` between paragraphs. A few paragraphs, in plain prose, covering three things:

1. **Matching strengths** — which CV experiences answer which requirements, by name. "Five years of Go at Company 1 covers the backend requirement", not "good backend match".
2. **The gaps that matter** — what is missing and whether it is disqualifying or arguable. Say plainly when the posting is a stretch.
3. **What to emphasise** — the two or three things the application should lead with, given this specific posting.

Skip the preamble and the summary of the job description. The candidate has read it; they want the verdict. Do not state a grade or a score: code adds that below your write-up.

## Output contract

The shape is enforced by a JSON schema on the call; you only need to fill it.

## When there is nothing to assess

If the description is truncated, or is boilerplate with no requirements in it, leave `requirements` empty and say in `depth_analysis` that the description carried nothing to judge. Do not invent requirements, and do not grade against the example template.
