# NCAAB lines dashboard

Live page: https://zainsomji.github.io/ncaab-lines/

GitHub Actions (`.github/workflows/track.yml`) runs `cloud_run.py` about every 10 minutes:
it saves the sharp-book average lines from Covers, matches the team notes from the Google
Sheet, rebuilds the dashboard and publishes it with GitHub Pages. Saved lines live in
`ncaab_odds.db` on the `data` branch.

To run an update right away: Actions tab -> "Track NCAAB lines" -> Run workflow.
