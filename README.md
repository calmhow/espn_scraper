1. Install python libraries using pip:

  `
  pip install fastapi uvicorn httpx playwright
  `

  `
  playwright install chromium
  `

2. Run the test imports python script and ensure an "ok" output is produced.

3. Run the live_espn_perfect_brackets.py and ensure no errors are returned (only 200's should be seen for html).

4. open a browser and go to http://127.0.0.1:8000/ to see the amount of possible current brackets still standing.
