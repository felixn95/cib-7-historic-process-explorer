# What the data holds

What the history of a CIB seven / Camunda 7 engine gives you -- and what it does not. The tool
checks at connect time and writes "not recorded", with a reason, everywhere it matters, rather
than showing a 0. This document explains where those limits come from.

## The decisive setting: history level

It lives in `act_ge_property` under `historyLevel` and decides which tables the engine writes at
all:

| Level | Value | What is additionally written |
|---|---|---|
| NONE | 0 | nothing |
| ACTIVITY | 1 | `act_hi_procinst`, `act_hi_actinst`, `act_hi_taskinst`, `act_hi_varinst` |
| AUDIT | 2 | the same, plus details of task assignments |
| FULL | 3 | additionally `act_hi_detail`, `act_hi_incident`, `act_hi_identitylink`, `act_hi_op_log`, `act_hi_job_log`, `act_hi_ext_task_log`, `act_hi_decinst`, comments, attachments |

**At AUDIT those tables are empty**, and that has immediate consequences:

| What you would expect | Why it is missing |
|---|---|
| Share of instances with incidents, incidents per definition | `act_hi_incident` is empty. All that exists is `act_ru_incident` = **open** incidents of running instances, no history |
| "Variable set once or overwritten many times", the history of a value | `act_hi_detail` is empty. `act_hi_varinst` holds only the latest state per variable instance |
| The claim timestamp of a user task, and with it any split between waiting time and working time | `act_hi_op_log` and `act_hi_identitylink` are empty. From `act_hi_taskinst` there is only creation, end, assignee, duration |
| Manual interventions, the reason an instance was terminated from outside | `act_hi_op_log` is empty -- and `delete_reason_` is typically NULL for externally terminated instances |
| DMN decisions | `act_hi_decinst` is empty |

The tool makes no assumptions about any of this: `detect.py` checks per capability whether the
table exists and holds rows, and the views state the reason. Against a FULL database the same
queries light up on their own.

## Further traps the tool absorbs

**`start_user_id_` can be empty throughout.** Who triggered an instance is then recorded nowhere.
The start trigger is therefore derived from what does exist: the parent process
(`super_process_instance_id_`), the start activity (`start_act_id_` joined with
`act_hi_actinst.act_type_`, i.e. `signalStartEvent`, `messageStartEvent`, `timerStartEvent`), a
restart (`restarted_proc_inst_id_`). When nothing remains, the answer is explicitly "not derivable
from the data" rather than "API".

One detail when resolving the start activity: join on the instance's `start_act_id_`, **not** on
the activity type. Embedded subprocesses have start events of their own, and a type filter counts
those too.

**Timestamps are naive.** Every time column is `timestamp without time zone`; the engine writes
them in the zone of its JVM, and that zone is not stored in the database. It belongs in the
connection profile (`source_timezone`), and the interface states the zone it used everywhere. It
can be evidenced from the distance between server time and the most recent history timestamp, and
from the hour-of-day distribution of instance starts.

**Equal timestamps mean an undetermined order.** `act_hi_actinst` keeps a `sequence_counter_`
which decides; `act_hi_procinst` does not. Two instances with an identical start time are
therefore marked "order undetermined" instead of appearing arbitrarily sorted.

**History cleanup moves the edge.** `removal_time_` = end time plus the history TTL. Once it lies
in the past, the instance may be deleted at any moment. An empty stretch in a timeline can
therefore mean "no longer present" rather than "nothing happened" -- and the interface says so
next to the number, not in the small print.

**The business key is not inherited automatically.** Instances started through call activities
carry it only when the model says so, and some carry a *different* key -- a quotation process
calling an order process under the order number. Both belong to the case and stay separately
marked. See
`docs/DESIGN-DECISIONS.md`.

**Variables can occur several times per process instance.** Multi-instance loops and subprocesses
create several variable instances of the same name. That is **not** overwriting (which would live
in `act_hi_detail`) and must not be counted as such. Be especially careful with marker variables:
the same variable can be the invocation parameter at instance start and be set again later by a
service task with a different meaning. Counting "any occurrence is true" produces numbers several
times too high; what counts is the value at instance start (`act_inst_id_ = proc_inst_id_`).

**The technical type of the same variable changes.** A marker may be written as a `boolean`
(`long_` 0/1) by some processes and as a `string` ("true"/"false") by others. Filtering on one
form loses hits. The variable catalogue reports such type switches explicitly -- they are also a
signal for name collisions.

**Serialised Java objects are not resolvable without the application.** `var_type_ =
'serializable'` with the class name in `text2_`. JDK classes (`java.time.*`, `java.util.*`) are
harmless, application classes are not. The catalogue marks what would be resolvable; the instance
view falls back to the raw bytes, explicitly labelled as such.

**Process definitions can have hundreds of versions.** Where every build redeploys everything, a
single definition key easily accumulates two or three hundred versions in `act_re_procdef`. The
version axis is then noise rather than domain: the views group by `proc_def_key_` by default and
offer the version as a breakdown. There can also be definitions that are deployed and never ran --
and conversely keys that exist only historically.

**`tenant_id_` is not a dimension.** In single-installation deployments the field is empty or
constant. The tool shows what is in it and does not compute with it.

## Orders of magnitude

From a development database holding roughly 226 000 process instances over eighteen months --
useful for estimating runtimes, not as an expectation of other systems:

| Table | Rows | Size |
|---|---|---|
| `act_hi_varinst` | 6.5 M | 3.8 GB |
| `act_hi_actinst` | 2.0 M | 2.1 GB |
| `act_ge_bytearray` | 1.8 M | 5.1 GB |
| `act_hi_procinst` | 226 442 | 148 MB |
| `act_re_procdef` | 60 808 | 24 MB |

And the resulting runtimes: detection 0.2 s · one case 0.1 s · definition list 4 s · variable
catalogue about 90 s (two minutes cold) · landscape 7 s. Business key search is fast even as a
substring (under 100 ms), because `business_key_` is indexed and the table stays small.
