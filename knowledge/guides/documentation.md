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

`build_translation_xslt` returns this section as `field_mapping`, written from the same mapping as the XSLT, so
the two agree. Use it as it is, under `## Field mapping`; regenerate it when the mapping changes rather than
editing the tables by hand. It has two tables:

1. **EventSource**: one row per element of `EventSource` (and `EventTime`), with columns XPath, Description (the
   event-logging schema's own description of the element) and Value. An element only some kinds of event have
   names those kinds.
2. **Event types**: one row per kind of event, with columns Source (the rule and the records it covers), TypeId,
   Description, and EventDetail: for each element below `EventDetail`, its XPath and then its value on the next
   line.

Values read: `field` for an input field, `expression` for a computed one, "text" for a constant, with the time
format, value map (`A → B`) or default after it.
