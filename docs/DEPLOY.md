# Deploying jobbot

A one-time walkthrough for getting jobbot running unattended on GitHub
Actions, polling every 20 minutes for free. Do these steps **in order** —
step 4 in particular is not optional; skipping it is the single most likely
way to have a bad first day with this bot (see the note at the end of that
step).

## 1. Create a public GitHub repo, push

GitHub Actions' free scheduled-workflow minutes require a public repo (a
private repo works too if you're on a paid plan with Actions minutes to
spare, but the free tier assumed by this project's zero-cost rule means
public).

```bash
gh repo create jobbot --public --source=. --remote=origin
git push -u origin master
```

(Or create the repo in the GitHub UI first, then `git remote add origin
<url>` and push — whichever you're used to.)

## 2. Create the Discord webhook

In the Discord channel you want postings to land in:

1. Click the gear icon next to the channel name (**Edit Channel**).
2. **Integrations** → **Webhooks** → **New Webhook**.
3. Name it (e.g. "jobbot"), then click **Copy Webhook URL**. It looks like
   `https://discord.com/api/webhooks/123456789012345678/AbCdEf...`.

Repeat for a second channel if you want operational errors (a source
repeatedly failing) kept separate from job postings — this is optional;
`JOBBOT_DISCORD_ERROR_WEBHOOK_URL` is unset by default and errors simply
aren't reported to Discord if you skip it.

## 3. Add the webhook as a repo secret

In the GitHub repo (not your account, the repo):

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `JOBBOT_DISCORD_WEBHOOK_URL`, value: the URL you copied in step 2.
- Repeat with name `JOBBOT_DISCORD_ERROR_WEBHOOK_URL` if you made a second
  webhook.

Secrets are write-only in the GitHub UI after saving — you can replace a
secret's value but never read it back, so keep the webhook URL somewhere
safe if you'll need it again.

## 4. Run the seed locally, then commit the state file

```bash
uv sync
uv run python -m jobbot.run --seed
git add jobbot_state.db
git commit -m "seed: baseline the board"
git push
```

`--seed` fetches every configured company's current postings and records
every single one as *already published*, without sending a single Discord
message. This is what `jobbot_state.db` (the SQLite state file) is for: it's
how the bot knows "already announced" from "genuinely new" on every run
after this one, and it's the file `.gitignore`'s `!jobbot_state.db`
exception exists to let you commit (see `.gitignore` — every other `*.db` in
the repo stays ignored).

**This step is not optional.** If you skip it and just turn the schedule on,
the very first scheduled run finds an *empty* state database, and every
currently-open posting across every configured company looks brand new —
the bot will post all of them, all at once, to your Discord channel. For a
handful of `hot`-tier companies that's dozens of messages; across a larger
`companies/` directory it's a flood. Seeding first means the first *real*
post the channel ever sees is a genuinely new opening, not a backlog dump.

## 5. Enable Actions, verify with a dry-run dispatch

Actions may need enabling on a freshly-created repo: **Settings → Actions →
General → Allow all actions and reusable workflows** (if it isn't already).

Then, **Actions tab → Poll → Run workflow**, set `dry_run` to `true`, run it.
This exercises the entire workflow — checkout, `uv sync`, running the bot,
the state-commit step — without a single Discord request, so you can confirm
the mechanics work (permissions to push, secrets wired correctly, no config
typos) before anything can actually post. Check the run's log for a clean
exit; a `jobbot_published_count=0` line (or whatever your board's dry-run
found) near the end is expected and fine.

## 6. Turn it loose

Nothing else to do — `.github/workflows/poll.yml`'s `schedule` trigger polls
every 20 minutes on its own from here. The next few runs are worth watching
(step 7) just to build confidence, but no further action is needed.

## 7. Checking on it

- **`uv run python -m jobbot.run --stats`** (locally, after pulling the
  latest `jobbot_state.db`, or via `gh workflow run` / a manual checkout) —
  total jobs tracked, how many published, pending, stale, disappeared, a
  per-company breakdown, and the 10 most recently published titles. This is
  the fastest way to sanity-check the bot without opening the SQLite file by
  hand.
- **The Actions tab** — every poll run's log, green or red, with the exit
  code and (on success) the published count in that run's state-commit
  message.
- **The error channel** (`JOBBOT_DISCORD_ERROR_WEBHOOK_URL`, if configured)
  — a single summarized Discord message per run when one or more sources
  failed, rather than silence or a flood of individual failure posts.

## 8. Troubleshooting

**Exit 1** — every configured source failed to fetch this run. State
(including the per-source failure counters) was still committed; the job is
marked red so it's visible, but nothing is lost. Usually transient (an ATS
having a bad moment) — check whether it clears on the next scheduled run
before doing anything. If it persists, the failing company's board token may
have changed or the company switched ATS; re-verify it (`python -m
jobbot.discover` against that one company's site is a fast way to check).

**Exit 2** — a config or environment problem: malformed yaml, a missing or
malformed `JOBBOT_DISCORD_WEBHOOK_URL`. Nothing is committed on exit 2 — the
workflow fails immediately rather than risk committing state from a run that
never really executed. Run `uv run python -m jobbot.run --check` (locally or
by adding a step to the workflow temporarily) to get a plain PASS/FAIL line
per thing that could be wrong, without it ever printing the webhook URL
itself.

**A source starts failing repeatedly**: check `--stats`' per-company counts
for a company that's stopped growing, or watch the Actions log / error
channel for its name recurring. Re-run discovery against just that company's
website to confirm its ATS and identifier haven't changed; if they have,
update its entry in `companies/*.yaml` by hand (never auto-promoted, see the
README's "Finding companies to poll" section).

**Scheduled workflows get disabled after 60 days of repo inactivity.** This
is a GitHub-wide policy, not something jobbot does: if a repository sees
absolutely no activity (no commits, no runs) for 60 days, GitHub
automatically disables its scheduled workflows, and someone has to manually
re-enable them from the Actions tab. This is exactly what the state commits
prevent — every successful poll run that changes `jobbot_state.db` is a
commit, so a bot that's actually finding and posting jobs keeps the repo
active and the schedule alive on its own. A company list quiet enough to go
60 days without a single new posting is the one scenario where you'd need to
check the Actions tab and manually re-enable the schedule.
