# Vault git workflow — private repo, single writer, Mac read access (ADR 0010 §4)

The vault is the operator's Obsidian knowledge base. As of ADR 0010 it is a
**git repository**: the coordinator commits on curated promotions, and the
vault watcher (`turing-vault-watcher.service`) reindexes the vector store by
**diffing each new commit against its parent and reindexing only the changed
markdown files**. The commit log is therefore the reward-signal audit trail —
every promotion the operator curates is one commit.

This document is the operator runbook for the parts that touch **your private
data** and **your own machines**. The code (the watcher, the diff/reindex
logic, the systemd unit) is already in the repo; the steps below are the
operator-gated bringup that cannot be automated for you because they create a
private GitHub repo and move your existing notes.

> **The single-writer invariant (read this first).**
> The coordinator's git working tree on WSL2 is the **only writer** to the
> vault. Every other device — your Mac, your phone, anything else — is a
> **read-only git remote**: it `git pull`s, it never pushes during normal
> operation. **External Obsidian Sync of the same vault is incompatible**:
> Obsidian Sync and the coordinator's git would be two independent writers to
> the same files, and you would get silent overwrites and a corrupted reward
> audit trail. Pick git. Turn Obsidian Sync **off** for this vault. (ADR 0010
> §4 and the Consequences section — the operator is on board with this trade.)

---

## 1. Create the private GitHub repo (operator step — private data)

Do this once. It is **not** automated because it creates a repo under your
account holding your private notes.

```bash
# From any machine with the gh CLI authenticated as you.
gh repo create <your-username>/turing-vault \
  --private \
  --description "Turing operator vault — single-writer git, indexed by the coordinator"
```

Or via the GitHub web UI: **New repository → Private → name `turing-vault` →
do not add a README/.gitignore** (you will seed it from your existing vault in
step 2).

Keep it **private**. The vault holds personal notes and the cluster's grounded
research drafts; it is never public.

## 2. Move your existing vault content into the repo (operator step)

On the machine that currently holds the canonical copy of your Obsidian vault:

```bash
cd /path/to/your/existing/Obsidian/Vault

# If Obsidian Sync was managing this vault, disable it for this vault now
# (Settings → Sync → turn off) BEFORE initialising git, so there is exactly
# one writer from this point forward.

git init
git branch -M main

# Recommended .gitignore — keep Obsidian's per-machine workspace state and any
# local caches out of the reward audit trail.
cat > .gitignore <<'EOF'
.obsidian/workspace*.json
.obsidian/cache/
.trash/
.DS_Store
EOF

git add -A
git commit -m "vault: seed from existing Obsidian content"
git remote add origin git@github.com:<your-username>/turing-vault.git
git push -u origin main
```

The vault watcher's **first** index build (cold start) diffs the root commit
against the empty tree, so this seed commit indexes the entire markdown tree
once. Every subsequent commit only reindexes its own diff.

## 3. Clone onto the coordinator (operator/hardware step)

This happens inside WSL2 on the Surface, as the `turing` service user, and is
itemised in `scripts/setup-coordinator.sh` Phase 6 (vault). On a fresh
coordinator:

```bash
sudo -u turing -H git clone git@github.com:<your-username>/turing-vault.git \
  /home/turing/vault
```

The clone **must** live on WSL2 ext4 (`/home/turing/vault`), **not** under
`/mnt/c` — `/mnt/c` does not deliver reliable inotify and is slow for git. The
watcher reads from `TURING_VAULT_ROOT` (default `/home/turing/vault`); override
it in `/etc/turing/coordinator.env` only if you cloned elsewhere.

Obsidian on the Surface opens this same vault over the WSL filesystem bridge at:

```
\\wsl$\Ubuntu\home\turing\vault
```

(Read/edit from Obsidian on Windows if you like — but commits are still the
coordinator's job; see the single-writer invariant above.)

## 4. Mac-side read access — `git clone` then `git pull` (operator step)

Your Mac is a **read-only consumer** of the vault. Clone once, then pull to
refresh whenever you want the latest curated state in Obsidian on the Mac.

```bash
# One-time, on the Mac:
git clone git@github.com:<your-username>/turing-vault.git ~/Vaults/turing-vault

# Point Obsidian (Mac) at ~/Vaults/turing-vault and turn OFF Obsidian Sync for it.

# To refresh with the coordinator's latest curated promotions:
cd ~/Vaults/turing-vault
git pull --ff-only
```

`--ff-only` is deliberate: if a `pull` ever refuses to fast-forward, that is
the single-writer invariant catching a second writer. **Do not** `git merge`
or `git push` from the Mac to "fix" it — investigate which device wrote
out-of-band. The expected steady state is: coordinator commits & pushes, every
other device only pulls.

Optional convenience: a launchd/cron `git -C ~/Vaults/turing-vault pull
--ff-only` on a timer keeps the Mac copy fresh without manual pulls. Still
pull-only.

---

## How the watcher reindexes (reference)

You do not run this by hand — `turing-vault-watcher.service` does — but for
debugging:

- On start it catches the in-memory vector index up to `HEAD` (a cold start
  indexes the whole markdown tree once).
- Then it polls `git HEAD` every `TURING_VAULT_POLL_INTERVAL_SECONDS` (default
  5 s). When `HEAD` advances, it runs the equivalent of:

  ```bash
  git diff --name-status -z <prev>^ <prev>   # for each new commit
  ```

  and applies the diff to the vector index:
  - **added / modified** markdown → re-embedded and upserted,
  - **deleted** markdown → dropped from the index,
  - **renamed** markdown → old path dropped, new path added,
  - non-markdown files (images, attachments, `.gitignore`) → ignored.

- Reapplying a commit the watcher already holds is a no-op (idempotent), so a
  restart that re-reads the same `HEAD` does not double-index.

Check it:

```bash
sudo systemctl status turing-vault-watcher --no-pager
sudo journalctl -u turing-vault-watcher -f      # watch vault_watcher.reindexed lines
```

---

## Operator / hardware checklist (cannot be automated for you)

- [ ] Private `turing-vault` repo created under your GitHub account (step 1).
- [ ] Existing Obsidian vault content committed and pushed to it (step 2).
- [ ] **Obsidian Sync disabled** for this vault on every device.
- [ ] Vault cloned to `/home/turing/vault` on the coordinator (step 3 /
      setup-coordinator.sh Phase 6).
- [ ] Mac clone created and Obsidian (Mac) pointed at it, **pull-only** (step 4).
- [ ] `turing-vault-watcher.service` enabled and `vault_watcher.cold_start_complete`
      seen in the journal.
