## [2026-05-02] Invite Tracking Count Fix + One-Time Startup Patch Posting

- Fixes:
  - Fixed invite counts resetting to 0 after bot restarts by persisting `INVITE_COUNTS` to `invite_counts.json`.
  - Fixed invite detection for invites created after bot startup (new invite codes with existing uses now count correctly).

- Newly Added:
  - Added startup one-time patchlog trigger via `post_patchlog_once.flag`.
  - Startup now always sends boot marker, but only posts patch notes when the one-time flag file exists.

- Notes:
  - The one-time flag is consumed (deleted) after use, so patch notes are not reposted on every startup.

## [2026-04-29] Server Health Stability + Dyno Card V2

- Fixes:
  - Fixed `/serverhealth` runtime crash (`NameError: gid is not defined`) that caused "application did not respond".
  - Added safer XP leader lookup by normalizing member IDs before guild member resolution.

- Bug Patches:
  - Added interaction defer + followup send in `/serverhealth` to prevent timeout windows while diagnostics are computed.

- Newly Added:
  - Added Dyno Card V2 layout with clearer sections and stronger readability.
  - Added raid status badge, action queue, community pulse, and top XP spotlight.

- Improvements:
  - Reworked health card structure to be easier to scan in admin workflows.
  - Improved wording consistency across server health sections.
