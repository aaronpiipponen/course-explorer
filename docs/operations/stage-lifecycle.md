# Stage lifecycle

Every stage exposes the same five actions, and this page describes the durable states, boundaries, and failures behind them.

## The five actions

One algorithm serves all four stages, so ingestion, embedding, translation, and inference accept the same five actions with the same meaning.

| Action | What it does |
|---|---|
| start | Continues a session the stage retained, or begins a fresh one, takes the stage's reservations, then launches one detached worker. |
| pause | Publishes a pause request, waits for the owned worker to exit, and unloads the resources the session started. A pause releases no reservation. |
| resume | Re-arms the session the stage retained, takes its reservations again, and launches its worker again. |
| status | Reads the recorded state and the owned process and reports the lifecycle state. It writes nothing, starts no service and takes no reservation. |
| stop | Publishes a stop request, waits for the owned worker to exit, finalizes the completed work through its owning writer, releases the resources, releases the reservations, and returns the stage to idle. |

The commands behind the actions are listed in the [Operator menu](/docs/operations/operator-menu.md#the-stage-tools).

A start or resume that another owner refuses names that owner and writes no stage state. [Lifecycle core](/docs/operations/lifecycle-core.md#locks-intent-and-checkpoints) states the three reservations and what each covers.

## The durable states

One record holds the current state of a stage, and one immutable receipt per session holds the identity and the options of that session. The receipt is written when the session is created and never changes.

| State | Meaning |
|---|---|
| idle | The stage holds a session and its progress, and its status carries no session identity. |
| starting | A start reserved the stage and recorded the invoking command as the owner. |
| running | The detached worker reports that it is working. |
| paused | A pause took effect at a durable boundary, and the session is retained for a resume. |
| stopped | A stop took effect at a durable boundary, and the stop action then finalizes the session. |
| completed | The work of the session finished. |
| failed | The run ended with an error, and the stage holds the content-free failure record. |

A status read adds one projected state, interrupted, in place of starting or running when the recorded owned process is not alive. No command writes it, and the projection writes nothing. A start resumes the retained session when its work identity matches this device. Ingestion and embedding retain their work after a stop. A start on an idle session of theirs whose work is incomplete and whose identity differs selects the fresh session's options and takes its reservations before it moves that session's receipt and state to `superseded-<session_id>` under the stage controller directory, and the report names the superseded identity. A refused reservation leaves the retained receipt and state in place. Translation and inference finalize their work at a stop, so a start on their idle state creates a fresh session. A paused, interrupted, stopped or failed session whose identity differs refuses, and it leaves the retained work in place.

![One stage moves from idle through starting to running as its worker reports, and the run then ends at a durable boundary as completed, failed, paused, or stopped. A status read projects the interrupted state while a starting or running stage has no live owned process, a resume returns a paused stage to starting, and a stop action from any non-idle state finalizes the session and publishes idle.](/docs/diagrams/stage-lifecycle.svg)

## Starting a session

A start on a stage with no state creates a fresh session. The stage chooses its options, records a work identity from those options and its validated configuration, records the revision of the running checkout, and publishes starting. An ingestion start adopts the one partial capture the stage retained, and it fails when more than one candidate exists.

A start on a stage with a state continues that session when the retained identity matches this device. A retained ingestion session also re-reads its capture manifest, requires a regular file, and compares its bytes with the recorded hash when one is recorded. Which of the two happens follows from the retained state.

| The retained state | What a start does |
|---|---|
| A live owned worker | Reports the current status and launches nothing |
| completed | Reports the current status |
| idle after the work finished | Creates a fresh session |
| idle on a stage that finalizes its work at a stop | Creates a fresh session |
| idle with pending work whose identity does not match this device | Takes the fresh session's reservations, moves the receipt and state to `superseded-<session_id>`, and creates a fresh session |
| Anything else that matches this device | Re-arms the retained session |
| A paused, interrupted, stopped or failed session that does not match this device | Refuses, and leaves the retained state in place |

A resume follows the same compatibility rule as a start and is refused with a safe error when the stage retained no session. A resume with a mismatched identity refuses with `retained lifecycle work configuration does not match this device; stop the session and use start`. A retained capture whose manifest records a different period selection from the periods the resumed run computes refuses to resume with `selected curriculum periods changed on resume`. Remove that retained run directory before starting a new capture.

## Pause and stop

A pause publishes a pause request and waits for the owned worker to exit, and the run then ends at its next durable boundary. The stage runs its release hook, which for a remote stage hands the unloaded pause to the worker session and releases the owned worker resources, and the command then releases the services the session started. The session, its receipt, its progress, and its stored work are all retained. A pause never overwrites a pending stop. A pause releases no reservation of the session, because the retained session resumes under the same owner.

A stop publishes a stop request, waits for the owned worker to exit, and then finalizes the session. Finalization writes the completed work through the owning writer, releases the services the session started, deletes the local runner logs of the session, releases the session's reservations, and publishes idle. In the stop that finalizes a session, a release failure records the content-free stop failure at the local cleanup step and leaves the state non-idle. An idle state releases its reservations without recording a stop failure. The stop records whether the work had finished, so an idle state after a completed run holds the work as complete and an idle state after a partial run holds pending work. Either way, the stage holds the session receipt and the progress it recorded.

Ownership decides what a pause or a stop releases. The state records only the resources this session created or started, so the release never touches a service the session did not start. A local stage requests the database service, and the embedding stage also requests the embedding service, so only the embedding service is a session-owned temporary service that the stage stops and deletes.

> [!WARNING]
> An ingestion stop that reaches idle deletes every raw capture run under `RAW_DATA_ROOT` in `config/curriculum.toml` except the one the stage retains for an incomplete capture. After a completed capture, no raw capture run is retained. A failed stop deletes nothing.

> [!IMPORTANT]
> A stop with partial coverage is a valid end of a run. The stage returns to idle with the pending rows marked, and the next start takes that work up.

A stop that fails at one of its steps records a content-free stop failure and fails loudly. The record names the step that was executing, the exception class, and an available integer status, and the owned resources and the retained evidence are left in place. A following stop retries from the real state and clears the record when it completes.

## The boundaries of a run

Pause and stop act only at the durable boundaries of a stage, so a run ends on a committed unit of work.

| Stage | Where a pause or a stop takes effect |
|---|---|
| Ingestion | After a committed batch of `CAPTURE_CHECKPOINT_RESPONSE_COUNT` responses and before the next source request, between two stored responses during verification, and between a completed capture and normalization |
| Embedding | After the vectors of one batch are stored |
| Translation | Between two translated cells, after the retained output was republished |
| Inference | After a completed unit of work in the processing cycle |

Normalization is one transaction. It reads the completed capture, validates it, and writes the whole batch, and no control request interrupts it. [Ingestion](/docs/backend/catalogue/ingestion.md#pause-stop-and-the-normalization-transaction) describes that transaction.

## Active time and coverage

A stage measures active time only while it is running and its recorded start time is known, so the idle time between two runs does not inflate the measured rate or the projected remaining time. A backward step of the host clock contributes no elapsed time, so the measured active time never decreases.

Coverage counts the stored rows of the role the report names, so it says what has been written and not what the run is doing. The two facts are independent. A stage returns to idle after a completed run with complete coverage, and it also returns to idle after a partial run with pending rows. The [Full-catalogue inference run](/docs/experiments/inference/full-catalogue-inference-run.md) report records a run that stopped part-way and resumed to a complete cycle. The coverage reports are described in the [Operator menu](/docs/operations/operator-menu.md#coverage-and-summaries).

## The worker at a stop

A remote stage finalizes on the worker and returns the finished copy before it releases the resources the session owns and its local scratch, which [Worker sessions](/docs/operations/worker-sessions.md#owned-resources-only) describes.

## The periodic reboot

The worker restarts after a configured amount of model run time while `WORKER_GPU_REBOOT_ENABLED` in
`config/worker.toml` is on, and `WORKER_GPU_REBOOT_INTERVAL_MINUTES` in the same file sets the
interval. [Worker sessions](/docs/operations/worker-sessions.md#the-periodic-reboot) states the
configured values, the clock, the samples, the restart proof and the resumed session.

## Failures and recovery

A failed run publishes the failed state with one content-free record. The record holds the step that was executing, the exception class name, and an available integer status, and nothing else. No message, no input value, and no path from the failure is stored.

| Step | The phase the stage was in |
|---|---|
| session | Reading the retained session receipt |
| intent | Publishing the control request |
| await_exit | Waiting for the owned worker to exit |
| transport | Opening the connection to the worker |
| capture | Taking the input snapshot and creating the worker session |
| bring_up | Preparing the owned resources on the worker |
| launch | Launching the worker run and acknowledging it |
| status | Reading the worker status |
| release | Releasing the owned worker resources |
| reboot | Restarting the worker and proving it came back |
| finalize | Running the finalize hook of the stage |
| worker_finalize | Stopping and finalizing the worker session |
| return | Returning and retaining the worker dataset |
| worker_cleanup | Dropping the returned worker database |
| local_cleanup | Deleting the local scratch of the session |
| services | Releasing the services the session started |
| idle | Publishing the idle state |

A failed local run holds its progress and its ownership. The stop action then finalizes the session to idle with incomplete work, and the next start continues the pending work. A failed remote run holds the ownership record, so a following stop verifies the retained state, returns whatever the worker holds, cleans it, and clears the diagnostics.

> [!NOTE]
> A stage reports its failure in every status read. The failure record and the stop failure record are cleared only by a completed stop, and a pause preserves both.
