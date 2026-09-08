# derate/tts -- the container the `tts` runtime launches.
#
# Not the node image. That one is a control plane and says so out loud: it
# probes and samples and NEVER runs a model, which is why it asks the NVIDIA
# runtime for `utility` capabilities and ships no torch. This is the other
# kind of image, the kind sparkrun pulls onto a machine and runs a serve
# command inside -- the same role `dgx-vllm-eugr-nightly` plays for the vllm
# runtime and `dgx-spark-sglang` for sglang.
#
# Build and publish it exactly as those are consumed: by tag, pinned per
# deployment, overridable with DERATE_TTS_IMAGE.
#
#     docker buildx build -f docker/tts.Dockerfile \
#         --platform linux/arm64 \
#         -t ghcr.io/pizzaman213/derate/tts:latest --push .
#
# arm64 is the platform that matters -- a DGX Spark is a GB10 -- and unlike
# the node image there is no reason to build amd64 unless somebody is serving
# speech off a workstation, hence no default multi-arch build.

# The vLLM image this project already pins for the vllm runtime, and already
# pulls onto every node that has served a model. It is arm64, it carries a
# torch built against this hardware's CUDA, and reusing it means the layers
# below are the only new bytes a node has to fetch. Bump this together with
# DERATE_VLLM_IMAGE in control_plane/deploy/flags.py, or pass --build-arg.
ARG BASE=ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest
FROM ${BASE}

# transformers is the loader: these checkpoints ship their architecture as
# remote code in the repository, so what runs the model is the model's own
# Python and transformers is what executes it. Pinned to a major, because the
# remote code in a published checkpoint was written against one.
#
# soundfile is libsndfile, and it is the whole encoder: wav, mp3, flac, opus
# and raw pcm come out of it and nothing else does. scipy is there for exactly
# one job -- 44.1 kHz to 48 kHz for opus, which libsndfile will not write at
# the rate these codecs produce. Without it the server still starts and still
# serves the other four; it refuses opus by name, which is the honest failure.
ARG TRANSFORMERS="transformers>=4.57,<6"
RUN pip install --no-cache-dir \
      "${TRANSFORMERS}" \
      "soundfile>=0.12" \
      "scipy>=1.11" \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.34" \
 && python3 -c "import soundfile, sys; \
        missing = [f for f in ('WAV','MP3','FLAC','OGG','RAW') \
                   if f not in soundfile.available_formats()]; \
        sys.exit('libsndfile in this base cannot write: %s' % missing) if missing else None"

# Only what the server needs. `control_plane/runtimes/` imports nothing else in
# the package, and copying the whole control plane would put the coordinator's
# code -- and its dependency list -- inside the model container for no reason.
WORKDIR /opt/derate
COPY control_plane/__init__.py /opt/derate/control_plane/__init__.py
COPY control_plane/runtimes/ /opt/derate/control_plane/runtimes/
ENV PYTHONPATH=/opt/derate \
    PYTHONUNBUFFERED=1

# Where a voice library is, if there is one. Empty is the ordinary case: with
# no voices the model still speaks, in its own.
#
# This default is for running the container by hand:
#
#     -v /srv/voices:/voices
#
# A derate launch overrides it. sparkrun's containers get exactly one writable
# mount -- the HuggingFace cache -- and the recipe format has no `volumes:`
# key, so `env:` is the only channel that reaches them; `deploy/flags.py` sends
# DERATE_TTS_VOICE_DIR into RUNTIME_CACHE_DIR/voices, beside `hub/` and not
# inside it. Left here anyway rather than deleted: /voices is still right for
# anybody who runs this image themselves, and an image whose only voice path
# came from an orchestrator would be one you could not test on its own.
ENV DERATE_TTS_VOICE_DIR=/voices

# Documentation only: the port comes from the recipe, which gets it from the
# manager's own allocation, and host networking ignores published ports anyway.
EXPOSE 8100

# No ENTRYPOINT. sparkrun runs the recipe's `command`, which is
# `python3 -m control_plane.runtimes.tts ...` with every knob the planner and
# the fit gate decided on -- see control_plane/deploy/flags.py. An entrypoint
# here would be a second opinion about how to start the server.
CMD ["python3", "-m", "control_plane.runtimes.tts", "--help"]
