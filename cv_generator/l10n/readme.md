# Localisation Files

Those files should include strings that are the easier to translate

Points that are available in every CV, not mattering the job post (like the defintions of "profile" or "skills") and the spoken language names should be translated and kept here

We are still accepting further entries

## How they are used

`cv_generator/docx_gen.py` fills five placeholders from the CV the model wrote
(`{CV_INTRODUCTION}`, `{PROFILE_TEXT}`, `{SKILLS_LIST_TEXT}`,
`{EXPERIENCES_PLACEHOLDER}`, `{EDUCATION_PLACEHOLDER}`). **Every other `{KEY}`
in your template is looked up here**, in the file matching the posting's
language.

So anything the model does not write, you write once in your own template using
these IDs, and it comes out in whichever of the four languages the posting is
in. The Languages section is the reason this exists — the generator has no
opinion about which languages you speak:

```
{language_pt}, {LEVEL_NATIVE}
{language_fr}, C1 - {LEVEL_FLUENT}
{language_de}, A2 - {LEVEL_BASIC}
```

An ID that no file defines is refused with an error naming it, rather than
printed into the CV you send.

## Keys

| Key | What it is |
|---|---|
| `*_TITLE` | The five section headings, uppercase. ATS parsers key on these, so keep them the locale's standard wording rather than a literal translation. |
| `language_xx` | The name of language `xx`, written in this file's locale. Add a key for any language you list in your template. |
| `LEVEL_*` | Proficiency words: `NATIVE`, `FLUENT`, `INTERMEDIATE`, `BASIC`. Write a CEFR code around them if you want one — `C1 - {LEVEL_FLUENT}` — since the code needs no translating. |

Add a key to **all four files**, not one: a template that names an ID only
`values_fr.json` defines renders fine for French postings and fails for the
rest. `tests/test_docx_gen.py` checks the four files carry identical key sets.
