# Builds, review and promotion

## Reviewing a build

A build is a folder under `MCP Workspace`, and everything in it can be opened in Stroom as usual:

- **The pipelines**: step them yourself in Stroom, over the sample streams in the build's feed.
- **The XSLT**: generated to be read, with a template rule per kind of event. Its Documentation tab has the
  version history and, below a note, the mapping it was generated from.
- **The Documentation docs**: the field mapping, with sampled values and per-rule counts, the kinds of event, the
  errors processing found, and any the user accepted as benign, with their reason.
- **The verification dashboard** (`<index>-VERIFY`): opens on the sample's events, with a text pane that steps the
  selected record.
- **The survey doc** (`<FEED> - Survey`), for an existing feed: the kinds of event found, with example records.

Ask the agent for `build_status` to see each step's state and what is still missing before promotion.

## Checks before promotion

`build_status`, and the promotion approval, list what a build's pipelines still lack:

- a clean step of the pipeline's **current** code, recorded in the build record. Changing the XSLT or converter
  after stepping needs stepping again;
- for a new pipeline, a Documentation doc whose field mapping matches the current mapping and XSLT;
- an XSLT that is still what its mapping generates. A hand edit is reported, not undone;
- for indexing: an index template agreed for the current code (Elasticsearch), and the sample indexed and verified.

The user approves promotion with these in view. Promotion waits for one thing only: documentation the build's own
change left out of date, which the agent fixes in one step. A hand edit of your own only warns, so it never holds
up a promotion.

## What promotion does

1. The agent shows the layout in the chat, a tree of where each document will go, with new folders marked:

    ```
    System/Feeds/Events/Acme/
    ├── V1.1/  (new)
    │   ├── ACME-V1.1-Events  (Pipeline)
    │   └── ACME-V1.1-Events  (XSLT)
    └── ACME-V1.0-Events  (Pipeline: updated from its working copy, backed up first)
    ```

    A new version of a production document goes in a version subfolder beside it, named by the version in its own
    name, so old and new are told apart at a glance. The version's new index, documentation and dashboard go in
    with it. Brand-new documents go where the agent proposes, following
    where sibling sources live, for example `System/Feeds/Events/<Source>`. Ask for any folder to change, and the
    agent shows the tree again. A folder that already exists is always used, never made again beside itself (Stroom
    would allow two of the same name in one folder); that goes for the workspace and build folders too.

    When a build promotes documents of more than one role, its role subfolders (Events or Records, Indexing, CEF,
    Reference) are kept under each destination, as the tree shows them, so `…/V1.1/Events/` and `…/V1.1/Indexing/`
    keep the two stages apart. A build of one role (an index documented, a new indexing version) gets none. The agent asks
    whether you want them; say no, and each document goes straight into its folder. A folder you give for one
    particular document is used as you give it.

    An evaluation report is a doc of its own, `<pipeline> - Evaluation`, and goes beside the pipeline it evaluates
    (over an earlier report there, after a backup); the pipeline's documentation is left as it is.

    A build that works from data already in Stroom (an existing feed, or a new version or working copy of a
    production pipeline) keeps any feed it made in the workspace: those are test feeds, holding the Events its
    pipelines made from production streams (`<FEED>-MCP-TEST`, made by the server), or sample files you chose
    instead. Give one a folder of its own if it is really a new production feed (a new feed version, say).
2. The user approves the layout they confirmed, in a short form.
3. New documents are moved into place. Their UUIDs don't change, so references keep working. They lose
   `mcp-managed` and keep `mcp-generated`.
4. Working copies are written back over the production documents they copy. Each production document is backed up
   to `MCP Workspace/backups` first.
5. Each promoted pipeline gets a processor filter on its feed, with the feed task limit
   (`STROOM_MCP_MAX_FEED_FILTER_TASKS`, 2 by default), **created disabled**. It has no minimum stream creation
   time: enabled, it processes everything on the feed, existing data and the streams the pipeline processed in the
   build included (Stroom processes those again, replacing their Events). An indexing pipeline's filter carries
   `before_enabling`: have the cluster admin drop the Elasticsearch index first (`DELETE _data_stream/<index>` or
   `DELETE <index>`, as the agreed template makes it), or delete a Lucene index's shards, so it starts fresh rather
   than holding the build's events twice. When the events pipeline whose Events it indexes is promoted with it,
   `before_enabling` also says to enable it only once that pipeline's filter has processed the build's streams again:
   enabled at the same time, it indexes their earlier Events too, then their replacements. A pipeline that already
   has an enabled filter selecting what arrives on the feed gets no second one (the reply marks it `existing`). For a
   build on an existing feed the filter names that feed, never a test feed. Each pipeline's StreamAppender writes into its input's feed again: the test feed was for the build only.

## After promotion

The agent shows the layout again as promoted, the same tree you approved, with a link to open each destination
folder in Stroom (and each production document updated in place).

- **Enable processing yourself.** Review the pipeline through the link the server gives, then enable the new
  filter. It processes the feed's existing data too: where an earlier version still processes the same feed,
  enabling the new one processes that history again. Disable the earlier one, or limit the new filter in Stroom
  (its minimum creation time), first.
- **Index versions are yours to switch.** Aliases, data views and retiring a previous index version are left to
  you.
- **Later changes go through a working copy.** Promoted documents aren't `mcp-managed`, so the agent changes them
  in a new build and promotes again.
- **Unpromoted builds stay.** A build that was never promoted stays in the workspace with its build record, for a
  person to clear.
