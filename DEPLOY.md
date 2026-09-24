# Deploying Owl4u

Two parts, both on free plans:

1. **The web page**: a Cloudflare Worker (`web/`) that answers searches and serves the page, with the scores in a Cloudflare D1 database.
2. **The nightly job**: a GitHub Actions workflow (`.github/workflows/nightly.yml`) that re-scores ontologies with a new BioPortal submission and updates D1.

Do part 1 after the version 1.1 notebook run has finished and Step 9 (export) has written the files in `C:\owl4_run\exports`.

---

## Part 1: The web page

All commands run in **Anaconda PowerShell Prompt**. None of them need administrator rights.

### 1. Install Node.js (one time)

```powershell
conda activate rtx5090_env
conda install -c conda-forge nodejs -y
node --version
```

### 2. Create a Cloudflare account and log in

Sign up at dash.cloudflare.com (the free plan is enough). Then, in the `web` folder of this repo:

```powershell
cd C:\path\to\Owl4u\web
npx wrangler login
```

The first `npx wrangler` command asks to install Wrangler; answer `y`. A browser window opens; click **Allow**.

### 3. Create the database and load the scores

```powershell
npx wrangler d1 create owl4u
```

It prints a `database_id`. Open `web\wrangler.toml` and put that value in place of `REPLACE_WITH_YOUR_DATABASE_ID`. (If Wrangler offers to add it to the config for you, you can also say yes and then check that the file has the right id.)

Load the four export files from your run, in this order:

```powershell
npx wrangler d1 execute owl4u --remote --file=C:\owl4_run\exports\d1_schema.sql
npx wrangler d1 execute owl4u --remote --file=C:\owl4_run\exports\d1_data.sql
npx wrangler d1 execute owl4u --remote --file=C:\owl4_run\exports\d1_fts.sql
npx wrangler d1 execute owl4u --remote --file=C:\owl4_run\exports\d1_fts_data.sql
```

Check that the scores are there:

```powershell
npx wrangler d1 execute owl4u --remote --command "SELECT status, COUNT(*) AS n FROM ontology_scores GROUP BY status"
```

### 4. Add the BioPortal API key and publish

```powershell
npx wrangler secret put BIOPORTAL_API_KEY
npx wrangler deploy
```

`secret put` asks for the key and stores it encrypted in Cloudflare (it is never in the repo). `deploy` prints the page address, for example `https://owl4u.<your-name>.workers.dev`.

Open it and search for `diabetes`. The line under the search box shows how long the search took and where the matches came from.

### Updating the scores after a new GPU run

Run the notebook's export step, then load `d1_data.sql` and `d1_fts_data.sql` again (step 3, second and fourth commands). Rows are replaced, so nothing needs deleting first.

---

## Part 2: The nightly job

### 1. Turn on Actions in the fork

GitHub turns Actions off in forked repositories. Open the repo's **Actions** tab and click the button to enable workflows.

### 2. Create a Cloudflare API token for D1

In the Cloudflare dashboard: **My Profile**, then **API Tokens**, then **Create Token**, then **Create Custom Token**.

- Permissions: **Account**, **D1**, **Edit**
- Account resources: your account

Copy the token when it is shown (it is shown once). Also copy your **Account ID** (on the Workers and Pages overview page).

### 3. Add four repository secrets

In the GitHub repo: **Settings**, then **Secrets and variables**, then **Actions**, then **New repository secret**. Add:

| Name | Value |
|---|---|
| `BIOPORTAL_API_KEY` | your BioPortal API key |
| `CLOUDFLARE_API_TOKEN` | the token from step 2 |
| `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare account ID |
| `D1_DATABASE_ID` | the `database_id` from Part 1, step 3 |

### 4. Test it once by hand

**Actions** tab, then **Nightly re-scoring**, then **Run workflow**. For a quick first test, set "Most ontologies to score" to `3`. When it finishes, the run's summary page lists what was scored, and the outputs are attached to the run for 14 days.

After that it runs by itself every day at 07:17 UTC.

### What the nightly job does

- Compares BioPortal's latest submission of every ontology with what is in D1, and scores only new ontologies and new submissions (at most 60 per run; the rest wait for the next run).
- Runs on a CPU-only GitHub runner with 16 GB of RAM. Files over 0.75 GB are skipped, because the largest ontologies (NCIT, MESH, BERO, DRON, DDSS) need more memory than the runner has. For those, the old scores stay on the page and the run summary lists them under "Run these on the GPU machine". Score them with the notebook (`P.evaluate_phase(ctx, only=[...])` after a download), then reload the D1 files as above.
- Uses the same code as the notebook (`batch/owl4_*.py`), including the 1.1 repairs and the fixed hash seed.

### Things to know

- GitHub pauses scheduled workflows in public repositories after 60 days with no commits. GitHub emails a warning first; any commit, or re-enabling the workflow in the Actions tab, restarts it.
- Free-plan limits that matter here: Cloudflare Workers allow 100,000 requests a day, and D1 allows 5 million rows read a day. One search reads at most about 100 rows, so both limits are far above what this page needs.
