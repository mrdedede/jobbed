# Writing the cover letter for one posting, JSON out

The candidate's CV (already tailored to this posting), the grading write-up
done on that posting, the candidate's own answers about why they want this job,
and the posting itself are all in the prompt. Nothing is read from or written to
the database. Your entire reply is one JSON object — this skill exists to be
piped into something else.

## What you are given

- **The tailored CV**, as JSON. The only source of facts about the candidate's
  experience, skills and education.
- **The analysis**, the write-up already produced for this posting. Its
  "what to emphasise" paragraph says what the letter should lead with.
- **The candidate's answers**: why this job interests them, how it fits their
  career goals, why the offer makes sense to them. The only source of the
  candidate's motivation. Any of them can be empty.
- **The job post**, at the end of the prompt.

## The ground rule

Never invent. Not an employer, not a date, not a degree, not a tool, not a
metric, not a responsibility, not a motivation. Every claim about the candidate
traces back to the CV or to their answers. Facts about the company come only
from the job post.

When an answer is empty, do not make one up for it: write less motivation, not
a generic one. "I am passionate about your mission" with nothing behind it is
worse than saying nothing.

## The language rule — MANDATORY

**The letter is written in the language of the JOB POST. Not the language of
the CV, not the language of the analysis or of the candidate's answers, not the
language of these instructions.** Those are often English; the job post often
is not. Translate the candidate's answers into the job post's language as you
use them.

1. Read the job post and decide its language first. That is `locale`.
2. Write the whole letter — greeting, body and sign-off — in that language,
   following that language's letter conventions (e.g. "Madame, Monsieur," and a
   formal closing formula in French).
3. Only proper nouns (company, university, product and tool names) stay as they are.

An English letter for a French posting is a failed answer, even if everything
else is perfect.

## The letter

- 250 to 400 words, three to five paragraphs, plain text.
- A greeting line. Address the hiring manager by name only when the job post
  names them; otherwise use the language's neutral form.
- Opening: the role, as the posting names it, and one sentence on why the
  candidate wants it, drawn from their answers.
- Middle: two or three concrete matches between the posting's main requirements
  and the CV, in the order the analysis says to emphasise. Show them through
  what the candidate did, not adjectives ("passionate", "results-driven",
  "dynamic" carry no information).
- One paragraph on fit with the company and the candidate's career goals, from
  their answers and what the job post says about the company.
- Close: availability for an interview, a sign-off. End with the sign-off
  formula only — the candidate's name is added at render time.
- No placeholders ("[Company address]", "[Your name]", "XX/XX"), no addresses,
  no date, no subject line, no Markdown.
- Paragraphs are separated by one blank line (`\n\n`).

## Output contract

The shape is enforced by a JSON schema on the call; you only need to fill it.

**`locale`** — the language the JOB POST is written in, as `en`, `es`, `fr` or
`pt`. Use `en` only when the job post is in English, or in a language outside
these four (then still write the letter in the job post's language).

**`letter`** — the letter text, as described above.

Nothing outside the JSON object.

## When you cannot write

If the CV is missing or empty, or the prompt carries no posting text, return
`locale: "en"` and an empty string for `letter`. Do not write a letter around
invented facts.
