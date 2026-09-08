# derate/vllm-audio -- the container the `vllm` runtime launches.
#
# The upstream image plus an audio decoder, and nothing else. It exists
# because the pinned vLLM image cannot read an audio file at all: no
# torchcodec, no soundfile, no PyAV, and no system ffmpeg. vLLM serves
# /v1/audio/transcriptions from that image quite happily -- a Whisper
# deployment launches, goes READY, passes the health identity check and is
# offered on the route -- and then answers every upload with "Invalid or
# unsupported audio file." (entrypoints/speech_to_text/base/serving.py). A
# working gate, a working router, and nothing behind them.
#
# Text deployments are unaffected either way. This is the default anyway
# rather than a second image chosen per modality, because two images for one
# runtime is two things to keep in step with the base bump, and the delta
# below is a few megabytes on top of layers every node has already pulled.
#
#     docker buildx build -f docker/audio.Dockerfile \
#         --platform linux/arm64 \
#         -t ghcr.io/pizzaman213/derate/vllm-audio:latest --push .
#
# arm64 only, for the reason docker/tts.Dockerfile gives: a DGX Spark is a
# GB10. Publish it before this default reaches a node, or the pull fails and
# every vLLM launch fails with it; DERATE_VLLM_IMAGE points back at the
# upstream image for anyone who wants exactly it.

# Bump this together with the tts image's BASE, which is the same one.
ARG BASE=ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest
FROM ${BASE}

# Two packages, and both are load-bearing -- soundfile alone is not enough,
# which is not obvious and cost an afternoon to find out.
#
# vLLM's loader chain is torchcodec -> soundfile -> PyAV, so soundfile is what
# opens the file. But `load_audio_soundfile` hands any rate conversion to
# `resample_audio_pyav` unconditionally (multimodal/media/audio.py), and
# Whisper wants 16 kHz. So a 16 kHz clip decodes with soundfile alone and
# every other rate raises ImportError("Please install vllm[audio]") -- which
# includes the 44.1 kHz this project's own tts runtime writes. PyAV is what
# makes the resample work, and it brings its own ffmpeg, so it also covers
# the container formats libsndfile will not open (m4a, webm).
ARG AV="av>=13"
ARG SOUNDFILE="soundfile>=0.12"
RUN pip install --no-cache-dir "${AV}" "${SOUNDFILE}"

# Prove it at build time, through vLLM's own entry point rather than by
# importing the packages -- the failure this image exists to fix was two
# installed libraries that still could not answer this call. 44.1 kHz in,
# 16 kHz out, which is exactly what a transcription request does with what
# `POST /v1/audio/speech` produced.
# PYTHONDONTWRITEBYTECODE, because importing vLLM to run this check writes
# 60 MB of .pyc across its tree and bakes it into the image. A build-time
# assertion must not change what ships.
RUN PYTHONDONTWRITEBYTECODE=1 python3 -c "\
import io, struct, sys; \
rate = 44100; data = b'\x11\x00' * rate; \
head = b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE' \
     + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, rate, rate * 2, 2, 16) \
     + b'data' + struct.pack('<I', len(data)); \
from vllm.multimodal.media.audio import load_audio; \
y, sr = load_audio(io.BytesIO(head + data), sr=16000, mono=True); \
sys.exit('resampled to %s at %s, wanted 16000 samples at 16000' % (y.shape, sr)) \
    if (y.shape != (16000,) or sr != 16000) else None"

# No ENTRYPOINT and no CMD, deliberately, and unlike docker/tts.Dockerfile
# there is not even a --help default: the base image already has whatever it
# has, and sparkrun runs the recipe's `command` -- `vllm serve ...` with the
# knobs the planner and the fit gate decided on. Overriding either here would
# be a second opinion about how to start a server this file did not write.
