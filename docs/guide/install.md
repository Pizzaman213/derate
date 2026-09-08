# Install derate on one machine, then on a second

One command puts a container on a machine and that machine becomes a working
single-node cluster. A second command, composed for you by the first machine,
adds another. You need Linux, and Docker — or a shell that can install Docker.

## Before you start: Linux only, today

The installer installs a *container*, and three of the flags it passes are
Linux-host features:

- `--network host` does not reach the LAN under Docker Desktop.
- `--gpus` needs the NVIDIA container toolkit.
- `--pid=host` has no host to name.

So this is not a check that could be relaxed. On anything else the script stops:

```
[derate] derate's container runs on Linux, and this is Darwin.
```

That message goes on to offer a native install line, `pipx install derate`. Do
not run it. That name on PyPI belongs to an unrelated project — the
coordinator's own join-command builder stopped offering the line for exactly
that reason, and the installer's copy of it has not been removed yet.

`--dry-run` is the one thing that still works off Linux: it prints the `docker
run` and exits before the platform check.

## 1. Machine one

Run this on the machine you want to be the coordinator.

```bash
curl -fsSL https://raw.githubusercontent.com/Pizzaman213/derate/main/install.sh | sh
```

It reports what it is doing on stderr, one line at a time:

```
[derate] pulling ghcr.io/pizzaman213/derate/node:latest
[derate] starting the node
[derate] waiting for the node to come up
[derate] installed build 264829a
```

Then it waits for `http://127.0.0.1:8081/agent/health` to answer — the one
endpoint that exists in both roles, so the same wait works on a coordinator and
on a node that joined one — and prints the address:

```
  derate is up.

  cluster:  qN7t2XbK9wR4mS1vZ0pJyL8dH3cF6gA5
  ui:       http://192.168.1.10:8080

  Add another machine: open the UI, Settings -> Add a node, and run the
  command it gives you on that machine. It joins and is admitted; there
  is nothing to click afterwards.

  (The permanent cluster token, if you ever need it directly:
   docker exec derate cat /data/cluster.json)
```

The permanent cluster token stays off the terminal on purpose. A token in a
scrollback is a token in a screenshot, and it is not the credential you carry
to the next machine anyway — see **The two tokens** below.

Open `http://192.168.1.10:8080` and the setup walkthrough runs. That is the
next page.

## 2. What that command actually did

The whole of it is one `docker run`. `--dry-run` prints it and stops, without
touching the machine and without needing Docker to be installed yet:

```bash
curl -fsSL https://raw.githubusercontent.com/Pizzaman213/derate/main/install.sh | sh -s -- --dry-run
```

On one line, that is:

```
docker run --gpus all --pid=host -d --name derate --network host --restart unless-stopped --label io.derate.node=1 -v derate:/data -e DERATE_PORT=8080 -e DERATE_AGENT_PORT=8081 ghcr.io/pizzaman213/derate/node:latest
```

Each flag is there because of what goes wrong without it.

**`--gpus all`** — the hardware probe is `nvidia-smi`. A container without one,
on a machine that has a GPU, probes as unidentified hardware: no device class,
no GPU name, zero memory. It still joins, still reports healthy, still appears
in the roster — and the planner never places work on it. Nothing looks broken.
That is the worst shape a failure takes here, which is why the installer says
so out loud when it has to fall back (see **What can go wrong**).

**`--pid=host`** — on GB10 every aggregate GPU memory field reads `[N/A]`, so
per-process accounting is the only thing that separates a resident model from
the desktop. `nvidia-smi` reports the processes it can see, and from inside its
own PID namespace it sees none of them: zero bytes of GPU memory, and the whole
unified pool attributed to the operating system.

**`--network host`** — mDNS is multicast and does not cross a Docker bridge, and
the agent needs the real interfaces to report the interconnect topology. This is
the one that fails loudly rather than quietly: the container refuses to start on
a bridge network and says so.

**`-v derate:/data`** — the cluster id and token, the node roster, the measured
links, the deployment records and the resolved-shape cache. Re-running the
installer removes the container and creates a new one; the volume is deliberately
untouched (`[derate] the derate volume is kept`). Without it, an upgrade would
come back as a different cluster and every node presenting the old token would
be turned away.

**`--restart unless-stopped`** brings the node back after a reboot.
**`--label io.derate.node=1`** is the mark a later run finds this container by,
including one somebody started by hand under another name.

