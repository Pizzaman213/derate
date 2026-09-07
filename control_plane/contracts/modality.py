"""What a served model actually does. 00-architecture.md appendix.

Additive to section 4: every field carrying a Modality defaults to TEXT, so
every record written before this existed still decodes, and a runtime that
never says otherwise keeps behaving exactly as it did.

The gateway has always had one endpoint family, so "which model" was the only
question a request had to answer. With /v1/audio/* there are two, and nothing
in the system could tell a TTS model from a chat model -- a provider catalog
already ingests whisper-1 and tts-1 as ordinary models (providers/discovery.py)
and they surface in /v1/models beside the chat ones. This enum is the missing
axis. It is deliberately about *the endpoint family a model answers on*, not
about the model's internals: that is the only distinction the router needs to
keep a chat request off a speech deployment.
"""

from __future__ import annotations

from enum import Enum


class Modality(str, Enum):
    TEXT = "text"  # /v1/chat/completions, /v1/completions
    EMBEDDING = "embedding"  # /v1/embeddings
    SPEECH = "speech"  # /v1/audio/speech        (text in, audio out)
    TRANSCRIPTION = "transcription"  # /v1/audio/transcriptions (audio in, text out)


#: The endpoint a model of each modality should be sent to. Used to tell a
#: client which one they wanted when they name a model on the wrong route,
#: because "no such model" would be a lie and a bare refusal is unactionable.
ENDPOINT_FOR_MODALITY: dict[Modality, str] = {
    Modality.TEXT: "/v1/chat/completions",
    Modality.EMBEDDING: "/v1/embeddings",
    Modality.SPEECH: "/v1/audio/speech",
    Modality.TRANSCRIPTION: "/v1/audio/transcriptions",
}
