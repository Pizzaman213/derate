# Research Prompt 2: How Local Runtimes Decide What Models Will Fit

## Objective
Reverse-engineer the logic that consumer and prosumer LLM tools use to tell a user "this model will run on your machine," so the same gating can be built for a multi-Spark cluster where the memory pool spans nodes.

## Core Question
Given a Hugging Face model ID and a hardware profile, how do existing tools compute whether the model is runnable, at what quantization, and with what context length?

## Tools to Analyze

### Primary
- LM Studio: its model catalog compatibility badges, how it estimates VRAM/unified memory requirements, and how it handles the Apple Silicon unified memory case, which is the closest analogue to Spark's memory model
- Ollama: the modelfile system, automatic quantization selection, and what happens on insufficient memory
- Jan and GPT4All: their compatibility checks and how they present them
- Hugging Face Hub itself: the hardware compatibility metadata, `safetensors` index parsing, and any official memory estimator utilities or Spaces

### Secondary
- llama.cpp and its layer offload calculation
- vLLM's own preflight memory profiling and `gpu_memory_utilization` behavior
- Any standalone VRAM calculator projects worth borrowing math from

## Specific Things to Extract

### The math
- How parameter count, dtype, and quantization scheme convert to a base weight footprint
- How KV cache size is computed as a function of context length, batch size, layer count, head count, and head dim
- What overhead margin each tool reserves for activations, CUDA context, and fragmentation
- How MoE models are handled, since active versus total parameters change the calculation significantly
- How multimodal and vision towers are accounted for

### The metadata problem
- Where each tool sources architecture details: `config.json`, GGUF headers, safetensors index, or a curated catalog
- What happens with a model whose config is nonstandard or whose architecture is unsupported by the backend
- How each tool handles the gap between "weights fit" and "the runtime actually supports this architecture"

### The presentation
- How compatibility is surfaced to the user: hard block, warning, or estimated tokens/sec
- Whether tools distinguish "will not load" from "will load but will be slow"

## Deliverable
A written spec of the fit calculation, precise enough to implement, plus notes on where each tool's estimate is known to be wrong or overly conservative. Include the multi-node wrinkle: what changes when the memory pool is split across two or more nodes connected by a finite-bandwidth link.
