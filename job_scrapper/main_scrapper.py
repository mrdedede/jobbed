import requests
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urljoin

INPUT_FILE = "job_pages.csv"
OUTPUT_FILE = "jobs.csv"

headers = {
    "User-Agent": "Mozilla/5.0"
}

links = pd.read_csv(INPUT_FILE)

jobs = []

for _, row in links.iterrows():

    company = row["company"]
    url = row["url"]

    print(f"Processing {company}: {url}")

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=20
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for link in soup.find_all("a", href=True):

            href = link["href"]
            title = link.get_text(" ", strip=True)

            # Ignore empty links
            if not title:
                continue

            # Convert relative URLs to absolute URLs
            job_url = urljoin(response.url, href)

            # Basic heuristic: keep links that look like job postings
            if (
                "/jobs/" in job_url
                or "/job/" in job_url
                or "/offre" in job_url.lower()
                or "/career" in job_url.lower()
                or "/careers" in job_url.lower()
            ):
                jobs.append({
                    "company": company,
                    "title": title,
                    "url": job_url
                })

    except requests.RequestException as e:
        print(f"Request failed: {e}")

    except Exception as e:
        print(f"Unexpected error: {e}")


# Remove duplicates
jobs_df = pd.DataFrame(jobs)
jobs_df = jobs_df.drop_duplicates(subset=["company", "url"])

jobs_df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8")

print(f"\nFound {len(jobs_df)} job postings.")
print(f"Saved to {OUTPUT_FILE}")