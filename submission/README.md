# Submission assets

Everything the submission form asks for, ready to upload.

| File | Field it goes in |
| --- | --- |
| `recording-guide.pdf` | Not submitted — the shot list for recording the video |
| `thumbnail.png` | **Thumbnail Upload** — exactly 200×200 |
| `agent-on-a-leash.zip` | **Project File** — 5.9 MB, verified to contain no credentials |

## Two blockers to clear before submitting

1. **The repository is private.** Judges opening the GitHub link get a 404.
   Make it public, or add them as collaborators.
2. **The OpenRouter key was pasted into a chat.** Rotate it once the event is
   over.

## Rebuilding the ZIP

If the code changes, rebuild it — and re-run the credential check, because the
archive is only safe as long as `.env` stays excluded:

```bash
./scripts/build-submission.sh
```
