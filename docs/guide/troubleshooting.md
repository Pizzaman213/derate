# Diagnosing a failure

The failures below have names. Each one prints something specific, and this page
matches the message you are looking at to the thing that has to change. You need
the machine that is misbehaving, a shell on it, and the coordinator's UI open at
`http://<coordinator>:8080`.

Most of what derate prints is written to be read. A refusal names the number
that blew the budget and the change that would work; a launch that dies names
the line the runtime printed on its way out. Where this page quotes a message,
the words are the product's own — match them character for character rather than
by gist, because two of the errors here are near-identical sentences about
completely different machines.

## The container refuses to start

You run the installer, or `docker run` by hand, and the container exits
immediately. `docker logs derate` has this in it:

```
================================================================================
derate refuses to start: this container is on bridge networking.

    Host networking is required. mDNS is multicast and does not cross a
    Docker bridge, so this container would start cleanly, report itself
    healthy, and never discover another node. The node agent also needs to
    see the real interfaces to report the ConnectX-7 topology.

Start it again with --network host:

    docker run --network host -v derate:/data ghcr.io/pizzaman213/derate/node

With compose, the service needs:

    network_mode: host

Interfaces visible in this namespace: <the ones it found>
```

The last line names the interfaces the container can actually see, which is the
evidence it refused on: every one of them was a veth pair, which is what a
bridged container gets and a host-networked one never does.

This is the one check derate makes before it starts, and it is a refusal rather
than a warning on purpose. Host networking is not a preference. mDNS is
multicast and does not cross a Docker bridge, so a bridged node comes up
looking perfectly healthy and never discovers anything — and never gets
discovered. The failure is silent and indistinguishable from "there is no second
node", which is why it is caught at startup instead of found three days later.

**The fix is `--network host`**, exactly as the message says, or `network_mode:
host` under compose. `install.sh` passes it for you; you only see this message
if you wrote the `docker run` yourself or edited a compose file.

If you are working in a sandbox where host networking genuinely is not
available, `DERATE_ALLOW_BRIDGE=1` downgrades the refusal to a warning:

```
[derate] WARNING: bridge networking detected (...). DERATE_ALLOW_BRIDGE is set, continuing anyway. mDNS discovery will not work and this node will not find or be found by any other.
```

That is not a supported configuration and the warning is not hedging. A cluster
of one is all you get.

When the check passes it says so, which is worth knowing so you can tell "it
checked and was happy" from "it never ran":

```
[derate] host networking confirmed (hardware interfaces visible: ...)
```

## A machine joins, reports healthy, and shows no hardware

The node appears in the roster and reports healthy. It reports host memory,
temperature and CPU. And where its hardware should be there is nothing: on the
node's own screen the GPU row reads `unidentified`, `addressable` reads `—`, it
contributes nothing to the cluster's free memory, and the planner never places
anything on it.

The container did not get the GPU. The hardware probe is `nvidia-smi`, and the
NVIDIA container toolkit is what injects that binary into a container — so
without `--gpus all` there is nothing to probe with. derate does not then guess.
It reads `/proc/driver/nvidia` and the PCI vendor ids, which a container can
still see, and if there *is* NVIDIA hardware here that nvidia-smi did not answer
for, it records the machine as unidentified rather than as a machine with no
GPU. Those are different facts and only one of them is a misconfiguration.

Open the node from the cluster screen. Above the hardware rows you will see:

```
device class is not recognized; cannot confirm this hardware is eligible to join the pool
```

The same sentence is under the node's name in the roster, which is drawn with a
dashed border and dimmed when a node is ineligible. In `node.log` on the
machine itself the probe recorded why:

```
node spark-02 probed as UNKNOWN: NVIDIA hardware is present but nvidia-smi did not answer; the driver or the container toolkit is missing
```

and the node's startup logged what that costs:

```
no GPU could be probed on spark-02; this node will be visible but the planner will skip it
```

**The fix is the NVIDIA container toolkit on that machine**, then re-run the
installer. If the installer already tried and fell back, it told you so at the
time — this is what it prints when the driver is present and Docker still could
not hand over the device:

