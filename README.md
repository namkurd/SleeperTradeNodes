# Trade Network - live setup guide

This turns the trade network page into something that updates itself: once an hour, a
robot (GitHub Actions) checks Sleeper for new trades, prices every 2026+ player using
your Parse.bot feed, and republishes the page. No Excel, no manual copying.

You've never used GitHub Actions before, so this walks through every click. It looks
long only because it explains each step - the actual setup takes about 10 minutes.

## What's in this folder

- `index.html` - the page itself (the same chart you've already seen, wired to load its
  data from `data.json` instead of having it baked in).
- `data.json` - the current dataset. Starts as a copy of the frozen 2021-2025 history;
  the robot rewrites this file every time it finds new trades.
- `frozen_history_2021_2025.json` - the permanent, never-recomputed 2021-2025 record.
  `update_league.py` always starts from this file and only adds 2026+ on top of it.
- `update_league.py` - the script that does the actual work (talks to Sleeper and
  Parse.bot, values new trades, rewrites `data.json`).
- `requirements.txt` - the one Python package the script needs.
- `.github/workflows/update.yml` - tells GitHub "run update_league.py once an hour."

## Step 1 - Create the GitHub repository

1. Go to [github.com](https://github.com) and sign in (or create a free account if you
   don't have one).
2. Click the **+** in the top-right corner -> **New repository**.
3. Name it something like `trade-network` (doesn't matter, this is just a URL slug).
4. Leave it **Public** (GitHub Pages hosting is free for public repos; if you'd rather
   keep it private you can, but that requires a paid GitHub plan for Pages).
5. Click **Create repository**.

## Step 2 - Upload these files

1. On your new (empty) repo's page, click **uploading an existing file**.
2. Drag in `index.html`, `data.json`, `frozen_history_2021_2025.json`,
   `update_league.py`, and `requirements.txt` from this folder. Commit them.
3. The `.github/workflows/update.yml` file needs to land at that exact path
   (`.github/workflows/update.yml`), and GitHub's drag-and-drop uploader doesn't
   preserve folder structure well. The reliable way:
   - Click **Add file** -> **Create new file**.
   - In the "Name your file" box, type `.github/workflows/update.yml` (typing the
     slashes makes GitHub create the folders automatically).
   - Open `update.yml` from this folder on your computer, copy its contents, and paste
     them into GitHub's editor.
   - Click **Commit changes**.

## Step 3 - Add your Parse.bot API key as a secret

This keeps your key out of the public repo while still letting the robot use it.

1. In your repo, go to **Settings** (top tab) -> **Secrets and variables** (left
   sidebar) -> **Actions**.
2. Click **New repository secret**.
3. Name: `PARSE_API_KEY`. Value: your key (starts with `pmx_`).
4. Click **Add secret**.

## Step 4 - Turn on GitHub Pages

1. Still in **Settings**, click **Pages** (left sidebar).
2. Under "Build and deployment" -> "Source", choose **Deploy from a branch**.
3. Branch: `main`, folder: `/ (root)`. Click **Save**.
4. GitHub will show you a URL like `https://<your-username>.github.io/trade-network/`.
   It can take a minute or two to go live the first time.

That URL is your permanent, live link - open it now and you should see the trade
network with the 2021-2025 data (since that's what `data.json` starts as).

## Step 5 - Run the update once by hand to make sure it works

1. Click the **Actions** tab at the top of your repo.
2. Click **Update trade network data** in the left list.
3. Click **Run workflow** -> **Run workflow** (button on the right).
4. Wait ~30-60 seconds, then refresh - you should see a run with a green checkmark.
   Click into it if you want to see exactly what it did.
5. If it's green, you're fully set up. It will now run automatically once an hour,
   and you can always come back to this tab and click **Run workflow** to force an
   update immediately (e.g., right after you make a trade, instead of waiting for the
   next hourly run).

## Troubleshooting: a trade isn't showing up

Work through these roughly in order:

1. **Give it a run.** With the hourly schedule, a trade can take up to ~an hour to
   appear on its own. To skip the wait, go to **Actions** -> **Update trade network
   data** -> **Run workflow** right now.
2. **Check the last run's status.** Still in the **Actions** tab, look at the most
   recent run. A red X means the whole update failed - click into it to read the
   error. By far the most common cause is a missing or expired `PARSE_API_KEY`
   secret (Step 3 above); when that happens, `data.json` isn't touched at all, so
   *every* trade since the last successful run is missing, not just one.
3. **Check the trade itself, if the run is green but the trade still isn't there:**
   - Sleeper marks each trade's `status` as `"complete"` only once any league-level
     trade review/veto period you have configured has passed. If your league has one
     turned on, a very recent trade can sit as `"pending"` until that window closes.
   - Trades involving **3 or more teams** in a single Sleeper transaction aren't
     picked up automatically (see the note above) - those need to be added by hand.
   - A trade that's exclusively "1 player for FAAB dollars" on one side is excluded
     on purpose (FAAB isn't tracked as an asset). A player-for-player trade is never
     excluded by this rule.

## Embedding this in a Google Site (optional)

1. Open your Google Site in edit mode.
2. On the right-hand "Insert" panel, choose **Embed** -> **By URL**.
3. Paste your GitHub Pages URL from Step 4.
4. Google Sites will embed the live page in an iframe. Because the real page lives on
   GitHub Pages and updates itself, the embedded copy in your Google Site updates too
   - you never have to touch the Google Site again.

## The one thing you'll need to do manually, once a year

Sleeper creates a **new league_id every season**. When your 2027 league exists on
Sleeper, open `update_league.py`, find this block near the top:

```python
LEAGUE_IDS = {
    2026: "1389416556617801728",
    # 2027: "<add next year's league_id here once Sleeper creates it>",
}
```

...and add a line for the new season and its league_id (visible in your Sleeper
league's URL). Commit that one-line change on GitHub (you can edit the file directly
in GitHub's web editor - no need to re-clone anything), and the next scheduled run
picks it up automatically. That's the entire yearly maintenance burden.

## How the numbers work (for reference)

- Every player value is normalized to a 0-100 scale **within its own season**, where
  100 = the most valuable player charted that season. This keeps seasons comparable
  even though the value source changes over time.
- 2021-2022: CBS Sports single-QB redraft trade chart. 2023-2025: CBS's 2QB/superflex
  chart (the league went superflex in 2023). Both frozen forever in
  `frozen_history_2021_2025.json` - `update_league.py` never touches them.
- 2026 onward: your Parse.bot feed (12-team, PPR, 2QB, redraft), refreshed roughly
  hourly as the update workflow runs.
- Team defenses and any player never listed on a chart/feed get a small flat
  placeholder (2-3 on the normalized scale) instead of a real market value.
- Trades that are exclusively "1 player for FAAB dollars" are excluded, same as the
  original rule.
- Multi-team trades (3+ rosters in one Sleeper transaction) aren't handled by
  `update_league.py` - this league hasn't had one, but if it ever does, that trade
  will need to be added by hand to `frozen_history_2021_2025.json` in the same format
  as the existing entries.
