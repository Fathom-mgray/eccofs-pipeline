# Conventions

## Platform boundary

This repo is the ECCOFS pipeline. The compute, storage, schedule and base
images belong to `fathomscience/aws-batch-hpc`. Every dependency on that
platform goes through the names listed in `INFRASTRUCTURE.md`. If a change
here needs a new platform name, such as a bucket, secret, job-definition
field or base-image path, add it to `INFRASTRUCTURE.md` and open the
matching platform MR.

## Comments vs. docstrings

- **Comments** (`#` in shell/Python/YAML/Dockerfiles) are short: a phrase or
  one sentence. If a comment needs more than that to explain itself, the
  explanation goes in `TECHNICAL.md` (or `container/TECHNICAL.md`), and the
  comment becomes a short pointer to the section, e.g.
  `# Retiles for the actual node count -- see TECHNICAL.md ("Tiling").`
- **Docstrings** are not comments and are not trimmed to pointers. A
  docstring is self-contained and as long as needed to describe what the
  function does and how to change it safely.

## Docs

- The audience is scientists. `README.md` explains the science and the
  workflow in plain terms. `TECHNICAL.md` is the pipeline reference.
  `docs/` holds procedures. `container/` has its own README and TECHNICAL
  pair.
- Docs state the current state as plain, present-tense fact. Don't narrate
  how something was verified or when it was fixed. If a claim needs a
  citation, point at a script or log.
- Don't write sections that only list what is *missing*. State the
  positive fact, or leave it out.

## CI

`rules:changes` globs list the actual code file types (`*.sh`, `*.py`,
`*.in`, ...), never a blanket `**/*`, so doc-only edits don't deploy or
rebuild anything.
