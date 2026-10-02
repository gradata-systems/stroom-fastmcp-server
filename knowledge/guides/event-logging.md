# Event-logging schema: working guide

Namespace `event-logging:3`. The authority is the XSD installed in Stroom: use `check_events`,
which picks the version from the document's `xsi:schemaLocation` (e.g.
`file://event-logging-v3.5.2.xsd`). Use the version the environment's pipelines already target.

## Shape of one event

```
<Events xmlns="event-logging:3" xsi:schemaLocation="event-logging:3 file://event-logging-vX.Y.Z.xsd" Version="X.Y.Z">
  <Event>
    <EventTime><TimeCreated>2026-09-28T10:00:00.000Z</TimeCreated></EventTime>
    <EventSource>
      <System><Name>...</Name><Environment>...</Environment></System>
      <Generator>...</Generator>
      <Device><HostName>...</HostName><IPAddress>...</IPAddress></Device>
      <!-- optional: Client, Server, User, RunAs, Interactive -->
    </EventSource>
    <EventDetail>
      <TypeId>...</TypeId>               <!-- stable id for this kind of event -->
      <Description>...</Description>
      <!-- exactly one action element, e.g. Authenticate, Authorise, View, Create, Update, Delete,
           Process, Search, Send, Receive, Network, Alert, Import, Export, Install, Uninstall -->
    </EventDetail>
  </Event>
</Events>
```

Element order matters: the XSD is a sequence. A misplaced element fails validation with
"Invalid content was found starting with element X. One of Y is expected", which names the element
that should come next.

## Rules the quality check applies (`check_events`)

- `TimeCreated` is UTC in `yyyy-MM-ddTHH:mm:ss.SSSZ`. `stroom:format-date(value, pattern)` produces it.
- `System/Name`, `System/Environment`, `Generator` and `TypeId` are set.
- `EventSource/Device` identifies the host the event came from.
- `EventDetail` holds exactly one action element.
- No empty elements: leave an element out rather than writing it empty (an empty `TimeCreated` fails
  the schema; an empty `HostName` passes it but carries no information).

## Authenticate, the common case

```
<Authenticate>
  <Action>Logon</Action>                 <!-- Logon, Logoff, ChangePassword, ... -->
  <User><Id>alice</Id></User>
  <Outcome><Success>false</Success><Description>Bad password</Description></Outcome>
</Authenticate>
```

`Outcome` is omitted on success in many environments; include `Success=false` with a
`Description` for failures.
