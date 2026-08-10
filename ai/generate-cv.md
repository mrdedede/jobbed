# Rewriting the CV for one posting, JSON out

The candidate's CV, the posting, and the grading write-up already done on that
posting are all in the prompt. Nothing is read from or written to the database.
Your entire reply is one JSON object — this skill exists to be piped into
something else.

## What you are given

- **The candidate's CV**, sent alongside this prompt. The only source of facts.
- **The job post**, at the end of the prompt.
- **The analysis**, the write-up already produced for this posting. Its "what to emphasise" paragraph is the ordering instruction: lead with what it names.

## The ground rule

Never invent. Not an employer, not a date, not a degree, not a tool, not a
metric, not a responsibility. Everything in your output must trace back to a line in the candidate's CV.

What you may do is reframe: reword a bullet in the posting's vocabulary,
reorder experiences and bullets so the relevant ones come first, group skills under labels the posting would recognise, and drop what this posting has no use for. Dropping is the main tool — a tailored CV is shorter than the source one, not longer.

A CV that gets the candidate an interview they cannot survive is worse than one that gets no call.

## The fields

**`locale`** — the language the posting is written in, as `en`, `es`, `fr`, or `pt`. Nothing else is supported; when the posting is in another language, or you cannot tell, use `en`. The whole CV body is written in that language, including experiences carried over from a CV written in a different one. Proper nouns (companies, universities, product names) stay as they are.

**`cv_introduction`** — the line under the candidate's name. The role, as this posting names it, plus location. One line, no sentence.

**`profile_text`** — one paragraph, three to five sentences. Who the candidate is in terms of this posting's hard requirements: years, the core stack, the domain. No adjectives that carry no information ("passionate", "results-driven"). Write in the same language as pointed out in the locale field.

**`skills`** — three to five competence groups, each with three to six entries. Label the groups with the posting's own vocabulary where the CV supports it. The skills the posting asks for come first, inside the group and across groups. Every entry appears somewhere in the source CV.

**`experiences`** — professional experiences, most relevant to this posting first, not necessarily most recent. Each carries `role`, `company`, `location` ("City, Country"), `start_date` and `end_date` as "Month, Year" — `end_date` is "Present" for a current job — and `bullets`. Write in the same language as pointed out in the locale field.

At least four bullets each, unless the source CV has fewer for that job. A
bullet names the task, the skills used to solve it, and the result, quantified whenever the source CV quantifies it. Rewrite bullets toward this posting; drop the ones it has no use for. An experience with nothing relevant left in it can be cut entirely — but never cut one just because it is old, gaps in a timeline get asked about.

**`education`** — each entry carries `diploma`, `institution`, `location`,
`period` ("2019 - 2021"), `degree` (the topic and level), and `details` (research topic, GPA — empty string when the source CV has neither). Most recent first. Education is not tailored; it is reported.

## Output contract

The shape is enforced by a JSON schema on the call; you only need to fill it. 
Every field above is required. Nothing outside the JSON object.

## When you cannot generate

If the CV is missing or empty, or the prompt carries no posting text, return the object with `locale: "en"`, empty strings for `cv_introduction` and `profile_text`, and empty arrays for the rest. Do not invent a CV, and do not generate against the example template — a plausible CV full of invented facts is the one failure here that costs the candidate more than an error does.
