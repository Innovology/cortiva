# GitHub workflow

Your terminal environment carries `GH_TOKEN` (and usually `GITHUB_ORG`)
when your employer has granted you GitHub access. The `gh` CLI picks up
`GH_TOKEN` automatically — never print, log, or write the token anywhere.

## Creating and managing issues

1. **Search before you create** — avoid duplicates:
   `gh issue list -R "$GITHUB_ORG/<repo>" --search "<keywords>" --state all`
2. Create with a clear, action-oriented title and a body that states
   context, evidence, and the definition of done:
   `gh issue create -R "$GITHUB_ORG/<repo>" --title "..." --body "..." --label "..."`
3. When your work changes an issue's state, say so on the issue:
   `gh issue comment <num> -R "$GITHUB_ORG/<repo>" --body "..."` and
   close with `gh issue close <num> -R ... --comment "..."` when done.
4. Reference issues from commits and wiki pages as `org/repo#123` so
   everything cross-links.

## Keeping project boards current

- List boards: `gh project list --owner "$GITHUB_ORG"`
- Add an issue to a board:
  `gh project item-add <number> --owner "$GITHUB_ORG" --url <issue-url>`
- Move/edit items (status, fields):
  `gh project item-edit --id <item-id> --field-id <field-id> ...`
  (discover ids with `gh project field-list` / `gh project item-list`)
- Treat the board as the single source of truth for state: if you start,
  finish, or block on something, reflect it on the board the same day.

## Wikis for product thinking

Repo wikis are where you develop and maintain durable product thinking —
briefs, opportunity assessments, decision records, research notes. Issues
are for *work*; the wiki is for *thinking that outlives the work*.

A wiki is a plain git repo:

```sh
git clone "https://x-access-token:${GH_TOKEN}@github.com/$GITHUB_ORG/<repo>.wiki.git"
```

1. Pages are Markdown files; the filename (minus `.md`) is the page title.
2. Use stable, scannable names: `Product-Brief-<topic>.md`,
   `Decision-Record-YYYY-MM-DD-<topic>.md`, `Research-<topic>.md`.
3. Update pages in place as thinking evolves — a wiki page is a living
   document, not an append-only log. Record significant reversals in a
   short "History" section at the bottom of the page.
4. Link to the issues that came out of the thinking, and from those
   issues back to the page.
5. Commit with a message describing the change in thinking, then
   `git push`. Never commit the token or any URL containing it; clone
   into a scratch directory and remove it (`rm -rf`) when done so the
   token-bearing remote URL doesn't linger.

## Conduct

- You act as your own named bot account; your work is attributed and
  auditable. Write like a colleague, not a script.
- Stay within the repos of your employer's organisation (`$GITHUB_ORG`).
- If a command fails with a permissions error, escalate to your employer
  rather than working around it.