Three more mounts are added, but only when the directory already exists on the
host — a bind mount of a missing path silently creates a root-owned directory in
your home:

```
-v $HOME/.config/sparkrun:/root/.config/sparkrun:ro    sparkrun's own config
-v $HOME/.ssh:/root/.ssh:ro                            sparkrun drives the cluster over SSH
-v $HOME/.cache/huggingface:/root/.cache/huggingface   the model cache, read-write
```

The last one is read-write, and it is what lets Settings → Storage show and
reclaim downloaded weights. Without it the node reports no model cache and
nothing breaks; it can only no longer see or delete what the runtime downloaded.

One thing to know before you put this on a shared network: **the gateway has no
inbound authentication yet.** Anyone who can reach port 8080 can call the API,
admit a node, and launch a model.

## 3. Machine two

The coordinator serves the installer itself, at `GET /install.sh`, as
`text/x-shellscript` — not HTML, because a proxy that decides to rewrite HTML in
flight would corrupt a script that is about to be piped into `sh`. The script it
serves never contains a token; the credential is only ever an argument.

```bash
curl -fsSL http://<coordinator>:8080/install.sh | sh -s -- \
    --join http://<coordinator>:8080 --token ej_...
```

Do not type that from memory. Open the coordinator's UI, go to
**Settings → Nodes → Add a node**, and press **Generate join command**: the
coordinator mints the token and composes the whole line with its own probed
address already in it. The browser's idea of the address is right in production
and a lie in development, and a wrong address fails on a machine nobody is
looking at. The next page covers that card.

When it finishes, the joining machine prints one of three things:

```
  This machine joined http://192.168.1.10:8080 and was admitted. It is a member now.
  It will appear in the cluster graph within a few seconds.
```

```
  This machine reached http://192.168.1.10:8080 and is waiting to be admitted.
  Open the UI at http://192.168.1.10:8080, go to Settings, and click Admit next to it.

  A token from the UI's "Add a node" card admits automatically.
  The permanent cluster token does not, by design.
```

```
  This machine is up and looking for a coordinator.
  Check the UI at http://192.168.1.10:8080, or read: docker logs derate
```

### The two tokens

They are not interchangeable, and the second one is the reason the first
message above exists.

| | Enrollment token (`ej_...`) | Permanent cluster token |
|---|---|---|
| Where it comes from | Settings → Add a node, minted on demand | generated on the coordinator's first start |
| Lives for | one hour by default, one day at most | forever |
| Spent | after one machine uses it | never |
| What it gets you | **member** — admitted on arrival, nothing to click | **candidate** — waits for somebody to press Admit |
| Can be revoked | yes, from the same card | no |

A node admitted with an enrollment token is handed the permanent token on the
way in, so the enrollment token expiring later cannot lock an established member
out of its own cluster.

## 4. The rest of the options

`install.sh --help` prints them all. The ones worth knowing:

- **`--dry-run`** — print the `docker run` and stop. Runs before the Docker
  check and before the Linux check, so you can read it from any machine.
- **`--no-gpu`** — do not pass the GPU in. On a machine that has one, the node
  then probes as unidentified hardware; on a machine that has none — a Pi, a
  NAS, a spare box — it probes as a CPU node, which is what it is. Neither is
  given a rank by the planner.
- **`--uninstall`** — stop and remove the container. The data volume is kept:
  `[derate] kept the derate volume. Re-run with --purge to delete it.`
- **`--purge`** — with `--uninstall`, also delete the volume (cluster token,
  roster, telemetry).
- **`--leave`** — forget the cluster this machine belongs to. It drops the
  cluster id and token and keeps this node's own identity, its label and its
  position on the cluster floor. Use it before moving a machine to a different
  cluster, or the node keeps presenting the old cluster's token and is turned
  away. Without `--uninstall` it restarts the node afterwards, so it comes back
  looking for a cluster to join.
- **`--install-docker`** — install Docker from `get.docker.com` if it is
  missing. Opt-in: the script does not install daemons you did not ask for.
- **`--install-ollama`** — install Ollama on this machine and bind it to
  `0.0.0.0:11434` through a systemd drop-in, so a node with no GPU can serve a
  small model over the network. The node itself does not need it and never calls
  it; you add that machine as a provider afterwards.
