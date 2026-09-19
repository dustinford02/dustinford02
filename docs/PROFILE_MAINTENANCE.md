# Maintaining this profile

This is a plain-language guide for keeping the GitHub profile
(`README.md`) current. The system is deliberately review-first: an agent
proposes changes in a pull request, and you decide whether to publish.

## How to ask an agent to refresh the profile

You do not need to write anything technical. A request like this is enough:

> Please refresh my GitHub profile README from the approved sources. Follow
> `AGENTS.md`. Only use verifiable facts, keep clearance, contract dollar
> amounts, contact details, and client-specific program names out of it,
> and open a pull request for me to review. Do not merge it.

The agent will:

1. Read `AGENTS.md` and `docs/PROFILE_SOURCES.md`.
2. Check the approved sources for anything that has genuinely changed.
3. Make no change if nothing material is different.
4. If something changed, update `README.md`, run the checks below, and open
   a pull request describing what changed and what it left out.

## How to review the resulting pull request

1. **Read the summary.** The pull request should say what changed, what was
   deliberately excluded, and any open questions. If it does not, send it
   back.
2. **Check the facts.** Every claim should trace to a source in
   `docs/PROFILE_SOURCES.md`. If you cannot verify something, ask for it to
   be removed.
3. **Check the privacy rules.** Confirm there is no email, clearance
   status, dollar amount, client-specific program name, tracking, or
   overly precise location.
4. **Look at the rendering.** The pull request includes a screenshot of the
   rendered README. You can also open the "Files changed" tab, or run the
   preview yourself (below), to see it as GitHub will display it.
5. **Publish when satisfied.** Merge the pull request yourself. Nothing is
   published to your profile until you merge.

## Running the checks yourself (optional)

If you want to see the profile locally before merging:

```bash
npm ci             # one-time install of the tooling
npm run preview    # serve a live preview at http://localhost:6419
npm run build      # or write a static preview to dist/index.html
npm run lint       # Markdown lint
npm run check:links # confirm links resolve (LinkedIn is skipped; check by hand)
```

The preview sanitizes HTML, so it is safe to render even if the Markdown
ever contains raw HTML.

## What the system will never do

- It will never publish to `main` or merge a pull request for you.
- It will never edit LinkedIn, ServiceNow, or any other account.
- It will never message anyone or turn on an automation by itself.
- It will never invent a credential, date, or metric. When in doubt, it
  leaves the claim out and asks.
