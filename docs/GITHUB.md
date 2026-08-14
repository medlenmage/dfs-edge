# Putting this on your GitHub

Step by step, assuming you've used git a little but don't do it daily.

**Before anything else, the important part:** your `.env` file has your
API keys in it. Anyone who gets your Anthropic key can spend your money.
This project's `.gitignore` already excludes `.env`, and step 3 below
verifies it. Don't skip that check.

---

## 1. Check git is installed and knows who you are

```bash
git --version
```

No output? Install it: [git-scm.com/downloads](https://git-scm.com/downloads)
(on Mac, running `git --version` may itself offer to install it).

Then tell git your identity — this gets stamped on every commit:

```bash
git config --global user.name "Joshua Medlen"
git config --global user.email "joshua246@gmail.com"
```

> Use the same email as your GitHub account, or your commits won't be
> linked to your profile. If you'd rather not have your real email in
> public commit history, GitHub gives you a private relay address at
> **Settings → Emails → "Keep my email addresses private"** — use that
> `…@users.noreply.github.com` address instead.

---

## 2. Create the repository on GitHub

1. Go to [github.com/new](https://github.com/new)
2. **Repository name:** `dfs-edge`
3. **Description:** "Personal MLB DFS research dashboard"
4. **Private** — recommended. You can always flip it public later; you
   can't un-publish something that's been scraped.
5. **Do not** tick "Add a README", "Add .gitignore", or "Choose a
   license". You already have those files locally, and adding them on
   GitHub creates a conflict you'd have to untangle on your first push.
6. Click **Create repository**

Leave that page open — you'll need the URL it shows you.

---

## 3. Verify your keys won't be committed

Run this from the project folder **before** you commit anything:

```bash
cd dfs-edge
git init
git add .
git status --short | grep -E "\.env$"
```

**You want NO output from that last command.** Silence means `.env` is
correctly ignored.

If it *does* print `A .env`, stop and fix it:

```bash
git rm --cached .env
echo ".env" >> .gitignore
```

Then run the check again.

Double-check the other direction too — `.env.example` *should* be there
(it's the template, with no real keys in it):

```bash
git status --short | grep env
# expect:  A  .env.example
```

---

## 4. Make your first commit

```bash
git commit -m "Initial commit: MLB DFS dashboard"
```

If git complains that nothing is staged, run `git add .` again first.

---

## 5. Connect to GitHub and push

GitHub gave you a URL on the page from step 2. There are two flavours,
and the choice matters:

### Option A — HTTPS (simpler to start)

```bash
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/dfs-edge.git
git push -u origin main
```

You'll be asked to log in. **Your GitHub password will not work** —
GitHub stopped accepting passwords for git in 2021. You need a Personal
Access Token:

1. [github.com/settings/tokens](https://github.com/settings/tokens) →
   **Generate new token (classic)**
2. Note: "dfs-edge laptop"
3. Expiration: 90 days (or whatever you're comfortable with)
4. Tick the **`repo`** scope
5. Generate, then **copy the token immediately** — it's shown once
6. Paste it when git asks for your password

To avoid pasting it every time:

```bash
git config --global credential.helper store    # Linux
git config --global credential.helper osxkeychain   # Mac
```

### Option B — SSH (better long-term, five more minutes now)

No tokens to rotate, no passwords to paste.

```bash
# 1. Make a key (press Enter at every prompt to accept defaults)
ssh-keygen -t ed25519 -C "joshua246@gmail.com"

# 2. Print the PUBLIC key
cat ~/.ssh/id_ed25519.pub
```

Copy that whole line (starts `ssh-ed25519`, ends with your email), then:

3. Go to [github.com/settings/keys](https://github.com/settings/keys) →
   **New SSH key**
4. Title: "My laptop". Paste the key. **Add SSH key**.

```bash
# 5. Test it - you should see "Hi YOUR-USERNAME! You've successfully authenticated"
ssh -T git@github.com

# 6. Push
git branch -M main
git remote add origin git@github.com:YOUR-USERNAME/dfs-edge.git
git push -u origin main
```

> Only ever share the `.pub` file. The other file (`id_ed25519`, no
> extension) is your private key — it never leaves your machine.

Refresh your GitHub page. Your code is there.

---

## 6. Day-to-day from here

After you change something:

```bash
git add .
git commit -m "Add bullpen strength to the scoring model"
git push
```

That's the whole loop. `git push` on its own works after the first
`-u origin main`.

Useful checks:

```bash
git status              # what have I changed?
git diff                # show me the actual changes
git log --oneline -10   # last 10 commits
```

### Working on a second machine

```bash
git clone git@github.com:YOUR-USERNAME/dfs-edge.git
cd dfs-edge
# then follow the setup steps in README.md - and make a NEW .env,
# because it deliberately isn't in the repo
```

Pull down changes you pushed from elsewhere:

```bash
git pull
```

---

## If you leak a key anyway

It happens. Do this in order:

1. **Revoke the key immediately.** Anthropic:
   [console.anthropic.com](https://console.anthropic.com) → API Keys →
   delete it. The Odds API: regenerate from your dashboard.
2. **Generate a new one** and put it in your local `.env`.
3. *Then* worry about scrubbing git history.

Deleting the file in a new commit is **not** enough — the old commit
still contains it, and if the repo was ever public, assume it was
scraped within minutes. That's why step 1 is first and steps 3+ are
optional cleanup.

To actually purge it from history:

```bash
# git-filter-repo is the current recommended tool
pip install git-filter-repo
git filter-repo --path .env --invert-paths --force
git push --force
```

Again: this does not un-leak the key. Only revoking does.

---

## Optional niceties

**Add a license** if you ever go public. MIT is the usual "do what you
like" choice — GitHub can add one for you via **Add file → Create new
file → `LICENSE`**, which offers a template picker.

**Tag versions** when you hit a state you might want to return to:

```bash
git tag v0.1 -m "First working MLB dashboard"
git push --tags
```

**Branch before big changes** so `main` always runs:

```bash
git checkout -b add-savant-data
# ...work, commit...
git push -u origin add-savant-data
```

Then open a Pull Request on GitHub to merge it — overkill for solo work,
but it's good practice and gives you a readable history of *why* things
changed.
