# Reference data and dictionaries

Translations often need values the record does not carry: a user's department, a host's site, a vendor code's
meaning. Stroom has two mechanisms, and the mapping (`build_translation_xslt`) writes the XSLT for both.

| Need | Mechanism | In the mapping |
| --- | --- | --- |
| A table that changes, is large, or comes as a feed (user list, asset register, GeoIP) | Reference data: a reference feed, a reference-data pipeline, `stroom:lookup()` | `lookup: {map, field}` |
| A small static table or list kept with the content | A Dictionary doc, read with `stroom:dictionary()` | `dictionary: 'name'` (key=value lines), `in_dictionary` (a list) |
| A value computed from the record | `transform` (lower, upper, trim, strip_domain, domain, digits), `any_of`, `extract`, `xpath` | see the XSLT guide |

## Reference data

How it fits together:

1. **A reference feed** holds the table as raw data (stream type `Raw Reference`): `create_feed` with
   `stream_type='Raw Reference'`, `upload_sample` with `stream_type='Raw Reference'` and an `effective_time`.
   Reference data is versioned by time: a lookup uses the version in effect at the event stream's time, so a
   table uploaded after the sample streams is invisible to them. Give `effective_time` before the events
   (`2000-01-01T00:00:00.000Z` for a table that always applied); real feeds carry it in their receipt headers.
2. **A reference-data pipeline**, a child of the `Reference Data` template (`find_pipeline_templates
   stage=reference`), parses it like any feed (a Data Splitter on `combinedParser.textConverter` for text) and its
   XSLT writes `reference-data:2`: one `<reference>` per record and map, with the map name, the key and the
   value. `build_reference_xslt` writes that XSLT from a mapping:

   ```json
   {"input": "data_splitter",
    "maps": [{"name": "USER_TO_DEPARTMENT", "key": "user",
              "values": [{"element": "department", "field": "dept"}, {"element": "site", "field": "site"}]},
             {"name": "USER_TO_NAME", "key_xpath": "lower-case(data[@name='user']/@value)", "values": [{"field": "name"}]}]}
   ```

   Process the reference stream (`create_processor_filter`, `wait_for_processing output_type='Reference'`): the
   pipeline writes `Reference` streams.
3. **The events pipeline names the feed** as a pipeline reference on its translation step, loaded by the
   standard `Reference Loader` pipeline: `create_pipeline references=[{"feed": "ACME-USERS", "loader_pipeline":
   "Reference Loader"}]`, or `set_pipeline_references` on a pipeline that exists. Without this the lookups find
   nothing and stepping shows a lookup warning on every record.
4. **The translation looks keys up**: `{"path": "EventSource/User/UserDetails/Organisation", "lookup": {"map":
   "USER_TO_DEPARTMENT", "field": "user", "path": "department"}}`. `path` names an element inside the value
   (omit it for a text value). A key the map lacks gives no value, so the element is left out, or `default` is
   written. Keys are compared as written: normalise them the same way on both sides (`transform: lower` or
   `strip_domain` on the events side, `key_xpath` with `lower-case()` on the reference side).

`find_reference_data` lists what the environment already loads: each map, its key and value shape, the feeds
and pipeline that load it, the loader to name, and which pipelines use it. Reuse those before creating new
reference data; `list_template_children` shows how sibling pipelines attach them.

## Dictionaries

A Dictionary doc is plain text, one entry per line. `create_dictionary` makes one in the build (and
`update_dictionary` changes it); the generated XSLT reads it at run time, so later edits apply without
regenerating:

- `key=value` lines and `{"path": "...", "field": "code", "dictionary": "Vendor codes"}` give the value for a
  record's key (`default` when the key is missing).
- Plain lines and a rule condition `{"field": "user", "in_dictionary": "VIP users"}` test membership.

Keys and values are trimmed; comparison is exact and case-sensitive, so `transform: lower` the key when the
dictionary is lower case.
