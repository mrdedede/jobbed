# Suggesting search terms from a CV, JSON out

The candidate's CV is in the prompt. Suggest the terms that will filter
scraped job postings for them. Your entire reply is one JSON object.

## How the terms are used

Each term is matched case-insensitively, as a whole word or phrase, against a
posting's title, URL and description.

- **Keywords** pick postings: one is kept when it mentions enough of them.
  Suggest what a fitting posting would actually say: role titles ("backend
  developer", "data engineer"), the core languages, frameworks and tools the
  CV shows real use of, and the domain if the CV is specialised. Prefer terms
  recruiters write over the CV's own phrasing. 10 to 25 terms.
- **Blacklist** drops a posting outright when any term appears anywhere in it,
  description included. So a blacklist term must only ever show up in postings
  that are certainly wrong for this candidate: other professions ("nurse",
  "accountant"), contract types or levels the CV rules out ("internship" for
  someone with ten years of experience). Never a word a fitting posting could
  use in passing ("senior", "java", "sales"). 0 to 10 terms; fewer is better.

## Output contract

The shape is enforced by a JSON schema on the call; you only need to fill it:

- `keywords` — array of lowercase strings, each under 100 characters.
- `blacklist` — array of lowercase strings, each under 100 characters.

Write terms in the language the postings will most likely be in, which is
usually the CV's language; add English role titles too when the CV is not in
English. If the CV is empty or not a CV, return two empty arrays.
