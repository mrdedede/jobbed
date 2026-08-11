import os
import subprocess

HAIKU_MODEL = "haiku"
SONNET_MODEL = "sonnet"
OPUS_MODEL = "opus"

#: The CLI reads the user's CLAUDE.md files unless told not to, and a grading
#: run must see only the prompt it was handed. This belongs in the environment,
#: not in the argument list: `subprocess.run` with a list does not go through a
#: shell, so `["VAR=1 claude", ...]` names an executable called "VAR=1 claude"
#: and raises FileNotFoundError before any model is ever called.
ENV = {**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def call_claude(model: str, prompt: str, json_schema: str = None):
    if json_schema is None:
        result = subprocess.run(
            ["claude", "--model", model, "-p", prompt],
            capture_output=True,
            text=True,
            check=False,
            env=ENV
        )
    else:
        result = subprocess.run(
            ["claude", "--model", model, "--json-schema", json_schema, "-p", prompt],
            capture_output=True,
            text=True,
            check=False,
            env=ENV
        )

    if result.returncode != 0:
        print(f"Error running claude: {result.stderr}")
        return None

    return result
