# Errors and triage

Errors from stepping, and from the Error streams of pipelines that run on Events (such as indexing), are classed by
rules rather than by the model. The class comes from the severity, the element that raised the error (the build's
own, or one inherited from a template) and the message. The model explains and acts on the result.

| Class | Typical markers | What happens |
| --- | --- | --- |
| Blocking | ERROR or FATAL from the text converter, parser or the build's XSLT; any schema validation failure; Elasticsearch mapping conflicts or rejected documents; indexing output missing `StreamId`, `EventId` or `@timestamp` | The agent fixes it and steps or reprocesses. The stage can't pass |
| Review | An ERROR from an inherited element; a WARN on every record; a decoration lookup that fails for every record because of a field the build's XSLT sets | The agent checks whether its translation causes it, and fixes it or asks the user |
| Benign | INFO or WARN from inherited elements on some records, such as a failed user lookup for an unknown account | Reported with counts and examples. No change |

## Own errors first

Errors in the build's own content (mapping, XSLT, text converter) are fixed by the agent before anything reaches the
user. Only what it can't resolve is raised: an inherited element, reference data, or the source data itself.

## Accepting an error as benign

When the user says an error is harmless, it is recorded in the pipeline's Documentation doc, in the Errors section,
with their reason. They confirm it in a form. From then on, triage reports that kind of error as benign for that
pipeline, so later reviews don't raise it again.

## Known-harmless messages for your environment

`error_rules.yaml` (`STROOM_MCP_ERROR_RULES_FILE`) holds the classing rules: a message pattern, the element, the
severity and the class. Add rules for messages that are known to be harmless in your environment, such as warnings
from a shared decoration step.
