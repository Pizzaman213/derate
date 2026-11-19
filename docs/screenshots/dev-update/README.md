# Development PR screenshots

Saved UI captures published for the development update on 2026-11-19. Copied from `ui/screens/`; these are not fresh captures of the live cluster.

| File | Surface | Context |
| --- | --- | --- |
| `login.png` | Administrator sign-in | Empty sign-in form; no credentials shown. |
| `cluster.png` | Cluster wiring editor | Two nodes, no active deployments. Shows the new editor rather than the retired read-only graph. |
| `usage-keys.png` | API keys and organizations | Key identifiers and usage only; no API-key secrets. |
| `chat-thinking.png` | Reasoning and session statistics | Scripted browser-verification responses. Timing and throughput are not benchmarks. |

These are screenshots of the rendered derate UI, not redrawn illustrations. The cluster wires are not retouched: straight, non-curved connections are tracked in [issue #5](https://github.com/Pizzaman213/derate/issues/5).

The PR embeds these files using immutable GitHub raw URLs. The source capture folder is ignored by Git, so links to `ui/screens/` would not work on GitHub.
