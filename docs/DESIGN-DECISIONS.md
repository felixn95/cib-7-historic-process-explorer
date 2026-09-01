# Design decisions

Why the tool is built the way it is. Every decision with the measurement or the finding behind
it, so that it can be judged later -- and reversed where necessary -- without repeating the
measurement.

Figures quoted here were measured against real Camunda 7 histories in the low millions of rows.
They are orders of magnitude, not promises.

---

## Data access

### The case closure runs over `root_proc_inst_id_`, not recursively

A case comprises the instances carrying the business key **and** the complete parent/child chain
around them. Two routes lead there:

| Route | Result for one sample case | Time |
|---|---|---|
| recursive CTE over `super_process_instance_id_`, bidirectional | 28 instances | 493 ms |
| two index lookups over `root_proc_inst_id_` | 28 instances | **5 ms** |

Across several hundred sampled business keys both routes returned **the same instance set**, with
no deviations. That works because the engine keeps `root_proc_inst_id_` populated consistently: no
NULLs, no root instance with a diverging value, no unresolvable parent references.

The view takes the fast route. The recursive variant stays as a counter-check in an integration
test (`verify_closure_equivalence`) -- on a database with a patchy `root_proc_inst_id_` (an older
engine, a migrated history) that test fails, instead of the view quietly showing half a timeline.

### Foreign business keys are counted, not merged in

Instances started through call activities inherit the business key only when the model says so.
Measured on one real installation: the large majority of child instances inherit the key, a few
per cent carry none at all, and a few per cent carry a *different* one -- a quotation process
starting an order process, for instance.

For roughly 40 % of all business keys the closure pulls in instances that do not carry the key.
That is the normal case, not the exception. So the origin stays visibly separated per instance:
carries the key / no key of its own / different key.

A different key is, however, **not** expanded transitively. A case can pull in a foreign key with
a dozen instances while that key carries twice as many in total -- the rest belong to another
chain. And keys that identify a business partner rather than a transaction can carry tens of
thousands of instances; expanding those transitively would run away entirely. So the view shows a
count and a link to jump.

### Correlation variables ship empty

The case view has a second track: instances carrying the *same value of an identifying variable*
as this case, without belonging to it through the business key or the parent chain. It answers a
question the first track cannot -- "what else touched this object?" -- and it is the one feature
the tool cannot configure for you.

Which variable identifies a business object is a modelling decision of whoever built the
processes. An earlier version shipped a list of names taken from one installation, and that was
wrong in both directions: on any other installation it finds nothing, and where a name happens to
exist with a different meaning it correlates on something that is not the same object at all --
producing a number that looks like an answer. There is no default that is right for more than one
installation.

So the list is empty until it is configured, and the view says exactly that rather than showing an
empty result, because "nothing configured" and "nothing found" are different statements. The
variable catalogue is the place to look the names up: it shows which variables exist per
definition and how widespread they are, which is precisely what identifies a candidate.

This track also reads variable *values*, so it is subject to the value policy like everything
else -- on a profile without value access it stays closed and says so.

### The case level is computed in Python, not in SQL

Co-occurrence, transitions and sequences need combinatorics per business key. The source queries
are narrow and fast:

| Query | Time | Rows |
|---|---|---|
| root instances (key, definition, time) | 327 ms | 93 372 |
| parent relationships | 124 ms | 226 442 |
| monthly aggregate per definition | 157 ms | 1 652 |
| call graph (definition pairs) | 357 ms | 197 |

About one second in total. The same analysis in SQL would be a construction of window functions
and self-joins -- harder to read, and no longer controllable while it runs on somebody's
production database.

### Precompute rather than compute live -- with a visible timestamp

The variable catalogue scans `act_hi_varinst` several times; on a multi-gigabyte table that comes
to roughly two minutes cold. The landscape takes seconds. Both run as background jobs with
progress, the result lands in a local SQLite file, and the **age is visible in the view** together
with a rebuild button.

The cache is local rather than inside the target database, because that database is only ever
read. The file name contains the installation id and a schema fingerprint, so a cache can never
belong to the wrong database.

### Time limits: 30 s in a request, 300 s in a background job

The point of the statement timeout is to protect a production database from a careless
interactive query. A background build with a progress indicator is a different case -- it may take
longer, but not without bound: a query that needs five minutes should be noticed.