- **`--name`**, **`--image`**, **`--port`**, **`--agent-port`** — override the
  node id (default: this machine's hostname), the image, `8080` and `8081`.
- **`--keep-images`** — do not reclaim the derate images this upgrade
  superseded. By default, once the new node has answered its health check, the
  old ones are removed; a derate image is around 300 MB and every upgrade would
  otherwise leave one behind forever.

## 5. By hand, or with compose

The `docker run` is supported directly, and is the whole of what the script
does:

```bash
docker run -d --name derate --network host --gpus all --pid=host \
    --restart unless-stopped \
    -v derate:/data \
    -v "$HOME/.config/sparkrun:/root/.config/sparkrun:ro" \
    -v "$HOME/.ssh:/root/.ssh:ro" \
    ghcr.io/pizzaman213/derate/node
```

To make that machine join an existing cluster instead, add the two environment
variables the script would have set:

```bash
-e DERATE_JOIN=http://<coordinator>:8080 -e DERATE_TOKEN=ej_...
```

`compose.yaml` in the repository is the same container as a compose service, for
the single-node case and for people who prefer compose. It is never required —
its own header says why: a compose file you must have is a configuration step,
and the pitch is that there are none. What it adds over the `docker run` is a
health check and a commented list of every environment variable with its
default, so they are discoverable rather than because they need setting.

## What can go wrong here

**`[derate] docker is not installed.`**

```
    Install it, then re-run this script:
        curl -fsSL https://get.docker.com | sh
    or re-run this script with --install-docker to do that automatically.
    Packaged installs: https://docs.docker.com/engine/install/
```

**`[derate] docker is installed but not responding. Is the daemon running? Try: systemctl start docker`**
— the script has already tried re-running Docker under `sudo`, which is the fix
for the usual cause (your user is not in the `docker` group). If that had
worked, it would have said `[derate] using sudo for docker (this user is not in
the docker group)` and carried on.

**The GPU could not be passed in.** The run is retried without `--gpus all
--pid=host` rather than failing, and then the script tells you exactly what was
lost:

```
[derate] this machine has an NVIDIA driver but Docker could not pass the GPU
[derate] into the container, so the node started without it. It joins and
[derate] reports healthy, and it reports host memory, temperature and CPU --
[derate] but the probe can see the driver and not the GPU, so it records
[derate] unidentified hardware and the planner will not place work on it.
[derate] Install the NVIDIA container toolkit and re-run:
```

That is almost always the NVIDIA container toolkit: the driver is on the
machine, and Docker has no way to hand the device to a container. Install it and
re-run the same command — re-running is an upgrade in place and keeps the
volume.

**The image will not pull.** A locally built or `docker load`ed image is a
first-class case, not an error, so a failed pull is fatal only when this machine
has no copy at all:

```
[derate] could not pull ghcr.io/pizzaman213/derate/node:latest, and this machine has no local copy.
[derate] Copy it from a machine that has it:
[derate]   docker save ghcr.io/pizzaman213/derate/node:latest | gzip > node.tgz   # there
[derate]   gunzip -c node.tgz | sudo docker load  # here
[derate] no image to run
```

If the machine does have a copy, it says `[derate] could not pull ...; using the
copy already on this machine` and carries on.

**`[derate] the node did not answer /agent/health within 60s.`** — the script
prints the last 30 lines of `docker logs derate` and exits 1. If the container
stopped instead, it prints 40 lines and says `[derate] the container exited.
Its logs:`.

**Bridge networking**, which is what you get if you write your own `docker run`
and leave out `--network host`:

```
Bridge networking detected. mDNS discovery cannot cross a Docker bridge, so this node would never find or be found by a coordinator.
Start the container with host networking:
    docker run --network host -v derate:/data ghcr.io/pizzaman213/derate/node
If you are certain this is wrong, set DERATE_ALLOW_BRIDGE=1 to skip this check. Discovery will not work; use DERATE_JOIN=<addr> instead.
```

**`--token without --join has nothing to present it to. Add --join <coordinator url>.`**
— a token is a credential for a coordinator, so it needs one to present itself
to. On the first machine you want neither.

**`unknown option '--foo'. Try --help.`** — the script refuses rather than
ignoring it, because an ignored flag is an install that silently did something
else.

---

**Next:** [Your first visit to the coordinator](first-run.md) — the five-screen
walkthrough, and where a second machine's join command comes from.
