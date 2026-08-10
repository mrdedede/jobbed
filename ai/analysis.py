import json
from ai import ai
from job_scraper import paths
from typing import List, Tuple

with open(paths.GRADE_JOB_MD, "r") as f:
    grade_job = f.read()
    
with open(paths.CV_MD, "r") as f:
    my_cv = f.read()

# Enforced by the CLI, not by the prompt: asking for "no code fences" in prose
# got ignored often enough to break parsing.
ANALYSIS_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "adequation_grade": {"type": "integer", "minimum": 0, "maximum": 100},
        "depth_analysis": {"type": "string"},
    },
    "required": ["adequation_grade", "depth_analysis"],
})

ANALYSIS_PROMPT = f"""{grade_job}
--------
# User CV
{my_cv}
--------
# JOB POST

"""

def _send_claude_request(job_description: str):
    final_prompt = f"{ANALYSIS_PROMPT}{job_description}"
    
    # (model, prompt, schema) -- the schema and the prompt were swapped here,
    # so every call graded the schema text instead of the posting.
    result = ai.call_claude(ai.HAIKU_MODEL, final_prompt, ANALYSIS_SCHEMA)

    # call_claude already reports and returns None on a non-zero exit.
    if result is None:
        return None

    try:
        json_analysis = json.loads(result.stdout)
        return json_analysis
    except json.JSONDecodeError:
        print(f"Failed to parse JSON: {result.stdout}")
        return None

def analyze(job: List[Tuple]):
    json_result = _send_claude_request(job[3])
    return json_result