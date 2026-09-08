"""Inference servers derate ships itself.

Everything here runs INSIDE a model container, never in the node image: the
dependencies are torch, transformers and an audio encoder, and none of them
are in ``requirements.txt``. Nothing else in ``control_plane/`` imports this
package, and every module in it keeps its heavy imports inside the functions
that need them, so importing one on a machine with no CUDA still works and its
pure parts stay testable.

There is exactly one, and it exists because the alternative was nothing:
``tts`` serves ``POST /v1/audio/speech``, which neither vLLM nor SGLang has.
"""
