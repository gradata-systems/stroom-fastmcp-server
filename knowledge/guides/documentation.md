# Pipeline documentation

Every pipeline built, changed or evaluated gets a Documentation doc with the pipeline's name, written with
`write_documentation` in Markdown. The same content is returned in the chat. On an update, revise the sections
that changed; the tool keeps the change log and adds a line to it.

## Sections

| Section | Events pipeline | Indexing pipeline |
| --- | --- | --- |
| Purpose and data | Feeds, source system, record format, volumes | Source feed, destination index, Elastic Cluster |
| Processing | Element chain, inherited template, reference lookups and decoration | Element chain, inherited template, enrichments |
| Field mapping | The tables below | Event-logging path or source field to index field and mapped type |
| Output | Event types (`EventDetail`, `TypeId`, `Action`) with counts | Index template name and version; verification searches and results |
| Conformance | Schema validation and quality pass rates; recent error groups | Error stream triage summary |
| Open items | Suggestions and known limitations; input fields not mapped | Suggestions and known limitations |

## Field mapping (events pipelines)

`build_translation_xslt` returns this section as `field_mapping` when called with the pipeline (`pipeline_uuid`)
and its sample streams (`stream_ids`): it steps the XSLT it generates over them, nothing saved, so the tables
agree with the XSLT and hold the values the events actually get. Without them it returns no table, only what it
needs. Use the result as it is, under `## Field mapping`; regenerate it when the mapping changes rather than
editing the tables by hand. Computed values name fields, not selectors: `normalize-space(username)`. It has two
tables:

1. **EventSource**: one row per element of `EventSource` (and `EventTime`), with columns XPath, Description (the
   event-logging schema's own description of the element) and Value: the values the sample's events got, up to
   three, then how many more. An element only some kinds of event have names those kinds; one no sampled event
   got shows "(not in the sample)".
2. **Event types**: one row per rule and TypeId written for the sample, with columns Source (the rule and the
   records it covers), TypeId, Description (the values seen, up to three), and EventDetail: every other element
   below `EventDetail` of one event with that TypeId, a line each as `XPath="value"`. A rule nothing in the
   sample reached shows "(not in the sample)", with its elements from the mapping (`XPath="{input}"`): widen the
   sample, or say so under Open items.

A rule the sample didn't reach is described from the mapping instead: a constant in quotes, an input in braces
(`XPath="{username}"`), with any value map (`A → B`) or default.
