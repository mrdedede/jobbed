import subprocess
import json
from job_scraper import paths 
from db import db_connection
from typing import List, Tuple

with open(paths.GRADE_JOB_MD, "r") as f:
    grade_job = f.read()
    
with open(paths.CV_MD, "r") as f:
    my_cv = f.read()

HAIKU_MODEL = "haiku"
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
    
    result = subprocess.run(
        ["claude", "--model", HAIKU_MODEL, "--json-schema", ANALYSIS_SCHEMA, "-p", final_prompt],
        capture_output=True,
        text=True,
        check=False
    )
    
    if result.returncode != 0:
        print(f"Error running claude: {result.stderr}")
        return None

    try:
        json_analysis = json.loads(result.stdout)
        return json_analysis
    except json.JSONDecodeError:
        print(f"Failed to parse JSON: {result.stdout}")
        return None

def send_claude_request(job: List[Tuple]):
    json_result = _send_claude_request(job[3])
    return json_result