```
[derate] this machine has an NVIDIA driver but Docker could not pass the GPU
[derate] into the container, so the node started without it. It joins and
[derate] reports healthy, and it reports host memory, temperature and CPU --
[derate] but the probe can see the driver and not the GPU, so it records
[derate] unidentified hardware and the planner will not place work on it.
[derate] Install the NVIDIA container toolkit and re-run:
[derate]   https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
```

You do not have to restart the coordinator or re-enrol the node. Re-running the
installer replaces that machine's container, and the roster picks up the new
hardware on its own — a node re-reads what it is running on periodically, not
only at startup.

A machine that genuinely has no GPU is a different row and not a fault. A
Raspberry Pi, a NAS or a spare x86 box probes as a CPU node: a real cluster
member with real host telemetry, which can front a remote provider but will
never be given a rank. Its GPU rows read `—` rather than zeros, and it is not
marked ineligible.

## Memory numbers that look impossible on a DGX Spark

Two symptoms, one cause.

**A Spark that reports zero GPU memory in use while it is obviously busy.** On
GB10 `nvidia-smi` reports `[N/A]` for every aggregate framebuffer field —
`memory.used`, `memory.total`, `memory.free`, `memory.reserved`. There is no
discrete framebuffer to describe. Only per-process accounting still works, and
nvidia-smi only reports processes it can see, so a container in its own PID
namespace sees none of them and attributes the entire unified pool to the
operating system. That is what `--pid=host` is for, and `install.sh` passes it.
If you wrote the `docker run` yourself, add it.

**A Spark that says it has far more memory free than it does.** The static
ceiling is not headroom. On GB10 the model and the operating system spend the
same bytes, so the addressable figure describes what the hardware could ever
hold with nothing else running — and on a machine that is doing anything at all
it overstates what you can actually get, by a lot. The node inspector says this
under the hardware rows:

```
Unified memory: the model and the operating system share one pool, so the static ceiling overstates what is actually allocatable. Allocatable is the figure the fit gate plans against, read live from the node rather than worked out here.
```

The row to read is **allocatable now**, not **addressable**. `addressable` is
the ceiling and shows the guardrail applied to it (`0.90` by default);
`allocatable now` is what the node could hand out at this second, and it is the
number the fit gate plans against. The gap between them is the operating system
and whatever else is resident.

This is also why a launch can die at startup while the fit verdict said it
fitted. The runtime asks for a share of the whole device and refuses to start
unless that share is *free*:

```
Free memory on device cuda:0 (49.56/121.69 GiB) on startup is less than desired GPU memory utilization (0.9, 109.52 GiB)
```

Those numbers are from one real launch; yours will differ, and the shape of the
sentence is what to match on. derate reads that line as a memory failure even
though it never says "out of memory". If you see it, you were reading the
ceiling.

## A refusal you disagree with

Refusals are the product working. The fit gate blocks a launch that would run
out of memory, and it does not stop at "no" — it names the term that blew the
budget and what would change the answer.

The arithmetic is on the model's own screen: `/models/<model id>`, or click
through from **Models**. Pick the runtime and the machines, and the verdict card
shows the whole calculation, per node and per rank.

![The model detail screen: a refusal on one node, and the fit arithmetic it is based on](../screenshots/model-detail.png)

Two verdicts, each with its own lamp, because they are answering different
questions:

- **fits on idle hardware** — measured against `<N> GB ceiling`, what this
  machine could hold with nothing else on it.
- **fits right now** — measured against `<N> GB allocatable`, what it can hand
  out at this second.

Under them is the breakdown bar and the table it comes from: weights, kv cache,
activations, comm buffers, replicated, framework overhead, then `total`,
`ceiling on idle hardware`, `allocatable right now`, and `headroom`. The
limiting term is marked. That is the arithmetic; if you disagree with the
verdict, this is the row to disagree with.

