# Regulatory Watch - Module 1

## Run it
    pip install -r requirements.txt
    playwright install chromium      # only if you use "Browser mode" sources
    uvicorn main:app --reload
Open http://127.0.0.1:8000

## What happens
- 9:00 and 18:00 IST: crawl every active source, compare to what was seen before, download PDFs, screen for NBFC relevance, build a draft daily report.
- First crawl of a source = baseline only (nothing reported as new). From the second crawl onward, new / modified / backdated items appear under "Updates for review".
- Failed pages go to Exceptions, and the daily report says they are NOT covered by a "no update" confirmation.
- Optional AI screening: set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT.

## Adding sources
Nothing is pre-loaded. Open **Source register** and either:
- **+ Add source** for one page, or
- **Bulk add** to paste many URLs (one per line) or upload a CSV with columns `regulator,name,url`. Rows are checked first; duplicates and bad addresses are skipped and reported.

Use **Test this page** to confirm the crawler finds the circular list before saving. Where a page has many unrelated links,
set the "watch only this part of the page" selector so only the notification list is watched.

## Settings
Copy your Azure OpenAI details into `.env` (see the file for the names). Leave blank to use keyword screening only.