### The connect timeout is configurable

It used to be hard-coded at 10/15/20 seconds. That is defensible in production and absurd in a
test suite, where an unreachable target should give up immediately -- hard-coding it once cost a
test run two and a half minutes of pure waiting. `CIB7_CONNECT_TIMEOUT` now decides, defaulting
to 10 seconds.

---

## Presentation

### The unit of the timeline is the call tree, not the instance

A large case easily holds a hundred instances in a few dozen call trees across a couple of dozen
process definitions. A few dozen lanes are readable, a hundred are not. So one lane per root
instance, collapsed; expanded, the inner hierarchy.

### Gaps are computed against a running high-water mark

A tree's start is compared with the **latest end time of all previous** trees, not with the
immediately preceding tree in the list. Otherwise a short tree running inside a long one reports a
gap that does not exist. While something is still running there is no period without a process --
and therefore no gap.

A negative difference is not a gap but parallelism, and it is reported as such: on a real history
about a fifth of consecutive pairs overlap.

### No episode splitting for large keys

Some business keys carry thousands of instances over months -- typically on test and development
systems where the same partners get reused. Splitting along a gap threshold would make the view
manageable while inventing a case boundary that is not in the data. Instead: a density band across
the lifetime (only above 50 root instances), zoom windows, and the complete gap list.

### Transitions are a frequency count, not a process model

No model exists at case level -- that is precisely what is being explored. The count therefore
appears as a table rather than a flow chart, with no start and end nodes and no process notation.
Rare transitions are not filtered away; the view's threshold defaults to showing everything.

### The BPMN is drawn here, at 1:1

The BPMN file of every deployed definition version sits in `act_ge_bytearray`, including diagram
coordinates. That removes the need for a third-party library and a megabyte of JavaScript, and the
page stays readable with scripting disabled. Three things are not negotiable:

- **Read external labels.** Events and gateways carry their name in a separate `bpmndi:BPMNLabel`
  with its own coordinates. Ignore that and you draw a diagram in which no event is named -- in one
  sample model, 18 of 29 labels.
- **Draw 1:1, do not fit.** A model can be a couple of thousand units wide; scaled into a
  1 000-pixel column its labels end up five pixels tall and the picture is worthless. The frame
  scrolls; shrinking is an explicit toggle.
- **Edges stay unmarked.** The engine does not record sequence flows in its history. Two visited
  nodes do not mean the edge between them was taken.

### Values: a short preview in the table, the full value on click

A few thousand characters of JSON in a table cell are useless, and with dozens of variables the
page becomes unreadable. The table therefore carries a 200-character preview and fetches the full
value when it is opened -- which keeps the page small and sends nothing to the browser that nobody
looks at. The value is the **second** column: as the sixth it hid behind the table's horizontal
scrollbar in a narrow window and was invisible.

The route to a value is a real link: with JavaScript it opens a dialog (raw/formatted, copy),
without JavaScript a page of its own. What gets copied is always the whole value, never the
truncated preview.

### Every number in the landscape is clickable

The drill-down recomputes the case level for exactly the cell that was asked about -- about a
second -- instead of keeping the full cell-to-business-key mapping in the cache. An integration
test compares the aggregate against the drill-down: a number that says something different when
you open it would be worse than a number you cannot open.

### No `--root-path` on the server, only `CIB7_BASE_PATH`

Both mechanisms exist to serve the interface under a path prefix, and using both at once is wrong
whenever the proxy **strips** the prefix (nginx `proxy_pass` to `/`, Traefik's strip-prefix
middleware). Starlette then expects the path to still carry the prefix, the static mount answers
404 under `/static/...` -- an interface without CSS -- and any path comparison in middleware stops
matching. That is how a health check once ended up redirected to the login page.

The prefix belongs where the links are generated, and only there.

---

## What is deliberately not built

- **SSH access.** The abstraction is in place -- after `connect()` nothing knows where the
  connection came from -- but the profile kind and the tunnel are missing.
- **No incremental resume for a restore.** An aborted `pg_restore` leaves a half-filled volume,
  and there is no clean restart point for it. The way forward is "discard the volume and start
  over", and that is what the error message says instead of pretending resumability.
- **No force-directed layout in the call graph.** It would need JavaScript, and the arrangement
  would suggest a proximity that is not in the data. The circle orders by call volume only.