The refusals are shaped so the fix is in the sentence. `Won't fit: weights alone
are ... per rank against ...` means context and concurrency cannot help you at
this quantization — it ends `it needs` and then lists requantizing or spreading
over more machines. `Over budget by ... KV cache is the problem: ... per rank at
... tokens x ... sequences` ends with the context length and the concurrency
that would fit. A context refusal is not about memory at all:

```
Won't fit: <n> tokens of context was requested, but <model> was trained on <w> -- the runtime refuses to start past a model's own max_position_embeddings, however much memory is free. Lower context to at most <w>.
```

And a verdict of **fits, degraded** is not a refusal — the model loads, and the
card tells you what you are agreeing to: `It will load and decode at <n> tok/s.`
Below 10 tok/s the gate says so rather than letting you find out.

**When you can overrule it.** A refusal made against the *live* reading can be
overridden, because the machine may be busy now and idle in ten minutes. When
that is the case the card shows the refusal in full, a checkbox whose label is
the claim you are making, and no button until you tick it:

> Serve anyway. I am overriding the live fit gate, which measured `<n>` GB
> allocatable and refused. The `<m>` GB static ceiling is not reachable right
> now.

Ticking it turns the button into **Serve anyway**. There is a second gate with
the same shape for pooling machines that are not alike, and if a launch needs
both, each gets its own sentence and its own box and the button appears only
when every box is ticked.

**When you cannot.** A refusal made against the static ceiling offers no
checkbox and no button, only the reason. That is a claim that the model does not
fit this hardware at all, and no amount of waiting changes it. Requantize, pick
a smaller model, or add machines.

If there is no live reading at all, the card says which answer you are looking
at rather than quietly using the other one:

```
No live memory reading: <reason>. Only the idle-hardware answer is available.
```

## A launch that clears every gate and dies at load

derate keeps a table of the architectures each runtime can load, and checks a
model against it before anything is committed. A model whose architecture is not
in it is refused up front with a 400:

```
<Architecture> is not in the model registry of <image>:<tag> (<runtime version>), the image this build launches
```

or, when there is no image on the coordinator to be asked:

```
<Architecture> is not in vllm's supported architecture list
```

Either sentence can end with somewhere else to go — `; the tts runtime loads it,
on /v1/audio/speech` — when another runtime here does load that architecture.
Change the runtime rather than the model.

The two openings are different strengths of claim and it matters which one you
got. The first was read out of the image's own model registry: that is what the
runtime you are about to launch actually holds, and its version is named so you
can check it. The second is derate's hand-copied table, used when there is no
image locally to interrogate, and a hand-kept table goes stale in the direction
nobody notices: it keeps refusing models that started working.

**If you got the second sentence and you believe the model is supported**, pull
the runtime image onto the machine running the coordinator, then restart the
coordinator — the image is read once, in the background, at startup. derate
never pulls it for this: the image is around 24 GB and a page load is the wrong
place to start that download. Once it is present, the static table is replaced
by the registry read out of the image itself, cached by image id so a moved tag
re-reads and a stale answer cannot survive.

Being in the registry means the class exists, and that is not the same claim as
"a forward pass completes". Where a launch has actually been watched to die,
the architecture is recorded with what it did, and the refusal says so plainly:

```
vllm lists <Architecture> in its registry but it does not actually run: <what happened>
```

Those entries name the image version they were observed on. A newer image may
have fixed the bug.

## `manifest unknown`, and a TTS launch that never starts

A TTS deployment fails early, and the error recorded against it is a registry
error rather than anything about memory or architecture: `manifest unknown`,
`pull access denied`, `failed to ensure local image`.

The image is not published. The `tts` runtime is derate's own speech server, and
its image defaults to `ghcr.io/pizzaman213/derate/tts:latest` — a tag that has
to exist somewhere the node can pull it, and does not yet. Every gate passes,
because none of them is asking whether the registry has the image; the failure
happens on the node, at the pull.

Two ways through, and the tag is the one that survives a restart.

**Build it on each machine that will serve speech, and tag it with the name the
launch will ask for.** It is the vLLM image this project already pins plus a
handful of pip layers, so on a node that has served anything only those layers
are new bytes. You need a checkout of the repository on that machine — the build
copies derate's own speech server into the image — and you run it from the root
of that checkout:

