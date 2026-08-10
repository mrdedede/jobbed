import subprocess

HAIKU_MODEL = "haiku"
SONNET_MODEL = "sonnet"
OPUS_MODEL = "opus"

def call_claude(model: str, prompt: str, json_schema: str = None):
    if json_schema is None:
        result = subprocess.run(
            ["claude", "--model", model, "-p", prompt],
            capture_output=True,
            text=True,
            check=False
        )
    else:
        result = subprocess.run(
            ["claude", "--model", model, "--json-schema", json_schema, "-p", prompt],
            capture_output=True,
            text=True,
            check=False
        )
        
    if result.returncode != 0:
        print(f"Error running claude: {result.stderr}")
        return None

    return result