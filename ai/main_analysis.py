import sys
from db import db_connection
from ai import analysis

def main() -> int:
    """Scrape all boards and write results to CSV.

    Returns:
        0 on success.
    """
    jobs = db_connection.select_jobs_to_analyse(20)
    for job in jobs:
        json = analysis.send_claude_request(job)
        try:
            db_connection.insert_analysis([json["adequation_grade"],
                json["depth_analysis"], analysis.HAIKU_MODEL, job[0]])
        except:
            print("Could not make this one.")

if __name__ == "__main__":
    sys.exit(main())