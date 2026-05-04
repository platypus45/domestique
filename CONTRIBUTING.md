# Contributing to Domestique

Thanks for your interest in contributing. This document explains how to submit
changes that we can accept without creating legal or maintenance problems.

## Developer Certificate of Origin (DCO)

All contributions must be signed off under the
[Developer Certificate of Origin 1.1](https://developercertificate.org/). By
signing off, you certify that you wrote the code (or have the right to submit
it) and that it is licensed under Apache-2.0 compatible with this project.

Sign off every commit with the `-s` flag:

```bash
git commit -s -m "Fix power cap logic for KICKR reconnect"
```

This appends a `Signed-off-by: Your Name <your@email>` trailer. PRs without
sign-off will be asked to rebase.

## What We Accept

- **Bug fixes** — with a clear description of the bug and, where practical, a
  regression test.
- **Features** — ideally discussed in an issue first so we can align on scope.
  Training-science features should cite peer-reviewed sources where claims
  about physiology are made.
- **Documentation** — typo fixes, clarifications, examples, new guides.
- **Tests** — adding coverage to existing code is always welcome.
- **Performance and reliability improvements** — benchmarks or before/after
  numbers appreciated.

## What We Reject

We cannot accept the following. PRs containing any of these will be closed.

- **Code copied from copyleft projects** (GPL, AGPL, LGPL in a way that
  couples it to the binary, SSPL, etc.). Apache-2.0 cannot absorb copyleft.
- **Scraped or re-hosted workout files** (ZWO or otherwise) from commercial
  platforms (TrainerRoad, Zwift, Wahoo SYSTM, etc.). Generate procedurally or
  author originals.
- **Route data scraped from Strava, Zwift, or any service whose terms
  prohibit redistribution.** Use OpenStreetMap, SRTM, or user-supplied GPX.
- **Brand logos** or artwork you do not have rights to distribute.
- **Nutrition database entries** copied from commercial sources (MyFitnessPal,
  Cronometer proprietary data, etc.). Use Open Food Facts or original data.

When in doubt, open an issue before doing the work.

## Code Style

- Follow the style of the surrounding code. Python targets 3.9+.
- Keep functions small; keep side effects contained.
- Don't reformat unrelated code in a feature PR — it makes review harder.
- Run the test suite locally before submitting:

  ```bash
  python -m pytest
  ```

- Keep the frontend dependency-free (no npm, no bundler). Vanilla JS + modern
  browser APIs.

## License Headers

New source files should carry a short header:

```python
# Copyright (c) 2024-2026 Domestique contributors
# SPDX-License-Identifier: Apache-2.0
```

## Pull Request Checklist

Before submitting, confirm:

- [ ] Commits are signed off (`git commit -s`)
- [ ] Tests pass locally (`python -m pytest`)
- [ ] New source files have the license header
- [ ] No third-party IP (copyleft code, scraped workouts, scraped routes,
      proprietary data, unlicensed logos)
- [ ] The PR description explains *why*, not just *what*
- [ ] Any physiology or training-science claims cite a source
- [ ] Public APIs or on-disk formats that changed are noted in the PR

## Reporting Security Issues

Please do **not** open a public issue for security vulnerabilities. Contact
the maintainers privately via the address listed in the repository metadata.