```bash
docker buildx build -f docker/tts.Dockerfile --platform linux/arm64 --load \
    -t ghcr.io/pizzaman213/derate/tts:latest .
```

arm64 is the platform that matters — a DGX Spark is a GB10. `--load` puts the
result in that machine's own image store; swap it for `--push` if you can
publish to the registry, which is the actual fix and makes the rest of this
section unnecessary. A local tag with that name is what the launch resolves, and
nothing pulls over it.

**Or point derate at a tag of your own** with `DERATE_TTS_IMAGE`, set in the
environment of the container running the coordinator. That variable is read when
the launch recipe is built, so it needs a restart to take effect — and it is one
`docker rm` away from being lost, which is how a cluster goes straight back to
`manifest unknown` without anybody changing anything. Prefer the tag.

The same failure shape reaches the vLLM runtime if its default image is not on
your nodes either. `DERATE_VLLM_IMAGE` overrides it the same way, and pointing
it at the upstream image is supported — you lose audio *decoding*, so a Whisper
deployment launched from upstream reaches READY and then refuses every upload
with `Invalid or unsupported audio file.`, but text deployments cannot tell the
two images apart.

## A deployment that says running long after it stopped answering

The launcher says the workload is running. The port never answers. The
deployment sits in `launching` for as long as the readiness timeout allows.

A solo launch execs the serve command inside a container that sleeps forever.
So when the engine exits, the container does not: `sparkrun cluster check-job`
still reports the workload as running, correctly, because the container it is
looking at is up. Nothing about the container's state distinguishes a serving
deployment from a dead one.

What catches it is the runtime's own words. derate follows the backend's log and
treats four literal lines as the end of a launch rather than as a step in it:

```
EngineCore failed to start.
EngineCore encountered a fatal error.
Engine core initialization failed.
File "/usr/local/bin/vllm"
```

and one more for derate's own speech server:

```
fatal: the speech server failed to start.
```

When one appears, the deployment fails immediately with the runtime's line
carried through:

```
the backend died during startup: <the runtime's own line>
```

**Read that line.** It is the whole diagnosis, and it is a different sentence
every time: a config the runtime rejected, a memory refusal, a checkpoint whose
remote code does not do what the server calls. "The backend did not answer
within 1800s" would have told you nothing.

Open the deployment from the dashboard — the URL gains `?dep=`. The sheet has
the launch stepper on the left (Preparing the machine, Downloading weights,
Loading onto the GPU, Starting the engine, Serving) with the launcher's current
line verbatim beside it, the recorded error under the header, and the backend's
log in a column on the right. That log column is the only place this output
appears: a solo launch's output goes to `/tmp/sparkrun_serve.log` **inside the
container**, not to `docker logs`, and the way to read it is `sparkrun logs`,
which follows rather than returns.

The stepper's progress figures come from the programs doing the work — the
download counts its files, the checkpoint loader counts its shards. Nothing else
reports a total, so the compile and the CUDA graph capture get a caption and no
bar. That is not the screen failing to measure them: neither step reports one,
and a bar filling at a rate the UI made up would be read as an estimate and
planned around. On a machine that already has the image and the weights, graph
capture is the longest of the four steps and the one with no bar.

## Errors from the endpoint itself

These come from the gateway rather than from a backend. An error a backend
produced is passed through untouched, so a `401` on a `/v1` call is a provider
rejecting your key for *that* provider — derate has no inbound authentication of
its own to fail.

Every body below is OpenAI-shaped: `error.message`, `error.type`, `error.code`.
The `code` is the field to match on.

**404 `model_not_found`** — the name is not one this cluster serves. The message
lists what does exist so you can correct the call:

```
The model '<name>' does not exist. Available models: '<a>', '<b>'.
```

**400 `wrong_modality`** — the model exists and is serving, on a different
route. The message names the endpoint that would have worked:

```
The model '<name>' is a speech model and cannot serve /v1/chat/completions, which requires a text model. Send it to /v1/audio/speech instead.
```

