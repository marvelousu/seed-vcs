# Git bridge

`seed_git.py` is a deliberately one-way bridge between a Git repository and a
Seed repository. It is for carrying a reviewed snapshot across the boundary;
it is not a synchronizer and it never treats later Git edits as Seed changes.

Import accepts one full or unambiguous hexadecimal Git commit ID and creates a
single Seed proposal from that tree. The target Seed repository must be empty.
The imported commit is recorded in the proposal message, so a retry after a
successful landing returns the same Seed revision instead of importing it
again.

```powershell
python seed.py --db .\seed.db init
python seed_git.py --db .\seed.db import --git D:\work\source --commit <git-commit>
```

For the HTTP service, provide the same token used by the Seed client. HTTPS is
the default; `--allow-http` is required for an explicitly local HTTP service.

```powershell
$env:SEED_TOKEN = "..."
python seed_git.py --url https://seed.example --repo example import `
  --git D:\work\source --commit <git-commit>
```

Only regular `100644` files with UTF-8 paths are supported. Imports reject
executable files, symlinks, submodules, empty trees, invalid paths, and trees
larger than 1,000 files. Each blob is limited to 1 MiB and the imported tree
to 8 MiB. Git object sizes are checked before any blob bytes are read. This
keeps the imported representation within Seed's ordinary file model.

Export writes one pinned Seed revision to a dedicated Git branch whose name
starts with `seed/`. The branch is updated with Git plumbing only; the bridge
does not check it out and rejects a branch that is checked out in any linked
worktree.

```powershell
python seed_git.py --db .\seed.db export --git D:\work\destination `
  --revision <seed-revision> --branch seed/reviewed
```

An export commit includes a `Seed-Revision` trailer. The bridge records a
private `refs/seed/exports/<seed-revision>` mapping and checks the branch
lineage before advancing it. A retry after interruption between the branch
update and mapping update finds the trailer, verifies its tree against the
requested Seed revision, and restores the mapping. Existing non-Seed branches,
unrelated `seed/` branches, symbolic refs, divergent lineages, and a matching
trailer with different contents are rejected instead of being overwritten.

Git is invoked with argument lists, never through a shell. The bridge removes
inherited `GIT_*` environment variables, disables global/system Git
configuration, and disables hooks and signing for its plumbing commands. This
prevents a caller's Git context from redirecting the bridge into another
repository and prevents local hook side effects. It does not make a Git
repository trusted: choose the Git commit and Seed revision deliberately.
