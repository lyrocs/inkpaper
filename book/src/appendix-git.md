# Appendix B: Following along in git

Every chapter starts with a **Where we are** box listing commit hashes. Checking
those commits out lets you see the code exactly as it was at that step, which is
often simpler than today's version and closer to the idea being introduced.

## Get the full history

If you cloned with `--depth`, or your copy came from a CI system, the history may
be truncated. Check, and fetch the rest if needed:

```bash
git rev-parse --is-shallow-repository   # prints "true" for a shallow clone
git fetch --unshallow
```

## Look at a commit

```bash
# what changed, file by file
git show --stat dceaacd

# the full diff
git show dceaacd

# one file as it was at that commit, without checking anything out
git show dceaacd:crates/ui/src/div.rs

# the files that existed at that commit
git ls-tree -r --name-only dceaacd
```

## Check out a commit

```bash
git switch --detach dceaacd   # or: git checkout dceaacd
cargo test -p inkpaper-ui     # early commits may need an older toolchain; see below
git switch -                  # go back to where you were
```

Early commits were written against the Rust toolchain of their time. If a very
old commit fails to build with your current compiler, reading it is still useful:
`git show <hash>:<path>` needs no build at all.

## Find the history of an idea

```bash
# every commit that touched a file, oldest first
git log --reverse --oneline -- crates/ui/src/slot_table.rs

# commits whose message mentions a word
git log --oneline --grep "damage"

# when a line of code appeared or disappeared ("pickaxe")
git log --oneline -S "AvailableSpace::Unbounded"

# who last changed each line of a file, and in which commit
git blame crates/ui/src/layout/flex.rs
```

## A suggested reading path

| Chapter | Start here |
|---------|------------|
| 2 | `git show dceaacd` (the whole first commit) |
| 3 | `git show d3cf9df -- crates/ui/src/entity.rs` |
| 4 | `git show 496fe14 --stat`, then today's `crates/ui/src/slot_table.rs` |
| 5 | `git show f309c6d` |
| 7 | `git show 8638df7 -- crates/ui/src/layout/mod.rs` (sentinel → enum) |
| 9 | `git log --oneline 0c8b3f7~1..1d215f6` (the damage week) |
| 11 | `git show 2f7f212` (the first driver) |
| 13 | `git show 83cd7e5 --stat` (the first firmware) |
| 15 | `git log --oneline d1f5f46~1..858ea16` (the EPUB day) |
| 19 | `git show 4abd05f` (the first `rsx!`) |
| 20 | `git show 20ed551` (the first trace recorder) |

## Reading commit messages

InkPaper's commit messages describe **behaviour**, in the imperative, without a
prefix: "wait on the GT911 interrupt instead of polling while idle", "skip book
references that point outside it instead of failing to open". Reading
`git log --oneline` from top to bottom is a fast way to learn what the project
can do and in what order it learned to do it. That's how this book was planned.