`GET /v1/models` reports a `modality` per model, which is where that came from.

**503 `model_not_ready`** — the deployment exists and is not up yet. Comes with
`Retry-After: 5` and the state it is actually in:

```
The model '<name>' is not ready to serve. Current state: launching.
```

Retry. If it never leaves `launching`, read the previous section.

**503 `no_target_admitting`** — every target for this model is unhealthy,
draining, or under critical memory pressure. Nothing is wrong with your request:

```
No target for '<name>' is currently admitting requests.
```

Look at the nodes, not the call.

**502 `upstream_unreachable`** — no target could be reached at all. Nothing was
learned from any backend because none of them answered:

```
No upstream for '<name>' is reachable: <detail> after trying <n> targets.
```

This one *is* about the backends. Check that the deployment is up and that the
provider you configured is reachable from the coordinator.

**503 `upstream_pool_exhausted`** — this is the one that reads like the previous
one and means the opposite. The gateway ran out of its own outbound connections
and never got as far as dialling anything:

```
The gateway has no free upstream connection for '<name>' (tried <n> targets). Every connection is held by a request already in flight. Retry shortly; the backends themselves are not implicated.
```

The backends are fine. The last sentence is there because a leak in the
connection pool once made every model report its backend unreachable while every
backend was answering in under a millisecond, and the wrong machine got
investigated. Retryable and short — a slot frees when an in-flight request
finishes.

## sparkrun is not installed

Discovery, planning, the fit gate and the UI all work. Launching anything is
refused:

```
sparkrun is not installed. The control plane launches every backend through sparkrun and will not launch vLLM or SGLang by hand.
Install it with:  uv tool install sparkrun
Then re-run this launch. If sparkrun is installed somewhere unusual, point DERATE_SPARKRUN_BIN at it.
```

The container says so at startup too, before you get as far as trying:

```
[derate] WARNING: sparkrun is not on PATH. Discovery, planning and
[derate] the UI still work; launching a backend will be refused.
```

## Where the logs are

Two files in one folder, written by every derate process:

- **`node.log`** — every record the process logs, at `DERATE_LOG_LEVEL` (INFO by
  default).
- **`proxy.log`** — the same stream narrowed to the path a request takes out to
  an upstream and back: routing, admission, forwarding, the circuit breaker, and
  every provider module.

**`proxy.log` is not a grep of `node.log`.** It is filtered where the records
are written rather than after the fact, which matters because a traceback's
continuation lines do not carry a logger name. Grepping the wide file for the
proxy gives you the first line of an upstream failure and drops the exception
underneath it. The narrow file keeps whole records.

The folder is next to the code, which in the published container means
`/opt/derate/logs` — a layer of the image, **not** the `derate:/data` volume. Logs
there do not survive `docker rm`. If you want them to, set
`DERATE_LOG_DIR=/data/logs` on the container and they land in the volume
instead. `DERATE_LOG_DIR` is obeyed unconditionally, so `/var/log/derate` works
equally well. Both files rotate at 32 MiB with three backups kept, so the whole
folder is bounded at 256 MiB and cannot crowd out the model weights on the same
volume.

One thing the log files will never contain is an API key. The scrubber sits on
the file handler itself rather than on any one logger, so it catches records
from every part of the process, including the ones written by libraries.

`DERATE_LOG_FILES=0` turns file logging off entirely, for a container whose
stderr is already being collected by the host. An unwritable folder is a warning
and no files — it never fails a startup, so if the files are not there, look for
that warning on stderr.

You do not need a shell for the common case. Open a node from the cluster screen
and pick a window other than **live**: the **what happened here** section shows
that machine's recent events and log lines, warnings and worse by default. What
it draws is the recorded journal rather than the files, and the coordinator's own
1 Hz polling of each node is left out of it — those access lines were 99.6% of
the stream, and with them in, "everything" could only ever show forty of them.
It is deliberately a diagnostic strip and not a log browser — there is no search
box — so for anything wider, read the files, where the access lines are still
there.

---

**Next:** back to the [project README](../../README.md) for the install path and
what the rest of the product does.
